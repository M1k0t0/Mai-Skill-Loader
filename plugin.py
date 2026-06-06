"""Skill Loader v2 — Agent Skills 加载器

架构：
- 覆写 get_components() 动态返回 skill tools
- 覆写 invoke_component() 统一分发 skill 调用
- Agent loop 带 token budget 和 context 截断
- Capabilities 带安全限制（bash 审批、路径白名单、高危命令黑名单）
- 多轮会话缓存（stream_id + skill_name 维度）
- 聊天上下文注入
- /skill reload 热加载
- /skill enable|disable 运行时开关
"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import asyncio
import importlib.util
import json
import logging
import re
import time
import traceback

import yaml

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase

logger = logging.getLogger("skill_loader")

# ====== 配置 ======


class CapabilitiesConfig(PluginConfigBase):
    """能力权限配置。"""
    __ui_label__ = "能力权限"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    allow_bash: bool = Field(default=True, description="允许执行 shell 命令")
    allow_read: bool = Field(default=True, description="允许读取文件")
    allow_write: bool = Field(default=False, description="允许写入文件")
    allow_edit: bool = Field(default=False, description="允许编辑文件（查找替换）")
    bash_working_dir: str = Field(default="", description="bash 工作目录（空=插件目录）")
    bash_timeout: int = Field(default=30, description="bash 命令超时（秒）")
    bash_blocked_commands: List[str] = Field(
        default_factory=lambda: [
            "rm -rf /", "rm -rf ~", "rm -rf .", "rmdir /",
            "mkfs", "dd if=", "shutdown", "reboot", "poweroff", "init 0", "init 6",
            "chmod -R 777 /", "chown -R", ":(){ :|:& };:",
            "curl|bash", "curl|sh", "wget|bash", "wget|sh",
            "> /dev/sda", "mv / ", "cat /dev/zero",
            "passwd", "useradd", "userdel", "visudo",
            "iptables -F", "ufw disable",
            "systemctl stop", "systemctl disable",
            "kill -9 1", "killall",
        ],
        description="禁止的命令模式",
    )
    read_allowed_dirs: List[str] = Field(
        default_factory=list,
        description="读取目录白名单（空=默认仅限插件目录）",
    )
    write_allowed_dirs: List[str] = Field(
        default_factory=list,
        description="写入目录白名单（空=默认仅限插件 data/ 目录）",
    )
    write_max_size_kb: int = Field(default=1024, description="写入文件最大 KB")
    bash_require_approval: bool = Field(default=True, description="bash 命令是否需要管理员审批")
    bash_approval_timeout: int = Field(default=120, description="审批等待超时（秒）")
    admin_ids: List[str] = Field(
        default_factory=list,
        description="管理员 ID 列表（格式: 'qq:123456' 或纯数字 QQ 号）",
    )


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置节。"""
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class SkillLoaderConfig(PluginConfigBase):
    """Skill Loader 主配置。"""
    __ui_label__ = "Skill Loader"
    __ui_icon__ = "zap"
    __ui_order__ = 0

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    skills_dir: str = Field(default="skills", description="skills 目录路径")
    default_model: str = Field(default="", description="agent 默认模型（空=系统默认）")
    default_max_turns: int = Field(default=10, description="agent 默认最大轮数")
    timeout_seconds: int = Field(default=60, description="skill 调用超时（秒）")
    agent_max_context_tokens: int = Field(default=8000, description="agent 上下文 token 预算")
    session_enabled: bool = Field(default=True, description="是否启用多轮会话")
    session_ttl_seconds: int = Field(default=300, description="会话过期时间（秒），超时未交互则清除")
    session_max_history: int = Field(default=20, description="会话最多保留的消息轮数（超出截断旧轮）")
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)


# ====== Skill 定义与解析 ======

# allowed-tools 到 maibot capabilities 的映射
TOOLS_TO_CAPS: Dict[str, str] = {
    "bash": "bash", "Bash": "bash",
    "read": "read", "Read": "read",
    "write": "write", "Write": "write",
    "edit": "edit", "Edit": "edit",
}

# name 格式校验正则：小写字母、数字、连字符，不能以连字符开头/结尾，不能连续连字符
_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_CONSECUTIVE_HYPHENS = re.compile(r"--")


def _validate_name(name: str, dir_name: str) -> Optional[str]:
    """校验 skill name 是否符合规范，返回错误信息或 None。"""
    if not name:
        return "name 不能为空"
    if len(name) > 64:
        return f"name 超过 64 字符 ({len(name)})"
    if not _NAME_PATTERN.match(name):
        return f"name '{name}' 格式不合法（只允许小写字母、数字、连字符）"
    if _CONSECUTIVE_HYPHENS.search(name):
        return f"name '{name}' 包含连续连字符"
    if name != dir_name:
        return f"name '{name}' 与目录名 '{dir_name}' 不匹配"
    return None


def _parse_allowed_tools(allowed_tools: str) -> List[str]:
    """解析 allowed-tools 字段为 capabilities 列表。
    
    支持规范格式如 'Bash(git:*) Read' 和简写格式如 'bash read_file'。
    """
    if not allowed_tools:
        return []
    caps: List[str] = []
    # 按空格分割，去掉括号内的参数
    for token in allowed_tools.split():
        base_tool = token.split("(")[0]
        cap = TOOLS_TO_CAPS.get(base_tool)
        if cap and cap not in caps:
            caps.append(cap)
    return caps


class SkillDefinition:
    """解析后的 Skill（符合 Agent Skills 规范 + maibot 扩展）。"""
    __slots__ = (
        "name", "description", "mode", "model", "max_turns",
        "instructions", "scripts", "skill_path", "capabilities",
        "license", "compatibility", "metadata", "references_dir", "assets_dir",
    )

    def __init__(self, *, name: str, description: str, mode: str, model: str,
                 max_turns: int, instructions: str, scripts: Dict[str, Path],
                 skill_path: Path, capabilities: List[str],
                 license: str = "", compatibility: str = "",
                 metadata: Optional[Dict[str, str]] = None,
                 references_dir: Optional[Path] = None,
                 assets_dir: Optional[Path] = None):
        self.name = name
        self.description = description
        self.mode = mode
        self.model = model
        self.max_turns = max_turns
        self.instructions = instructions
        self.scripts = scripts
        self.skill_path = skill_path
        self.capabilities = capabilities
        self.license = license
        self.compatibility = compatibility
        self.metadata = metadata or {}
        self.references_dir = references_dir
        self.assets_dir = assets_dir


def parse_skill(skill_path: Path) -> Optional[SkillDefinition]:
    """解析单个 skill 目录（符合 Agent Skills 规范）。"""
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")
    frontmatter: Dict[str, Any] = {}
    instructions = content

    # 解析 YAML frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as e:
                logger.warning(f"SKILL.md 解析失败 ({skill_path.name}): {e}")
                return None
            instructions = parts[2].strip()

    # 标准字段
    name = str(frontmatter.get("name", skill_path.name)).strip()
    description = str(frontmatter.get("description", "")).strip()

    # name 校验
    name_error = _validate_name(name, skill_path.name)
    if name_error:
        logger.warning(f"Skill '{skill_path.name}' 跳过: {name_error}")
        return None

    # description 校验
    if not description:
        logger.warning(f"Skill '{name}' 缺少 description，跳过")
        return None
    if len(description) > 1024:
        logger.warning(f"Skill '{name}' description 超过 1024 字符，已截断")
        description = description[:1024]

    # 可选标准字段
    license_field = str(frontmatter.get("license", "")).strip()
    compatibility = str(frontmatter.get("compatibility", "")).strip()
    if len(compatibility) > 500:
        compatibility = compatibility[:500]
    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    # allowed-tools → capabilities
    allowed_tools = str(frontmatter.get("allowed-tools", "")).strip()
    capabilities = _parse_allowed_tools(allowed_tools)

    # maibot 扩展（从 metadata 读取）
    mode = str(metadata.get("maibot-mode", "agent")).strip()
    if mode not in ("direct", "agent"):
        mode = "agent"
    model = str(metadata.get("maibot-model", "")).strip()
    max_turns_raw = metadata.get("maibot-max-turns", 10)
    try:
        max_turns = int(max_turns_raw)
    except (TypeError, ValueError):
        logger.warning(f"Skill '{name}' maibot-max-turns 无效: {max_turns_raw!r}，使用默认 10")
        max_turns = 10

    # 是否默认启用（maibot-enabled: false 则跳过）
    enabled = str(metadata.get("maibot-enabled", "true")).strip().lower()
    if enabled in ("false", "0", "no", "off"):
        logger.info(f"Skill '{name}' 已禁用 (maibot-enabled: false)")
        return None

    # scripts/ 目录
    scripts: Dict[str, Path] = {}
    scripts_dir = skill_path / "scripts"
    if scripts_dir.exists():
        for f in scripts_dir.glob("*.py"):
            scripts[f.stem] = f

    # references/ 和 assets/ 目录
    references_dir = skill_path / "references"
    assets_dir = skill_path / "assets"

    return SkillDefinition(
        name=name, description=description, mode=mode, model=model,
        max_turns=max_turns, instructions=instructions, scripts=scripts,
        skill_path=skill_path, capabilities=capabilities,
        license=license_field, compatibility=compatibility,
        metadata=metadata,
        references_dir=references_dir if references_dir.exists() else None,
        assets_dir=assets_dir if assets_dir.exists() else None,
    )


def _try_parse_skill(skill_path: Path) -> Optional[SkillDefinition]:
    """解析 skill 目录，失败时记录日志并返回 None。"""
    try:
        return parse_skill(skill_path)
    except Exception as exc:
        logger.warning(f"解析 skill 失败 ({skill_path.name}): {exc}")
        return None


def scan_skills(skills_dir: Path) -> Dict[str, SkillDefinition]:
    """扫描目录返回 {name: SkillDefinition}。
    
    支持多种布局：
    1. 直接子目录: skills_dir/<skill-name>/SKILL.md
    2. .agents/skills 标准路径: skills_dir/.agents/skills/<skill-name>/SKILL.md
    3. 项目根 .agents/skills: 向上查找项目根目录的 .agents/skills/
    """
    result: Dict[str, SkillDefinition] = {}
    if not skills_dir.exists():
        return result

    # 扫描直接子目录
    for item in sorted(skills_dir.iterdir()):
        if not item.is_dir() or item.name.startswith(("_", ".")):
            continue
        skill = _try_parse_skill(item)
        if skill:
            result[skill.name] = skill

    # 扫描 .agents/skills/ 标准路径（npx skills add 在 skills_dir 下执行时的安装位置）
    agents_skills_dir = skills_dir / ".agents" / "skills"
    if agents_skills_dir.exists():
        for item in sorted(agents_skills_dir.iterdir()):
            if not item.is_dir() or item.name.startswith(("_", ".")):
                continue
            skill = _try_parse_skill(item)
            if skill and skill.name not in result:
                result[skill.name] = skill

    # 扫描项目根目录的 .agents/skills/（npx skills add 在项目根执行时的安装位置）
    # 从 skills_dir 向上找到包含 bot.py 或 pyproject.toml 的目录
    project_root = skills_dir
    for candidate in [skills_dir, *skills_dir.parents]:
        if (candidate / "bot.py").exists() or (candidate / "pyproject.toml").exists():
            project_root = candidate
            break
    root_agents_dir = project_root / ".agents" / "skills"
    if root_agents_dir.exists() and root_agents_dir != agents_skills_dir:
        for item in sorted(root_agents_dir.iterdir()):
            if not item.is_dir() or item.name.startswith(("_", ".")):
                continue
            skill = _try_parse_skill(item)
            if skill and skill.name not in result:
                result[skill.name] = skill

    return result


# ====== Capabilities 执行器 ======

CAPABILITY_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "bash": {"type": "function", "function": {"name": "bash", "description": "执行 shell 命令", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "shell 命令"}}, "required": ["command"]}}},
    "read": {"type": "function", "function": {"name": "read", "description": "读取文件内容（支持 txt/md/docx/pdf）", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}, "max_lines": {"type": "integer", "description": "最大行数，默认200（仅文本文件有效）"}}, "required": ["path"]}}},
    "write": {"type": "function", "function": {"name": "write", "description": "创建或覆盖文件", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}, "content": {"type": "string", "description": "文件内容"}}, "required": ["path", "content"]}}},
    "edit": {"type": "function", "function": {"name": "edit", "description": "编辑文件：查找并替换指定内容", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}, "old_str": {"type": "string", "description": "要查找的原始文本"}, "new_str": {"type": "string", "description": "替换为的新文本"}}, "required": ["path", "old_str", "new_str"]}}},
}


CAPABILITY_NAMES: tuple[str, ...] = ("bash", "read", "write", "edit")
CAPABILITY_CONFIG_ATTRS: Dict[str, str] = {
    "bash": "allow_bash",
    "read": "allow_read",
    "write": "allow_write",
    "edit": "allow_edit",
}
SKILL_COMMAND_PATTERN = (
    r"^/skill(?:\s+(?P<action>list|caps|enable|disable|reload)"
    r"(?:\s+(?P<target>\S+)(?:\s+(?P<skill_name>\S+))?)?)?$"
)


class SkillCapGrantStore:
    """Per-skill capability 运行时授权。未单独配置的 skill 继承全局开关。"""

    def __init__(self) -> None:
        self._grants: Dict[str, Set[str]] = {}

    def is_configured(self, skill_name: str) -> bool:
        return skill_name in self._grants

    def get_granted(self, skill_name: str) -> Optional[Set[str]]:
        return self._grants.get(skill_name)

    def ensure_initialized(self, skill_name: str, inherited_caps: Iterable[str]) -> Set[str]:
        if skill_name not in self._grants:
            self._grants[skill_name] = set(inherited_caps)
        return self._grants[skill_name]

    def prune(self, valid_skill_names: Iterable[str]) -> None:
        valid = set(valid_skill_names)
        for name in list(self._grants):
            if name not in valid:
                del self._grants[name]

    def reset_skill(self, skill_name: str) -> None:
        self._grants.pop(skill_name, None)


def _global_allowed_caps(skill: SkillDefinition, cap_cfg: CapabilitiesConfig) -> List[str]:
    """返回仅受全局开关约束的 capabilities。"""
    perm = {
        "bash": cap_cfg.allow_bash,
        "read": cap_cfg.allow_read,
        "write": cap_cfg.allow_write,
        "edit": cap_cfg.allow_edit,
    }
    return [cap for cap in skill.capabilities if perm.get(cap, False)]


def get_allowed_caps(
    skill: SkillDefinition,
    cap_cfg: CapabilitiesConfig,
    grant_store: Optional[SkillCapGrantStore] = None,
) -> List[str]:
    """返回 skill 实际被允许的 capabilities（全局开关 + 可选 per-skill 授权）。"""
    global_allowed = _global_allowed_caps(skill, cap_cfg)
    if grant_store is None or not grant_store.is_configured(skill.name):
        return global_allowed
    explicit = grant_store.get_granted(skill.name) or set()
    return [cap for cap in global_allowed if cap in explicit]


def _resolve_read_roots(
    cfg_dirs: List[str],
    plugin_dir: Optional[Path],
    extra_roots: Optional[List[Path]] = None,
) -> List[Path]:
    """解析读取目录白名单；未配置时默认插件目录 + 当前 skill 等资源目录。"""
    if cfg_dirs:
        return [Path(d).resolve() for d in cfg_dirs]

    roots: List[Path] = []
    seen: set[str] = set()
    for candidate in [plugin_dir, *(extra_roots or [])]:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _coerce_max_lines(raw: Any, default: int = 200) -> int:
    """将 read.max_lines 参数安全转换为正整数。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _resolve_write_roots(cfg_dirs: List[str], plugin_dir: Optional[Path]) -> List[Path]:
    """解析写入目录白名单；未配置时默认仅限插件 data/ 目录。"""
    if cfg_dirs:
        return [Path(d).resolve() for d in cfg_dirs]
    if plugin_dir is None:
        return []
    data_dir = (plugin_dir / "data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return [data_dir]


def _is_path_allowed(fp: Path, allowed_roots: List[Path]) -> bool:
    """检查路径是否在白名单根目录下。"""
    if not allowed_roots:
        return False
    return any(fp == root or root in fp.parents for root in allowed_roots)


async def run_capability(
    name: str,
    args: Dict[str, Any],
    cfg: CapabilitiesConfig,
    ctx: Any = None,
    stream_id: str = "",
    plugin_dir: Optional[Path] = None,
    read_extra_roots: Optional[List[Path]] = None,
) -> str:
    """执行单个 capability tool。"""
    if name == "bash":
        return await _cap_bash(args.get("command", ""), cfg, ctx=ctx, stream_id=stream_id, plugin_dir=plugin_dir)
    elif name == "read":
        return await _cap_read_file(
            args.get("path", ""),
            cfg,
            _coerce_max_lines(args.get("max_lines", 200)),
            plugin_dir=plugin_dir,
            read_extra_roots=read_extra_roots,
        )
    elif name == "write":
        return await _cap_write_file(args.get("path", ""), args.get("content", ""), cfg, plugin_dir=plugin_dir)
    elif name == "edit":
        return await _cap_edit_file(
            args.get("path", ""),
            args.get("old_str", ""),
            args.get("new_str", ""),
            cfg,
            plugin_dir=plugin_dir,
        )
    return f"未知 capability: {name}"


def _is_admin(user_id: str, admin_ids: List[str], platform: str = "") -> bool:
    """检查用户是否为管理员。支持 'platform:id' 和纯数字格式。"""
    normalized_user_id = str(user_id or "").strip()
    normalized_platform = str(platform or "").strip()
    if not normalized_user_id:
        return False

    for admin in admin_ids:
        admin_entry = str(admin or "").strip()
        if not admin_entry:
            continue
        if ":" in admin_entry:
            # 格式: qq:123456，必须平台与用户 ID 同时匹配
            admin_platform, admin_user_id = admin_entry.split(":", 1)
            admin_platform = admin_platform.strip()
            admin_user_id = admin_user_id.strip()
            if not normalized_platform or admin_platform != normalized_platform:
                continue
            if normalized_user_id == admin_user_id:
                return True
        elif normalized_user_id == admin_entry:
            return True
    return False


def _extract_message_timestamp(msg: Any) -> float:
    """从 get_recent 返回的消息中提取 Unix 时间戳。"""
    if not isinstance(msg, dict):
        return 0.0
    try:
        return float(msg.get("timestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _extract_message_user_id(msg: Any) -> str:
    """从 get_recent 返回的消息中提取发送者 user_id。"""
    if not isinstance(msg, dict):
        return ""
    top_level = str(msg.get("user_id") or "").strip()
    if top_level:
        return top_level
    message_info = msg.get("message_info")
    if isinstance(message_info, dict):
        user_info = message_info.get("user_info")
        if isinstance(user_info, dict):
            return str(user_info.get("user_id") or "").strip()
    return ""


def _extract_message_text(msg: Any) -> str:
    """从 get_recent 返回的消息中提取纯文本内容。"""
    if not isinstance(msg, dict):
        return ""
    top_content = str(msg.get("content") or "").strip()
    if top_content:
        return top_content
    raw_message = msg.get("raw_message")
    if not isinstance(raw_message, list):
        return ""
    parts: List[str] = []
    for segment in raw_message:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, str):
            parts.append(data)
        elif data is not None:
            parts.append(str(data))
    return "".join(parts).strip()


async def _wait_admin_approval(command: str, cfg: CapabilitiesConfig, ctx: Any, stream_id: str) -> bool:
    """发送审批请求并等待管理员回复 Y/n。"""
    if not cfg.admin_ids:
        return False  # 未配置管理员时拒绝所有 bash
    if not ctx or not stream_id:
        return False  # 无法发送审批请求时拒绝

    # 发送审批请求
    approval_msg = (
        f"[Skill Loader 安全审批]\n"
        f"Skill agent 请求执行以下命令:\n"
        f"$ {command}\n\n"
        f"管理员请回复 Y 同意 / N 拒绝（{cfg.bash_approval_timeout}秒超时自动拒绝）"
    )
    await ctx.send.text(approval_msg, stream_id)

    # 轮询等待管理员回复
    start_time = time.time()
    poll_interval = 2  # 每2秒检查一次
    while time.time() - start_time < cfg.bash_approval_timeout:
        await asyncio.sleep(poll_interval)
        try:
            recent = await ctx.message.get_recent(stream_id, limit=10)
            if not recent:
                continue
            for msg in recent:
                # 检查是否是审批请求之后的消息
                msg_time = _extract_message_timestamp(msg)
                if msg_time < start_time:
                    continue
                sender_id = _extract_message_user_id(msg)
                sender_platform = str(msg.get("platform") or "").strip()
                if not _is_admin(sender_id, cfg.admin_ids, sender_platform):
                    continue
                content = _extract_message_text(msg).strip().upper()
                if content in ("Y", "YES", "是", "同意"):
                    await ctx.send.text("[审批通过] 正在执行命令...", stream_id)
                    return True
                if content in ("N", "NO", "否", "拒绝"):
                    return False
        except Exception:
            continue

    return False  # 超时自动拒绝


async def _cap_bash(command: str, cfg: CapabilitiesConfig, ctx: Any = None, stream_id: str = "",
                    plugin_dir: Optional[Path] = None) -> str:
    # 高危命令直接拒绝
    for blocked in cfg.bash_blocked_commands:
        if blocked in command:
            return f"安全策略阻止: 命令包含 '{blocked}'"

    # 需要管理员审批
    if cfg.bash_require_approval:
        approved = await _wait_admin_approval(command, cfg, ctx, stream_id)
        if not approved:
            return "管理员拒绝执行该命令，或审批超时。"

    try:
        if cfg.bash_working_dir:
            working_dir = cfg.bash_working_dir
        elif plugin_dir:
            working_dir = str(plugin_dir)
        else:
            working_dir = None
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cfg.bash_timeout)
        out = stdout.decode("utf-8", errors="replace")[:20000]
        err = stderr.decode("utf-8", errors="replace")[:5000]
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        parts.append(f"[exit={proc.returncode}]")
        return "\n".join(parts)
    except asyncio.TimeoutError:
        return f"命令超时 ({cfg.bash_timeout}s)"
    except Exception as e:
        return f"执行失败: {e}"


async def _require_dependency(package_name: str, pip_name: str) -> Optional[str]:
    """检查依赖是否已安装，缺失时返回错误信息。"""
    try:
        __import__(package_name)
        return None
    except ImportError:
        return f"缺少 {pip_name} 依赖，请在插件 manifest 中声明并由依赖流水线安装"


async def _cap_read_file(
    path: str,
    cfg: CapabilitiesConfig,
    max_lines: int = 200,
    plugin_dir: Optional[Path] = None,
    read_extra_roots: Optional[List[Path]] = None,
) -> str:
    raw_fp = Path(path)
    if raw_fp.is_symlink():
        return "安全策略阻止: 不允许读取符号链接"
    fp = raw_fp.resolve()
    allowed_roots = _resolve_read_roots(cfg.read_allowed_dirs, plugin_dir, read_extra_roots)
    if not _is_path_allowed(fp, allowed_roots):
        return f"安全策略阻止: {path} 不在白名单目录中"
    if not fp.exists():
        return f"文件不存在: {path}"

    suffix = fp.suffix.lower()
    try:
        # docx 文件
        if suffix == ".docx":
            err = await _require_dependency("docx", "python-docx")
            if err:
                return err
            from docx import Document
            doc = Document(str(fp))
            content = "\n".join(p.text for p in doc.paragraphs)
            if len(content) > 60000:
                return content[:60000] + f"\n... (截断，共 {len(content)} 字符)"
            return content

        # pdf 文件
        if suffix == ".pdf":
            err = await _require_dependency("pypdf", "pypdf")
            if err:
                return err
            from pypdf import PdfReader
            reader = PdfReader(str(fp))
            pages = []
            for i, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                pages.append(f"--- 第 {i} 页 ---\n{text.strip()}")
            content = "\n\n".join(pages)
            if len(content) > 60000:
                return content[:60000] + f"\n... (截断，共 {len(content)} 字符)"
            return content

        # 纯文本（txt/md 等）
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... (截断，共 {len(lines)} 行)"
        return "\n".join(lines)
    except Exception as e:
        return f"读取失败: {e}"


async def _cap_write_file(path: str, content: str, cfg: CapabilitiesConfig,
                          plugin_dir: Optional[Path] = None) -> str:
    raw_fp = Path(path)
    if raw_fp.exists() and raw_fp.is_symlink():
        return "安全策略阻止: 不允许写入符号链接"
    fp = raw_fp.resolve()
    allowed_roots = _resolve_write_roots(cfg.write_allowed_dirs, plugin_dir)
    if not _is_path_allowed(fp, allowed_roots):
        return f"安全策略阻止: {path} 不在白名单目录中"
    if len(content.encode()) > cfg.write_max_size_kb * 1024:
        return f"内容超过 {cfg.write_max_size_kb}KB 限制"
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字符到 {path}"
    except Exception as e:
        return f"写入失败: {e}"


async def _cap_edit_file(path: str, old_str: str, new_str: str, cfg: CapabilitiesConfig,
                         plugin_dir: Optional[Path] = None) -> str:
    """查找替换文件内容。"""
    raw_fp = Path(path)
    if raw_fp.is_symlink():
        return "安全策略阻止: 不允许编辑符号链接"
    fp = raw_fp.resolve()
    allowed_roots = _resolve_write_roots(cfg.write_allowed_dirs, plugin_dir)
    if not _is_path_allowed(fp, allowed_roots):
        return f"安全策略阻止: {path} 不在白名单目录中"
    if not fp.exists():
        return f"文件不存在: {path}"
    if not fp.is_file():
        return f"不是普通文件: {path}"
    if not old_str:
        return "old_str 不能为空"
    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
        if old_str not in content:
            return "未找到要替换的内容"
        count = content.count(old_str)
        new_content = content.replace(old_str, new_str)
        fp.write_text(new_content, encoding="utf-8")
        return f"已替换 {count} 处匹配"
    except Exception as e:
        return f"编辑失败: {e}"


# ====== 多轮会话缓存 ======


class SessionStore:
    """管理 skill 多轮会话的短期缓存。"""

    def __init__(self):
        # key: "stream_id:skill_name" → {"messages": [...], "last_active": timestamp}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def _key(self, stream_id: str, skill_name: str) -> str:
        return f"{stream_id}:{skill_name}"

    def get(self, stream_id: str, skill_name: str, ttl: int) -> Optional[List[Dict[str, Any]]]:
        """获取活跃会话的 messages，过期返回 None。"""
        key = self._key(stream_id, skill_name)
        session = self._sessions.get(key)
        if not session:
            return None
        if time.time() - session["last_active"] > ttl:
            del self._sessions[key]
            return None
        return session["messages"]

    def save(self, stream_id: str, skill_name: str, messages: List[Dict[str, Any]], max_history: int) -> None:
        """保存会话 messages，截断超出的旧轮。"""
        key = self._key(stream_id, skill_name)
        # 只保留可独立回放的对话消息。工具调用链如果被截断，OpenAI 兼容接口会因为
        # assistant.tool_calls 与 tool 消息不成对而拒绝请求。
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [
            m
            for m in messages
            if m.get("role") in {"user", "assistant"} and not m.get("tool_calls")
        ]
        if len(other_msgs) > max_history:
            other_msgs = other_msgs[-max_history:]
        self._sessions[key] = {
            "messages": system_msgs + other_msgs,
            "last_active": time.time(),
        }

    def clear(self, stream_id: str, skill_name: str) -> None:
        """清除指定会话。"""
        key = self._key(stream_id, skill_name)
        self._sessions.pop(key, None)

    def cleanup_expired(self, ttl: int) -> None:
        """清理所有过期会话。"""
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v["last_active"] > ttl]
        for k in expired:
            del self._sessions[k]


# 全局会话存储实例
_session_store = SessionStore()

# 脚本模块缓存（key: 绝对路径）
_script_modules: Dict[str, Any] = {}


def _clear_script_cache() -> None:
    """清除脚本模块缓存，reload 后重新加载。"""
    _script_modules.clear()


def _load_script_module(script_path: Path) -> Optional[Any]:
    """加载并缓存 skill 脚本模块。"""
    cache_key = str(script_path.resolve())
    cached = _script_modules.get(cache_key)
    if cached is not None:
        return cached
    try:
        module_name = f"skill_script_{script_path.stem}_{abs(hash(cache_key))}"
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _script_modules[cache_key] = module
        return module
    except Exception as e:
        logger.warning(f"加载脚本失败 {script_path}: {e}")
        return None


def _load_script_fn(script_path: Path) -> Optional[Any]:
    """加载脚本的 run 函数。"""
    module = _load_script_module(script_path)
    if module is None:
        return None
    return getattr(module, "run", None)


def _build_script_tools(skill: SkillDefinition) -> List[Dict[str, Any]]:
    """从 scripts 构建 tool schema。"""
    tools = []
    for name, path in skill.scripts.items():
        module = _load_script_module(path)
        if module is None:
            continue
        schema = getattr(module, "TOOL_SCHEMA", None)
        if isinstance(schema, dict):
            tools.append(schema)
        elif hasattr(module, "run"):
            tools.append({"type": "function", "function": {
                "name": name,
                "description": (getattr(module, "__doc__", None) or f"执行 {name}").strip(),
                "parameters": {"type": "object", "properties": {"input": {"type": "string", "description": "输入"}}}
            }})
    return tools


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1.5 token/字，英文约 0.75 token/word）。"""
    return max(len(text) // 2, len(text.split()) * 2)


def _truncate_messages(messages: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, Any]]:
    """保留 system + 最近的消息，确保不超过 token 预算。"""
    if not messages:
        return messages
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    system_tokens = sum(_estimate_tokens(m.get("content", "")) for m in system_msgs)
    budget = max_tokens - system_tokens
    if budget <= 0:
        return system_msgs

    # 从后往前保留消息
    kept: List[Dict[str, Any]] = []
    used = 0
    for msg in reversed(other_msgs):
        msg_tokens = _estimate_tokens(str(msg.get("content", "")))
        if used + msg_tokens > budget:
            break
        kept.append(msg)
        used += msg_tokens
    kept.reverse()
    return system_msgs + kept


# ====== Agent Loop ======


async def run_agent_loop(
    skill: SkillDefinition, task: str, ctx: Any, config: SkillLoaderConfig,
    chat_context: str = "", stream_id: str = "", plugin_dir: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
    grant_store: Optional[SkillCapGrantStore] = None,
) -> str:
    """执行 agent 模式 skill。"""
    model = skill.model or config.default_model
    max_turns = skill.max_turns or config.default_max_turns
    max_tokens = config.agent_max_context_tokens
    cap_cfg = config.capabilities
    read_extra_roots: List[Path] = [skill.skill_path]
    if skills_dir is not None:
        read_extra_roots.append(skills_dir)

    # 构建 tools = scripts + allowed capabilities
    tools = _build_script_tools(skill)
    allowed_caps = get_allowed_caps(skill, cap_cfg, grant_store)
    denied_caps = [c for c in skill.capabilities if c not in allowed_caps]
    for cap in allowed_caps:
        if cap in CAPABILITY_SCHEMAS:
            tools.append(CAPABILITY_SCHEMAS[cap])
    cap_names = set(allowed_caps)

    # 加载脚本函数
    script_fns: Dict[str, Any] = {}
    for sname, spath in skill.scripts.items():
        fn = _load_script_fn(spath)
        if fn:
            script_fns[sname] = fn

    # System prompt + 权限提示 + 资源目录提示
    system_content = skill.instructions

    # 输出格式要求（结果会直接发送到聊天平台，不支持 markdown）
    system_content += "\n\n[重要：输出格式] 你的回复将直接发送到 QQ 等聊天平台，这些平台不支持 markdown 渲染。严禁使用任何 markdown 语法，包括但不限于：## 标题、**加粗**、*斜体*、```代码块```、- 列表。请用纯文本、换行和空格缩进来组织内容。"

    # 告知 agent 可用的资源目录（progressive disclosure）
    resource_hints = []
    if skill.references_dir:
        refs = [f.name for f in skill.references_dir.iterdir() if f.is_file()]
        if refs:
            resource_hints.append(f"参考文档目录 (references/): {', '.join(refs)}")
    if skill.assets_dir:
        assets = [f.name for f in skill.assets_dir.iterdir() if f.is_file()]
        if assets:
            resource_hints.append(f"资源文件目录 (assets/): {', '.join(assets)}")
    if resource_hints:
        system_content += "\n\n[可用资源]\n" + "\n".join(resource_hints)
        system_content += f"\n使用 read 工具读取，路径前缀: {skill.skill_path}/"

    if denied_caps:
        system_content += f"\n\n[系统提示] 以下能力因权限未开启不可用: {', '.join(denied_caps)}。请在不使用它们的前提下完成任务。"

    # 构建 user message：聊天上下文 + 任务
    user_content = task
    if chat_context:
        user_content = f"[最近的聊天记录，供你了解对话背景]\n{chat_context}\n\n[用户当前的需求]\n{task}"

    # 多轮会话：尝试恢复已有 session
    messages: List[Dict[str, Any]] = []
    if config.session_enabled and stream_id:
        existing = _session_store.get(stream_id, skill.name, config.session_ttl_seconds)
        if existing:
            # 延续已有会话，追加新的 user message
            messages = existing.copy()
            messages.append({"role": "user", "content": user_content})
        # 顺便清理过期会话
        _session_store.cleanup_expired(config.session_ttl_seconds)

    if not messages:
        # 新建会话
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    response_text = ""
    final_result = ""
    for turn in range(max_turns):
        # Token 截断
        messages = _truncate_messages(messages, max_tokens)

        try:
            if tools:
                result = await ctx.llm.generate_with_tools(prompt=messages, tools=tools, model=model)
            else:
                result = await ctx.llm.generate(prompt=messages, model=model)
        except Exception as e:
            final_result = f"Agent LLM 调用失败: {e}"
            break

        if not result.get("success", False):
            error = result.get("error", "未知错误")
            if turn > 0 and response_text:
                final_result = response_text + f"\n\n[Agent 在第 {turn+1} 轮遇到错误: {error}]"
            else:
                final_result = f"Agent 调用失败: {error}"
            break

        response_text = result.get("response", "")
        tool_calls = result.get("tool_calls", [])

        if not tool_calls:
            final_result = response_text
            break

        messages.append({"role": "assistant", "content": response_text, "tool_calls": [
            {"id": tc.get("id", ""), "type": "function", "function": tc.get("function", {})}
            for tc in tool_calls
        ]})

        for tc in tool_calls:
            fn_name = tc.get("function", {}).get("name", "")
            fn_args_raw = tc.get("function", {}).get("arguments", "{}")
            tc_id = tc.get("id", "")
            try:
                fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else (fn_args_raw or {})
            except (json.JSONDecodeError, TypeError):
                fn_args = {}

            # 分发：capability 还是 script
            if fn_name in cap_names:
                tool_result = await run_capability(
                    fn_name,
                    fn_args,
                    cap_cfg,
                    ctx=ctx,
                    stream_id=stream_id,
                    plugin_dir=plugin_dir,
                    read_extra_roots=read_extra_roots,
                )
            elif fn_name in script_fns:
                try:
                    fn = script_fns[fn_name]
                    if asyncio.iscoroutinefunction(fn):
                        tool_result = str(await fn(**fn_args))
                    else:
                        tool_result = str(await asyncio.to_thread(fn, **fn_args))
                except Exception as e:
                    tool_result = f"脚本执行错误: {e}"
            else:
                tool_result = f"未知工具: {fn_name}"

            # 截断过长的 tool 结果
            if len(tool_result) > 10000:
                tool_result = tool_result[:10000] + "\n... (结果已截断)"

            messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result})
    else:
        final_result = response_text or f"Agent 达到最大轮数 ({max_turns})"

    # 保存多轮会话
    if config.session_enabled and stream_id:
        # 把最终 assistant 回复也加入 messages（如果还没加）
        if final_result and (not messages or messages[-1].get("role") != "assistant"):
            messages.append({"role": "assistant", "content": final_result})
        _session_store.save(stream_id, skill.name, messages, config.session_max_history)

    return final_result


async def run_direct_skill(skill: SkillDefinition, task: str) -> str:
    """执行 direct 模式 skill。"""
    if not skill.scripts:
        return f"Skill '{skill.name}' 没有可执行脚本"
    entry = list(skill.scripts.values())[0]
    fn = _load_script_fn(entry)
    if fn is None:
        return f"无法加载脚本: {entry.name}"
    try:
        if asyncio.iscoroutinefunction(fn):
            return str(await fn(task))
        return str(await asyncio.to_thread(fn, task))
    except Exception as e:
        return f"执行失败: {e}\n{traceback.format_exc()}"


def _strip_markdown(text: str) -> str:
    """移除常见 markdown 标记，保留纯文本内容。"""
    import re as _re
    # 代码块 → 保留内容
    text = _re.sub(r'```\w*\n?', '', text)
    # 标题 → 保留文字
    text = _re.sub(r'^#{1,6}\s+', '', text, flags=_re.MULTILINE)
    # 加粗/斜体
    text = _re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = _re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)
    # 链接 [text](url) → text
    text = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # 行内代码
    text = _re.sub(r'`([^`]+)`', r'\1', text)
    return text.strip()


# ====== 后台任务管理 ======


class TaskManager:
    """管理超时后转入后台的 skill 任务。使用 asyncio.shield 保护原 task。"""

    def __init__(self):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, str] = {}

    def is_running(self, key: str) -> bool:
        t = self._tasks.get(key)
        return t is not None and not t.done()

    def get_result(self, key: str) -> Optional[str]:
        return self._results.pop(key, None)

    def shield_and_track(self, key: str, task: asyncio.Task) -> None:
        """跟踪一个已存在的 task（由 shield 保护的）。"""
        self._tasks[key] = task
        task.add_done_callback(lambda t: self._on_done(key, t))

    def _on_done(self, key: str, task: asyncio.Task) -> None:
        self._tasks.pop(key, None)
        try:
            exc = task.exception()
            if exc:
                self._results[key] = f"后台执行失败: {exc}"
            else:
                self._results[key] = task.result()
        except asyncio.CancelledError:
            self._results[key] = "任务被取消"

    def cleanup(self, max_age: float = 600.0) -> None:
        """清理过期结果（简单实现）。"""
        pass  # v2 暂不实现 TTL，结果取走即删


def _build_task_key(stream_id: str, skill_name: str) -> str:
    """构造后台任务 key，避免不同聊天流的同名 skill 串结果。"""

    normalized_stream_id = str(stream_id or "global").strip() or "global"
    return f"{normalized_stream_id}:{skill_name}"


# ====== 插件主类 ======


class SkillLoaderPlugin(MaiBotPlugin):
    """Skill Loader 插件 — 加载 Agent Skills 并注册为独立 Tool。"""

    config_model = SkillLoaderConfig

    def __init__(self):
        super().__init__()
        self._skills: Dict[str, SkillDefinition] = {}
        self._task_mgr = TaskManager()
        self._cap_grants = SkillCapGrantStore()
        self._last_loaded_skills_dir = ""

    @property
    def config(self) -> SkillLoaderConfig:
        return self._plugin_config_instance or SkillLoaderConfig()

    def _load_skills(self) -> None:
        """扫描并加载 skills。"""
        plugin_dir = Path(__file__).parent
        configured_skills_dir = Path(self.config.skills_dir)
        skills_dir = configured_skills_dir if configured_skills_dir.is_absolute() else plugin_dir / configured_skills_dir
        self._skills = scan_skills(skills_dir)
        self._last_loaded_skills_dir = str(skills_dir)
        self._cap_grants.prune(self._skills.keys())
        _clear_script_cache()
        if self._skills:
            logger.info(f"Skill Loader: 加载了 {len(self._skills)} 个 skill: {list(self._skills.keys())}")
        else:
            logger.warning(f"Skill Loader: 未找到任何 skill (目录: {skills_dir})")

    def set_plugin_config(self, config: Dict[str, Any]) -> None:
        """注入配置后按最新 skills_dir 扫描 skill。"""

        super().set_plugin_config(config)
        self._load_skills()

    def get_components(self) -> List[Dict[str, Any]]:
        """覆写：返回动态 skill tools + 静态 /skill command。"""
        if not self._last_loaded_skills_dir:
            self._load_skills()

        components = []

        # 动态 skill tools
        for skill in self._skills.values():
            params_schema: Dict[str, Any] = {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": f"要 {skill.name} 执行的任务描述",
                    }
                },
                "required": ["task"],
            }
            components.append({
                "name": skill.name,
                "type": "TOOL",
                "metadata": {
                    "handler_name": f"__skill__{skill.name}",  # 不存在的名字，触发 invoke_component 回退
                    "description": skill.description,
                    "parameters_raw": params_schema,
                },
            })

        # 静态 /skill command
        components.append({
            "name": "skill",
            "type": "COMMAND",
            "metadata": {
                "handler_name": "__skill__command",  # 不存在的名字，触发 invoke_component 回退
                "description": "管理 Agent Skills",
                "command_pattern": SKILL_COMMAND_PATTERN,
                "intercept_message_level": 1,
            },
        })

        return components

    async def invoke_component(self, component_name: str, **kwargs) -> Any:
        """覆写：统一分发组件调用。"""
        if component_name == "skill":
            return await self._handle_skill_command(**kwargs)
        elif component_name in self._skills:
            return await self._invoke_skill(component_name, **kwargs)
        return {"name": component_name, "content": f"未知组件: {component_name}"}

    async def _get_chat_context(self, stream_id: str, limit: int = 10) -> str:
        """获取最近的聊天记录作为上下文。"""
        try:
            messages = await self.ctx.message.get_recent(stream_id, limit=limit)
            if not messages:
                return ""
            readable = await self.ctx.message.build_readable(messages)
            if readable:
                return str(readable)
        except Exception as e:
            logger.debug(f"获取聊天上下文失败: {e}")
        return ""

    async def _invoke_skill(self, skill_name: str = "", task: str = "", **kwargs) -> Dict[str, str]:
        """执行 skill。"""
        # invoke_component 传入 component_name，也可能直接被 handler 调用
        name = skill_name or kwargs.get("component_name", "")
        skill = self._skills.get(name)
        if not skill:
            return {"name": name, "content": f"未找到 skill: {name}"}

        stream_id = kwargs.get("stream_id", "")
        task_key = _build_task_key(stream_id, skill.name)
        cap_cfg = self.config.capabilities
        timeout = self.config.timeout_seconds

        # 权限检查
        denied_caps: List[str] = []
        if skill.capabilities:
            allowed = get_allowed_caps(skill, cap_cfg, self._cap_grants)
            denied_caps = [c for c in skill.capabilities if c not in allowed]
            if denied_caps and not allowed and skill.mode == "agent":
                notice = (
                    f"[Skill Loader] {skill.name} 需要以下能力但均未开启: {', '.join(denied_caps)}\n"
                    f"请使用 /skill enable <capability> {skill.name} 开启。"
                )
                if stream_id:
                    await self.ctx.send.text(notice, stream_id)
                return {"name": skill.name, "content": "执行失败：所需能力未开启，已通知用户。"}

        # 部分权限缺失时通知用户
        if denied_caps and stream_id:
            await self.ctx.send.text(
                f"[Skill Loader] {skill.name} 部分能力未开启: {', '.join(denied_caps)}，功能可能受限。",
                stream_id,
            )

        # 检查后台任务结果
        bg_result = self._task_mgr.get_result(task_key)
        if bg_result is not None:
            if stream_id and bg_result:
                await self.ctx.send.text(_strip_markdown(bg_result), stream_id)
                return {"name": skill.name, "content": f"[{skill.name}] 后台任务完成，已将结果直接发送给用户。"}
            return {"name": skill.name, "content": bg_result}
        if self._task_mgr.is_running(task_key):
            return {"name": skill.name, "content": f"{skill.name} 正在后台执行中，请稍后再次调用。"}

        # 执行 skill（带超时 + shield）
        if skill.mode == "direct":
            coro = run_direct_skill(skill, task)
        else:
            # 获取聊天上下文注入给 agent
            chat_context = ""
            if stream_id:
                chat_context = await self._get_chat_context(stream_id)
            skills_dir = Path(self._last_loaded_skills_dir) if self._last_loaded_skills_dir else None
            coro = run_agent_loop(
                skill,
                task,
                self.ctx,
                self.config,
                chat_context=chat_context,
                stream_id=stream_id,
                plugin_dir=Path(__file__).parent,
                skills_dir=skills_dir,
                grant_store=self._cap_grants,
            )

        real_task = asyncio.ensure_future(coro)
        try:
            result = await asyncio.wait_for(asyncio.shield(real_task), timeout=timeout)
            # 直接发送结果给用户，不再走 Maisaka reply 流程
            if stream_id and result:
                await self.ctx.send.text(_strip_markdown(result), stream_id)
                return {"name": skill.name, "content": f"[{skill.name}] 已将结果直接发送给用户。"}
            return {"name": skill.name, "content": result}
        except asyncio.TimeoutError:
            # 超时：shield 保护了 real_task，它继续在后台跑
            self._task_mgr.shield_and_track(task_key, real_task)
            return {
                "name": skill.name,
                "content": f"{skill.name} 执行超时 ({timeout}s)，已转为后台运行。稍后再次调用可获取结果。",
            }
        except Exception as e:
            return {"name": skill.name, "content": f"执行异常: {e}"}

    async def _send_skill_command_reply(self, stream_id: str, message: str) -> str:
        """向聊天流发送 /skill 命令回复。"""
        if stream_id and message:
            await self.ctx.send.text(message, stream_id)
        return message

    async def _handle_skill_command(self, action: str = "list", target: str = "", **kwargs) -> str:
        """处理 /skill 命令。"""
        stream_id = str(kwargs.get("stream_id") or "").strip()
        skill_name = ""
        matched_groups = kwargs.get("matched_groups")
        if isinstance(matched_groups, dict):
            action = str(matched_groups.get("action") or action or "list").strip() or "list"
            target = str(matched_groups.get("target") or target or "").strip()
            skill_name = str(matched_groups.get("skill_name") or "").strip()
        else:
            raw_text = str(kwargs.get("text") or "").strip()
            match = re.match(SKILL_COMMAND_PATTERN, raw_text)
            if match:
                action = str(match.group("action") or "list").strip() or "list"
                target = str(match.group("target") or "").strip()
                skill_name = str(match.group("skill_name") or "").strip()

        user_id = str(kwargs.get("user_id") or "").strip()
        platform = str(kwargs.get("platform") or "").strip()
        admin_only_actions = {"enable", "disable", "reload"}
        if action in admin_only_actions and not _is_admin(
            user_id,
            self.config.capabilities.admin_ids,
            platform,
        ):
            return await self._send_skill_command_reply(
                stream_id,
                "仅管理员可执行此操作，请在配置 capabilities.admin_ids 中设置管理员。",
            )

        if action == "list":
            if not self._skills:
                return await self._send_skill_command_reply(stream_id, "当前没有加载任何 skill。")
            lines = ["已加载的 Skills:"]
            cap_cfg = self.config.capabilities
            for s in self._skills.values():
                effective = get_allowed_caps(s, cap_cfg, self._cap_grants)
                caps = f" [{', '.join(effective)}]" if effective else ""
                lines.append(f"  - {s.name} ({s.mode}): {s.description}{caps}")
            return await self._send_skill_command_reply(stream_id, "\n".join(lines))

        elif action == "caps":
            cfg = self.config.capabilities
            status = {
                "bash": cfg.allow_bash, "read": cfg.allow_read,
                "write": cfg.allow_write, "edit": cfg.allow_edit,
            }
            lines = ["Capabilities 全局状态:"]
            for name, enabled in status.items():
                icon = "ON" if enabled else "OFF"
                lines.append(f"  {name}: {icon}")
            if self._skills:
                lines.append("")
                lines.append("Skill 授权状态:")
                for s in self._skills.values():
                    effective = get_allowed_caps(s, cfg, self._cap_grants)
                    if self._cap_grants.is_configured(s.name):
                        detail = ", ".join(effective) if effective else "无"
                        lines.append(f"  {s.name}: {detail}")
                    else:
                        inherited = ", ".join(_global_allowed_caps(s, cfg)) if _global_allowed_caps(s, cfg) else "无"
                        lines.append(f"  {s.name}: 继承全局 ({inherited})")
            return await self._send_skill_command_reply(stream_id, "\n".join(lines))

        elif action == "enable":
            if skill_name:
                return await self._send_skill_command_reply(
                    stream_id,
                    self._set_skill_cap(skill_name, target, True),
                )
            return await self._send_skill_command_reply(stream_id, self._set_global_cap(target, True))

        elif action == "disable":
            if skill_name:
                return await self._send_skill_command_reply(
                    stream_id,
                    self._set_skill_cap(skill_name, target, False),
                )
            return await self._send_skill_command_reply(stream_id, self._set_global_cap(target, False))

        elif action == "reload":
            self._load_skills()
            return await self._send_skill_command_reply(
                stream_id,
                f"已重新加载，当前 {len(self._skills)} 个 skill: {list(self._skills.keys())}",
            )

        return await self._send_skill_command_reply(
            stream_id,
            f"未知操作: {action}。可用: list, caps, enable, disable, reload",
        )

    def _set_global_cap(self, target: str, enable: bool) -> str:
        """设置全局 capability 开关。"""
        cfg = self.config.capabilities
        action_word = "开启" if enable else "关闭"

        if target == "all":
            for attr in CAPABILITY_CONFIG_ATTRS.values():
                setattr(cfg, attr, enable)
            return f"已{action_word}所有全局 capabilities。"
        if target in CAPABILITY_CONFIG_ATTRS:
            setattr(cfg, CAPABILITY_CONFIG_ATTRS[target], enable)
            return f"已{action_word}全局 {target}。"
        return f"未知 capability: {target}。可用: {', '.join(CAPABILITY_NAMES)}, all"

    def _set_skill_cap(self, skill_name: str, target: str, enable: bool) -> str:
        """设置单个 skill 的 capability 授权。"""
        skill = self._skills.get(skill_name)
        if not skill:
            return f"未找到 skill: {skill_name}。"

        cap_cfg = self.config.capabilities
        action_word = "开启" if enable else "关闭"
        inherited = _global_allowed_caps(skill, cap_cfg)
        grants = self._cap_grants.ensure_initialized(skill.name, inherited)

        if target == "all":
            if enable:
                grants.update(inherited)
            else:
                grants.clear()
            return f"已为 {skill_name} {action_word}全部可用 capabilities。"

        if target not in CAPABILITY_NAMES:
            return f"未知 capability: {target}。可用: {', '.join(CAPABILITY_NAMES)}, all"

        if target not in skill.capabilities:
            return f"Skill {skill_name} 未声明 {target} 能力。"

        if target not in inherited:
            return f"全局 {target} 未开启，无法为 {skill_name} 单独授权。"

        if enable:
            grants.add(target)
        else:
            grants.discard(target)
        return f"已为 {skill_name} {action_word} {target}。"

    async def on_load(self) -> None:
        logger.info(f"Skill Loader 已启动，{len(self._skills)} 个 skill 就绪")

    async def on_unload(self) -> None:
        logger.info("Skill Loader 已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """处理配置热更新。"""
        if scope == "self":
            self._load_skills()
            logger.info("Skill Loader 配置已更新")


def create_plugin() -> SkillLoaderPlugin:
    return SkillLoaderPlugin()
