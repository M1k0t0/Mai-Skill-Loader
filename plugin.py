"""Skill Loader v2 — Agent Skills 加载器

架构：
- 覆写 get_components() 动态返回 skill tools
- 覆写 invoke_component() 统一分发 skill 调用
- Agent loop 带 token budget 和 context 截断
- Capabilities 通过 MCP sandbox 执行
- 多轮会话缓存（stream_id + skill_name 维度）
- 聊天上下文注入
- /skill reload 热加载
- /skill enable|disable 运行时开关
"""

from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

import asyncio
import ast
import base64
import json
import logging
import re
import shlex
import time

import yaml

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase

logger = logging.getLogger("skill_loader")

# ====== 配置 ======


class CapabilitiesConfig(PluginConfigBase):
    """能力权限配置。"""
    __ui_label__ = "能力权限"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    allow_bash: bool = Field(default=True, description="允许在 sandbox 中执行 shell 命令")
    allow_read: bool = Field(default=True, description="允许读取 sandbox 文件")
    allow_write: bool = Field(default=False, description="允许写入 sandbox 文件")
    allow_edit: bool = Field(default=False, description="允许编辑 sandbox 文件（查找替换）")
    write_max_size_kb: int = Field(default=1024, description="写入 sandbox 文件最大 KB")
    bash_require_approval: bool = Field(default=True, description="bash 命令是否需要管理员审批")
    bash_approval_timeout: int = Field(default=120, description="审批等待超时（秒）")
    admin_ids: List[str] = Field(
        default_factory=list,
        description="管理员 ID 列表（格式: 'qq:123456' 或纯数字 QQ 号）",
    )


class SandboxConfig(PluginConfigBase):
    """MCP sandbox 连接配置。"""
    __ui_label__ = "Sandbox"
    __ui_icon__ = "terminal"
    __ui_order__ = 2

    endpoint_url: str = Field(default="", description="MCP HTTP endpoint URL")
    transport: str = Field(default="auto", description="MCP transport: auto, sse, streamable_http")
    workdir: str = Field(default="/workspace", description="sandbox 内默认工作目录")
    skill_mount_path: str = Field(default="/workspace/skill", description="当前 skill 同步到 sandbox 内的路径")
    skill_sync_max_size_kb: int = Field(default=4096, description="同步单个 skill 目录最大 KB")
    cleanup_policy: str = Field(
        default="single_turn",
        description="sandbox 容器自动清理策略: single_turn(单轮), session(会话), never(不自动清理)",
    )
    reuse_scope: str = Field(
        default="skill",
        description="session 清理策略下的 sandbox 复用范围: skill(同一 skill), stream(同一聊天流)",
    )
    request_timeout: float = Field(default=30.0, description="MCP 请求超时（秒）")
    sse_read_timeout: float = Field(default=300.0, description="SSE/streamable HTTP 读取超时（秒）")


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
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


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
    if mode == "direct":
        logger.warning(f"Skill '{name}' 使用 direct 模式，sandbox 版本不再支持，已跳过")
        return None
    if mode != "agent":
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
    "bash": {"type": "function", "function": {"name": "bash", "description": "在 MCP sandbox 中执行 shell 命令", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "shell 命令"}}, "required": ["command"]}}},
    "read": {"type": "function", "function": {"name": "read", "description": "读取 MCP sandbox 内文件内容", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "sandbox 内文件路径"}, "max_lines": {"type": "integer", "description": "最大行数，默认200"}}, "required": ["path"]}}},
    "write": {"type": "function", "function": {"name": "write", "description": "创建或覆盖 MCP sandbox 内文件", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "sandbox 内文件路径"}, "content": {"type": "string", "description": "文件内容"}}, "required": ["path", "content"]}}},
    "edit": {"type": "function", "function": {"name": "edit", "description": "编辑 MCP sandbox 内文件：查找并替换指定内容", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "sandbox 内文件路径"}, "old_str": {"type": "string", "description": "要查找的原始文本"}, "new_str": {"type": "string", "description": "替换为的新文本"}}, "required": ["path", "old_str", "new_str"]}}},
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


def _coerce_max_lines(raw: Any, default: int = 200) -> int:
    """将 read.max_lines 参数安全转换为正整数。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _is_path_allowed(fp: Path, allowed_roots: List[Path]) -> bool:
    """检查路径是否在白名单根目录下。"""
    if not allowed_roots:
        return False
    return any(fp == root or root in fp.parents for root in allowed_roots)


class SandboxMCPError(RuntimeError):
    """MCP sandbox 执行错误。"""


def _mcp_structured_content(result: Any) -> Any:
    for attr in ("structuredContent", "structured_content"):
        value = getattr(result, attr, None)
        if value is not None:
            return value
    if isinstance(result, dict):
        return result.get("structuredContent") or result.get("structured_content")
    return None


def _mcp_is_error(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error"))
    return bool(getattr(result, "isError", False) or getattr(result, "is_error", False))


def _mcp_text_content(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
    else:
        content = getattr(result, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if text is not None:
                parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _parse_mcp_json_result(result: Any) -> Any:
    structured = _mcp_structured_content(result)
    if isinstance(structured, (dict, list)):
        return structured
    text = _mcp_text_content(result).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _format_process_result(payload: Dict[str, Any]) -> str:
    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    exit_code = payload.get("exit_code")
    if exit_code in (0, "0") and not stderr:
        return stdout.rstrip("\n")

    parts: List[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(f"[stderr] {stderr.rstrip()}")
    if exit_code is not None:
        parts.append(f"[exit={exit_code}]")
    return "\n".join(part for part in parts if part)


def _normalize_mcp_result(result: Any, tool_name: str = "") -> str:
    if _mcp_is_error(result):
        text = _mcp_text_content(result)
        return f"MCP 工具返回错误: {text}" if text else "MCP 工具返回错误"

    parsed = _parse_mcp_json_result(result)
    if isinstance(parsed, dict):
        if tool_name == "read_file" and "content" in parsed:
            return str(parsed.get("content") or "")
        if tool_name in {"execute_code", "run_command"} and (
            "stdout" in parsed or "stderr" in parsed or "exit_code" in parsed
        ):
            return _format_process_result(parsed)
        if tool_name == "write_file" and parsed.get("success") is True:
            return "写入成功"
        if tool_name == "destroy_sandbox" and parsed.get("success") is True:
            return "销毁成功"
        return json.dumps(parsed, ensure_ascii=False)
    if isinstance(parsed, list):
        return json.dumps(parsed, ensure_ascii=False)

    text = _mcp_text_content(result)
    if text:
        return text
    structured = _mcp_structured_content(result)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    if result is None:
        return ""
    return str(result)


def _mcp_failure_detail(parsed: Dict[str, Any]) -> str:
    for key in ("error", "message", "detail"):
        value = parsed.get(key)
        if value:
            return str(value)
    return json.dumps(parsed, ensure_ascii=False)


def _is_zero_exit_code(value: Any) -> bool:
    if value in (0, "0", None):
        return True
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False


def _raise_for_mcp_failure(result: Any, tool_name: str, fail_on_process_error: bool = False) -> None:
    """Raise for MCP-level errors and optionally failed process results."""
    if _mcp_is_error(result):
        text = _mcp_text_content(result)
        detail = f": {text}" if text else ""
        raise SandboxMCPError(f"MCP 工具 {tool_name} 返回错误{detail}")

    parsed = _parse_mcp_json_result(result)
    if not isinstance(parsed, dict):
        return

    if tool_name in {"write_file", "destroy_sandbox"}:
        if parsed.get("success") is False:
            raise SandboxMCPError(f"MCP 工具 {tool_name} 执行失败: {_mcp_failure_detail(parsed)}")
        if parsed.get("success") is not True and any(key in parsed for key in ("error", "message", "detail")):
            raise SandboxMCPError(f"MCP 工具 {tool_name} 执行失败: {_mcp_failure_detail(parsed)}")

    if fail_on_process_error and tool_name in {"execute_code", "run_command"}:
        exit_code = parsed.get("exit_code")
        if not _is_zero_exit_code(exit_code):
            output = _format_process_result(parsed)
            detail = f": {output}" if output else ""
            raise SandboxMCPError(f"MCP 工具 {tool_name} 退出码 {exit_code}{detail}")


def _extract_sandbox_id(result: Any) -> str:
    structured = _mcp_structured_content(result)
    if isinstance(structured, dict):
        for key in ("sandbox_id", "sandboxId", "id"):
            value = structured.get(key)
            if value:
                return str(value)

    text = _mcp_text_content(result).strip()
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("sandbox_id", "sandboxId", "id"):
                value = parsed.get(key)
                if value:
                    return str(value)

    if isinstance(result, dict):
        for key in ("sandbox_id", "sandboxId", "id"):
            value = result.get(key)
            if value:
                return str(value)
    return ""


def _normalize_sandbox_dir(path: str) -> str:
    """校验 sandbox 内目录路径。"""
    path_text = str(path or "").strip()
    if not path_text:
        raise SandboxMCPError("sandbox skill_mount_path 不能为空")
    posix_path = PurePosixPath(path_text)
    if not posix_path.is_absolute():
        raise SandboxMCPError("sandbox skill_mount_path 必须是绝对路径")
    if any(part == ".." for part in posix_path.parts):
        raise SandboxMCPError("sandbox skill_mount_path 不允许包含 '..'")
    if str(posix_path) == "/":
        raise SandboxMCPError("sandbox skill_mount_path 不能是根目录")
    return str(posix_path)


def _join_sandbox_path(root: str, rel_path: str) -> str:
    root_path = PurePosixPath(_normalize_sandbox_dir(root))
    rel = PurePosixPath(rel_path)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise SandboxMCPError("sandbox 相对路径不合法")
    return str(root_path.joinpath(rel))


def _resolve_mcp_transport(endpoint_url: str, transport: str) -> str:
    """Resolve configured MCP transport, auto-detecting Streamable HTTP /mcp URLs."""
    transport_text = str(transport or "auto").strip().lower().replace("-", "_")
    parsed_path = urlparse(str(endpoint_url or "")).path.rstrip("/")
    path_looks_streamable = parsed_path.endswith("/mcp") or parsed_path == "/mcp"

    if transport_text in ("", "auto"):
        return "streamable_http" if path_looks_streamable else "sse"
    if transport_text in ("streamable", "streamable_http", "http"):
        return "streamable_http"
    if transport_text == "sse":
        return "streamable_http" if path_looks_streamable else "sse"
    raise SandboxMCPError(f"不支持的 sandbox transport: {transport}")


SANDBOX_CLEANUP_SINGLE_TURN = "single_turn"
SANDBOX_CLEANUP_SESSION = "session"
SANDBOX_CLEANUP_NEVER = "never"
SANDBOX_REUSE_SKILL = "skill"
SANDBOX_REUSE_STREAM = "stream"


def _normalize_sandbox_cleanup_policy(policy: Any) -> str:
    """Normalize configured sandbox cleanup policy."""
    text = str(policy or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": SANDBOX_CLEANUP_SINGLE_TURN,
        "single": SANDBOX_CLEANUP_SINGLE_TURN,
        "single_turn": SANDBOX_CLEANUP_SINGLE_TURN,
        "turn": SANDBOX_CLEANUP_SINGLE_TURN,
        "per_turn": SANDBOX_CLEANUP_SINGLE_TURN,
        "call": SANDBOX_CLEANUP_SINGLE_TURN,
        "per_call": SANDBOX_CLEANUP_SINGLE_TURN,
        "单轮": SANDBOX_CLEANUP_SINGLE_TURN,
        "session": SANDBOX_CLEANUP_SESSION,
        "会话": SANDBOX_CLEANUP_SESSION,
        "never": SANDBOX_CLEANUP_NEVER,
        "none": SANDBOX_CLEANUP_NEVER,
        "no_auto": SANDBOX_CLEANUP_NEVER,
        "no_auto_cleanup": SANDBOX_CLEANUP_NEVER,
        "disabled": SANDBOX_CLEANUP_NEVER,
        "不自动清理": SANDBOX_CLEANUP_NEVER,
    }
    normalized = aliases.get(text)
    if normalized:
        return normalized
    logger.warning(f"未知 sandbox cleanup_policy: {policy!r}，使用 single_turn")
    return SANDBOX_CLEANUP_SINGLE_TURN


def _normalize_sandbox_reuse_scope(scope: Any) -> str:
    """Normalize configured session sandbox reuse scope."""
    text = str(scope or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": SANDBOX_REUSE_SKILL,
        "skill": SANDBOX_REUSE_SKILL,
        "per_skill": SANDBOX_REUSE_SKILL,
        "skill_name": SANDBOX_REUSE_SKILL,
        "技能": SANDBOX_REUSE_SKILL,
        "stream": SANDBOX_REUSE_STREAM,
        "stream_id": SANDBOX_REUSE_STREAM,
        "chat": SANDBOX_REUSE_STREAM,
        "conversation": SANDBOX_REUSE_STREAM,
        "聊天": SANDBOX_REUSE_STREAM,
        "聊天流": SANDBOX_REUSE_STREAM,
    }
    normalized = aliases.get(text)
    if normalized:
        return normalized
    logger.warning(f"未知 sandbox reuse_scope: {scope!r}，使用 skill")
    return SANDBOX_REUSE_SKILL


def _sandbox_lease_key(stream_id: str, skill_name: str, reuse_scope: str) -> str:
    normalized_stream_id = str(stream_id or "").strip()
    if not normalized_stream_id:
        return ""
    if reuse_scope == SANDBOX_REUSE_STREAM:
        return f"stream:{normalized_stream_id}"
    return f"skill:{normalized_stream_id}:{skill_name}"


def _sandbox_setup_message() -> str:
    return (
        "Sandbox 未配置：此 skill 需要 sandbox runtime capability 或 script tool。\n"
        "请在插件配置中设置 sandbox.endpoint_url，例如 http://localhost:18080/mcp，"
        "确认 sandbox MCP 服务已启动后执行 /skill reload 或重启 bot。"
    )


def _collect_skill_sync_files(skill: SkillDefinition, max_size_kb: int) -> List[Dict[str, str]]:
    """收集当前 skill 下普通文件，跳过 symlink，返回 sandbox 同步载荷。"""
    root = skill.skill_path.resolve()
    max_bytes = max_size_kb * 1024
    total = 0
    files: List[Dict[str, str]] = []

    for path in sorted(root.rglob("*")):
        try:
            if path.is_symlink():
                logger.warning(f"跳过 skill symlink: {path}")
                continue
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not _is_path_allowed(resolved, [root]):
                logger.warning(f"跳过 skill 目录外文件: {path}")
                continue
            rel = resolved.relative_to(root).as_posix()
            if rel.startswith("../") or rel == "..":
                logger.warning(f"跳过非法 skill 相对路径: {path}")
                continue
            data = path.read_bytes()
        except Exception as exc:
            raise SandboxMCPError(f"读取 skill 文件失败 {path}: {exc}") from exc

        total += len(data)
        if total > max_bytes:
            raise SandboxMCPError(f"skill 目录超过同步上限 {max_size_kb}KB")
        files.append({
            "path": rel,
            "content_b64": base64.b64encode(data).decode("ascii"),
        })
    return files


class MCPSandboxClient:
    """HTTP MCP sandbox client."""

    def __init__(self, cfg: SandboxConfig):
        self.cfg = cfg
        self._transport_cm: Any = None
        self._session_cm: Any = None
        self._session: Any = None
        self._transport_entered = False
        self._session_entered = False

    async def __aenter__(self) -> "MCPSandboxClient":
        if not self.cfg.endpoint_url:
            raise SandboxMCPError("未配置 sandbox.endpoint_url")
        transport = _resolve_mcp_transport(self.cfg.endpoint_url, self.cfg.transport)
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise SandboxMCPError("缺少 mcp 依赖，请在插件依赖中安装 mcp>=1.27,<2") from exc

        try:
            if transport == "sse":
                self._transport_cm = sse_client(
                    self.cfg.endpoint_url,
                    timeout=self.cfg.request_timeout,
                    sse_read_timeout=self.cfg.sse_read_timeout,
                )
            elif transport == "streamable_http":
                self._transport_cm = streamablehttp_client(
                    self.cfg.endpoint_url,
                    timeout=self.cfg.request_timeout,
                    sse_read_timeout=self.cfg.sse_read_timeout,
                )
            else:
                raise SandboxMCPError(f"不支持的 sandbox transport: {self.cfg.transport}")

            transport_result = await self._transport_cm.__aenter__()
            read_stream, write_stream = transport_result[0], transport_result[1]
            self._transport_entered = True
            self._session_cm = ClientSession(read_stream, write_stream)
            self._session = await self._session_cm.__aenter__()
            self._session_entered = True
            await asyncio.wait_for(self._session.initialize(), timeout=self.cfg.request_timeout)
            return self
        except Exception as exc:
            await self.__aexit__(None, None, None)
            raise SandboxMCPError(f"连接 sandbox MCP 失败: {exc}") from exc

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._session_cm is not None and self._session_entered:
                await self._session_cm.__aexit__(exc_type, exc, tb)
        finally:
            self._session_entered = False
            if self._transport_cm is not None and self._transport_entered:
                await self._transport_cm.__aexit__(exc_type, exc, tb)
            self._transport_entered = False

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if self._session is None:
            raise SandboxMCPError("MCP session 尚未初始化")
        try:
            return await asyncio.wait_for(
                self._session.call_tool(name, arguments),
                timeout=self.cfg.request_timeout,
            )
        except Exception as exc:
            raise SandboxMCPError(f"MCP 工具 {name} 调用失败: {exc}") from exc


class SandboxRuntime:
    """Sandbox lifecycle wrapper."""

    def __init__(
        self,
        cfg: SandboxConfig,
        client: Optional[Any] = None,
        *,
        sandbox_id: str = "",
        destroy_on_exit: bool = True,
    ):
        self.cfg = cfg
        self._client = client or MCPSandboxClient(cfg)
        self.sandbox_id = str(sandbox_id or "")
        self.destroy_on_exit = destroy_on_exit

    async def __aenter__(self) -> "SandboxRuntime":
        try:
            self._client = await self._client.__aenter__()
            if self.sandbox_id:
                return self
            result = await self._client.call_tool("create_sandbox", {})
            if _mcp_is_error(result):
                text = _mcp_text_content(result)
                detail = f": {text}" if text else ""
                raise SandboxMCPError(f"create_sandbox 返回错误{detail}")
            sandbox_id = _extract_sandbox_id(result)
            if not sandbox_id:
                raise SandboxMCPError("create_sandbox 未返回 sandbox id")
            self.sandbox_id = sandbox_id
            return self
        except Exception:
            await self._client.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self.destroy_on_exit:
                await self.destroy()
        finally:
            await self._client.__aexit__(exc_type, exc, tb)

    async def destroy(self) -> None:
        if not self.sandbox_id:
            return
        sandbox_id = self.sandbox_id
        result = await self._client.call_tool("destroy_sandbox", {"sandbox_id": sandbox_id})
        _raise_for_mcp_failure(result, "destroy_sandbox")
        self.sandbox_id = ""

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        payload = {"sandbox_id": self.sandbox_id, **arguments}
        result = await self._client.call_tool(name, payload)
        return _normalize_mcp_result(result, name)

    async def call_tool_checked(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        fail_on_process_error: bool = False,
    ) -> str:
        payload = {"sandbox_id": self.sandbox_id, **arguments}
        result = await self._client.call_tool(name, payload)
        _raise_for_mcp_failure(result, name, fail_on_process_error=fail_on_process_error)
        return _normalize_mcp_result(result, name)

    def skill_path(self, rel_path: str = "") -> str:
        return _join_sandbox_path(self.cfg.skill_mount_path, rel_path)

    async def sync_skill(self, skill: SkillDefinition) -> str:
        files = _collect_skill_sync_files(skill, self.cfg.skill_sync_max_size_kb)
        target_dir = _normalize_sandbox_dir(self.cfg.skill_mount_path)
        if target_dir == _normalize_sandbox_dir(self.cfg.workdir):
            raise SandboxMCPError("sandbox skill_mount_path 不能等于 workdir")
        payload = {
            "target_dir": target_dir,
            "files": files,
        }
        payload_text = json.dumps(payload, ensure_ascii=False)
        code = "\n".join(
            [
                "import base64, json, pathlib, shutil",
                f"payload = json.loads({json.dumps(payload_text, ensure_ascii=False)})",
                "target = pathlib.PurePosixPath(payload['target_dir'])",
                "if not target.is_absolute() or '..' in target.parts:",
                "    raise ValueError('invalid target_dir')",
                "root = pathlib.Path(str(target))",
                "if root.exists():",
                "    shutil.rmtree(root)",
                "root.mkdir(parents=True, exist_ok=True)",
                "for item in payload['files']:",
                "    rel = pathlib.PurePosixPath(item['path'])",
                "    if rel.is_absolute() or '..' in rel.parts:",
                "        raise ValueError('invalid relative path')",
                "    dest = root.joinpath(*rel.parts)",
                "    dest.parent.mkdir(parents=True, exist_ok=True)",
                "    dest.write_bytes(base64.b64decode(item['content_b64']))",
                "print(f\"synced {len(payload['files'])} files\")",
            ]
        )
        return await self.call_tool_checked(
            "execute_code",
            {"language": "python", "code": code},
            fail_on_process_error=True,
        )


async def run_capability(
    name: str,
    args: Dict[str, Any],
    cfg: CapabilitiesConfig,
    sandbox: Optional[SandboxRuntime],
    ctx: Any = None,
    stream_id: str = "",
) -> str:
    """通过 MCP sandbox 执行单个 capability tool。"""
    if sandbox is None:
        return "Sandbox 未初始化，无法执行 runtime capability。"
    if name == "bash":
        command = str(args.get("command", ""))
        if cfg.bash_require_approval:
            approved = await _wait_admin_approval(command, cfg, ctx, stream_id)
            if not approved:
                return "管理员拒绝执行该命令，或审批超时。"
        workdir = _normalize_sandbox_dir(sandbox.cfg.workdir)
        command = f"cd {shlex.quote(workdir)} && {command}"
        return await sandbox.call_tool(
            "run_command",
            {"command": command},
        )
    elif name == "read":
        try:
            content = await sandbox.call_tool_checked("read_file", {"path": str(args.get("path", ""))})
        except SandboxMCPError as exc:
            return f"读取失败: {exc}"
        lines = content.splitlines()
        max_lines = _coerce_max_lines(args.get("max_lines", 200))
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... (截断，共 {len(lines)} 行)"
        return content
    elif name == "write":
        content = str(args.get("content", ""))
        if len(content.encode()) > cfg.write_max_size_kb * 1024:
            return f"内容超过 {cfg.write_max_size_kb}KB 限制"
        try:
            return await sandbox.call_tool_checked(
                "write_file",
                {"path": str(args.get("path", "")), "content": content},
            )
        except SandboxMCPError as exc:
            return f"写入失败: {exc}"
    elif name == "edit":
        path = str(args.get("path", ""))
        old_str = str(args.get("old_str", ""))
        new_str = str(args.get("new_str", ""))
        if not old_str:
            return "old_str 不能为空"
        try:
            content = await sandbox.call_tool_checked("read_file", {"path": path})
        except SandboxMCPError as exc:
            return f"读取失败: {exc}"
        if old_str not in content:
            return "未找到要替换的内容"
        count = content.count(old_str)
        new_content = content.replace(old_str, new_str)
        if len(new_content.encode()) > cfg.write_max_size_kb * 1024:
            return f"内容超过 {cfg.write_max_size_kb}KB 限制"
        try:
            await sandbox.call_tool_checked("write_file", {"path": path, "content": new_content})
        except SandboxMCPError as exc:
            return f"写入失败: {exc}"
        return f"已替换 {count} 处匹配"
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
        for attr in ("processed_plain_text", "plain_text", "text", "content"):
            value = getattr(msg, attr, None)
            if value:
                return str(value).strip()
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


def _format_chat_context(messages: List[Any]) -> str:
    """将最近消息格式化为可注入 agent 的文本，兼容 dict 与消息对象。"""
    lines: List[str] = []
    for msg in messages:
        text = _extract_message_text(msg)
        if not text:
            continue
        user_id = _extract_message_user_id(msg)
        if not user_id and not isinstance(msg, dict):
            user_id = str(getattr(msg, "user_id", "") or "").strip()
        prefix = f"{user_id}: " if user_id else ""
        lines.append(f"{prefix}{text}")
    return "\n".join(lines)


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


# ====== 多轮会话缓存 ======


class SessionStore:
    """管理 skill 多轮会话的短期缓存。"""

    def __init__(self):
        # key: "stream_id:skill_name" → {"messages": [...], "last_active": timestamp}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def _key(self, stream_id: str, skill_name: str) -> str:
        return f"{stream_id}:{skill_name}"

    def key(self, stream_id: str, skill_name: str) -> str:
        return self._key(stream_id, skill_name)

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

    def cleanup_expired(self, ttl: int) -> List[str]:
        """清理所有过期会话。"""
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v["last_active"] > ttl]
        for k in expired:
            del self._sessions[k]
        return expired


class SandboxLeaseStore:
    """Tracks sandbox ids that are intentionally kept beyond one skill call."""

    def __init__(self) -> None:
        self._leases: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str, *, touch: bool = True) -> str:
        lease = self._leases.get(key)
        if not lease:
            return ""
        if touch:
            lease["last_active"] = time.time()
        return str(lease.get("sandbox_id") or "")

    def save(self, key: str, sandbox_id: str) -> None:
        if key and sandbox_id:
            self._leases[key] = {
                "sandbox_id": sandbox_id,
                "last_active": time.time(),
            }

    def clear(self, key: str) -> None:
        self._leases.pop(key, None)

    def clear_all(self) -> None:
        self._leases.clear()

    def pop_many(self, keys: Iterable[str]) -> List[str]:
        sandbox_ids: List[str] = []
        for key in keys:
            lease = self._leases.pop(key, None)
            sandbox_id = str((lease or {}).get("sandbox_id") or "")
            if sandbox_id:
                sandbox_ids.append(sandbox_id)
        return sandbox_ids

    def cleanup_expired(self, ttl: int, skip_keys: Optional[Set[str]] = None) -> List[str]:
        now = time.time()
        skip = skip_keys or set()
        expired = [
            key
            for key, lease in self._leases.items()
            if key not in skip
            if now - float(lease.get("last_active") or 0) > ttl
        ]
        return self.pop_many(expired)


class SandboxLeaseLockStore:
    """Serializes access to a reused sandbox lease."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def locked_keys(self) -> Set[str]:
        return {key for key, lock in self._locks.items() if lock.locked()}


async def _destroy_sandbox_id(cfg: SandboxConfig, sandbox_id: str, client: Optional[Any] = None) -> None:
    """Destroy a sandbox id using a short-lived MCP client session."""
    if not sandbox_id:
        return
    sandbox_client = client or MCPSandboxClient(cfg)
    active_client: Optional[Any] = None
    try:
        active_client = await sandbox_client.__aenter__()
        result = await active_client.call_tool("destroy_sandbox", {"sandbox_id": sandbox_id})
        _raise_for_mcp_failure(result, "destroy_sandbox")
    finally:
        if active_client is not None:
            await active_client.__aexit__(None, None, None)


async def _destroy_expired_sandbox_leases(
    ttl: int,
    cfg: SandboxConfig,
    client: Optional[Any] = None,
) -> None:
    sandbox_ids = _sandbox_leases.cleanup_expired(ttl, skip_keys=_sandbox_lease_locks.locked_keys())
    for sandbox_id in sandbox_ids:
        try:
            await _destroy_sandbox_id(cfg, sandbox_id, client)
        except Exception as exc:
            logger.warning(f"销毁过期 session sandbox 失败 ({sandbox_id}): {exc}")


# 全局会话存储实例
_session_store = SessionStore()
_sandbox_leases = SandboxLeaseStore()
_sandbox_lease_locks = SandboxLeaseLockStore()

def _clear_script_cache() -> None:
    """兼容旧调用；脚本不再在宿主机 import，因此无需缓存。"""


def _extract_script_tool_schema(script_path: Path) -> Optional[Dict[str, Any]]:
    """静态读取脚本 TOOL_SCHEMA，避免在宿主机执行 skill 代码。"""
    try:
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script_path))
    except Exception as exc:
        logger.warning(f"解析脚本失败 {script_path}: {exc}")
        return None

    has_run = False
    schema: Optional[Dict[str, Any]] = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            has_run = True

        schema_node: Optional[ast.AST] = None
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "TOOL_SCHEMA" for target in node.targets):
                schema_node = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "TOOL_SCHEMA" and node.value is not None:
                schema_node = node.value

        if schema_node is None:
            continue
        try:
            schema_value = ast.literal_eval(schema_node)
        except Exception as exc:
            logger.warning(f"脚本 TOOL_SCHEMA 不是静态字面量 {script_path}: {exc}")
            return None
        if isinstance(schema_value, dict):
            schema = schema_value

    if not has_run:
        return None
    if schema is not None:
        return schema
    return {"type": "function", "function": {
        "name": script_path.stem,
        "description": f"执行 {script_path.stem}",
        "parameters": {"type": "object", "properties": {"input": {"type": "string", "description": "输入"}}}
    }}


def _build_script_tools(skill: SkillDefinition) -> List[Dict[str, Any]]:
    """从 scripts 构建 tool schema。"""
    tools = []
    for path in skill.scripts.values():
        schema = _extract_script_tool_schema(path)
        if isinstance(schema, dict):
            tools.append(schema)
    return tools


async def run_script_tool_in_sandbox(
    skill: SkillDefinition,
    script_path: Path,
    args: Dict[str, Any],
    sandbox: Optional[SandboxRuntime],
) -> str:
    """在 MCP sandbox 中执行 skill script 的 run 函数。"""
    if sandbox is None:
        return "Sandbox 未初始化，无法执行 script tool。"

    root = skill.skill_path.resolve()
    resolved = script_path.resolve()
    if not _is_path_allowed(resolved, [root]):
        return "安全策略阻止: script 不在当前 skill 目录中"
    if script_path.is_symlink():
        return "安全策略阻止: 不允许执行符号链接 script"
    rel_path = resolved.relative_to(root).as_posix()
    sandbox_script_path = sandbox.skill_path(rel_path)

    args_json = json.dumps(args, ensure_ascii=False)
    wrapper = "\n".join(
        [
            "import asyncio, inspect, json, os, runpy, sys",
            f"__script_path = {json.dumps(sandbox_script_path, ensure_ascii=False)}",
            f"__args = json.loads({json.dumps(args_json, ensure_ascii=False)})",
            "__script_dir = os.path.dirname(__script_path)",
            "sys.path.insert(0, __script_dir)",
            "os.chdir(__script_dir)",
            "__globals = runpy.run_path(__script_path)",
            "run = __globals.get('run')",
            "if run is None:",
            "    raise RuntimeError('script has no run function')",
            "__sig = inspect.signature(run)",
            "try:",
            "    __sig.bind(**__args)",
            "    __call_mode = 'kwargs'",
            "except TypeError as __bind_error:",
            "    if set(__args.keys()) == {'input'}:",
            "        try:",
            "            __sig.bind(__args['input'])",
            "            __call_mode = 'input_positional'",
            "        except TypeError:",
            "            raise __bind_error",
            "    else:",
            "        raise",
            "if __call_mode == 'input_positional':",
            "    __result = run(__args['input'])",
            "else:",
            "    __result = run(**__args)",
            "if inspect.isawaitable(__result):",
            "    __result = asyncio.run(__result)",
            "print(str(__result))",
        ]
    )
    return await sandbox.call_tool(
        "execute_code",
        {"language": "python", "code": wrapper},
    )


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


def _is_missing_model_error(error: Any) -> bool:
    text = str(error or "")
    return "未找到名为" in text and "模型配置" in text


async def _read_llm_model_listing(ctx: Any) -> List[str]:
    llm = getattr(ctx, "llm", None)
    if llm is None:
        return []
    if not hasattr(llm, "get_available_models"):
        logger.warning("MaiBot LLM capability 缺少 get_available_models()，无法读取模型列表")
        return []
    try:
        models = await llm.get_available_models()
    except Exception as exc:
        logger.warning(f"读取 MaiBot LLM 可用模型列表失败: {exc}")
        return []
    if not isinstance(models, list):
        logger.warning(f"MaiBot LLM get_available_models() 返回类型异常: {type(models).__name__}")
        return []
    seen: set[str] = set()
    return [str(name) for name in models if str(name) and not (str(name) in seen or seen.add(str(name)))]


async def _log_llm_model_list(ctx: Any, requested_model: str, error: Any) -> None:
    names = await _read_llm_model_listing(ctx)
    if names:
        message = (
            f"MaiBot LLM 模型查找失败: requested={requested_model!r} "
            f"error={error} available_models={', '.join(names)}"
        )
    else:
        message = (
            f"MaiBot LLM 模型查找失败: requested={requested_model!r} "
            f"error={error}; available_models=<unavailable>"
        )
    logger.warning(message)
    ctx_logger = getattr(ctx, "logger", None)
    if ctx_logger is not None and ctx_logger is not logger:
        try:
            ctx_logger.warning(message)
        except Exception as exc:
            logger.debug(f"写入 ctx.logger 失败: {exc}")


async def _log_selected_llm_model(ctx: Any, skill_name: str, model: str) -> None:
    if not model:
        return
    names = await _read_llm_model_listing(ctx)
    if names:
        message = (
            f"MaiBot LLM 模型选择: skill={skill_name} requested={model!r} "
            f"available_models={', '.join(names)}"
        )
    else:
        message = f"MaiBot LLM 模型选择: skill={skill_name} requested={model!r} available_models=<unavailable>"
    logger.warning(message)
    ctx_logger = getattr(ctx, "logger", None)
    if ctx_logger is not None and ctx_logger is not logger:
        try:
            ctx_logger.warning(message)
        except Exception as exc:
            logger.debug(f"写入 ctx.logger 失败: {exc}")


async def _generate_with_model_diagnostics(
    ctx: Any,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    model: str,
) -> Dict[str, Any]:
    async def call(selected_model: str) -> Dict[str, Any]:
        if tools:
            return await ctx.llm.generate_with_tools(prompt=messages, tools=tools, model=selected_model)
        return await ctx.llm.generate(prompt=messages, model=selected_model)

    try:
        result = await call(model)
    except Exception as exc:
        if model and _is_missing_model_error(exc):
            await _log_llm_model_list(ctx, model, exc)
        raise

    if model and not result.get("success", False) and _is_missing_model_error(result.get("error", "")):
        await _log_llm_model_list(ctx, model, result.get("error", ""))
    return result


# ====== Agent Loop ======


async def run_agent_loop(
    skill: SkillDefinition, task: str, ctx: Any, config: SkillLoaderConfig,
    chat_context: str = "", stream_id: str = "", plugin_dir: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
    grant_store: Optional[SkillCapGrantStore] = None,
    sandbox_client: Optional[Any] = None,
) -> str:
    """执行 agent 模式 skill。"""
    model = skill.model or config.default_model
    max_turns = skill.max_turns or config.default_max_turns
    max_tokens = config.agent_max_context_tokens
    cap_cfg = config.capabilities
    sandbox_cleanup_policy = _normalize_sandbox_cleanup_policy(config.sandbox.cleanup_policy)
    sandbox_reuse_scope = _normalize_sandbox_reuse_scope(config.sandbox.reuse_scope)
    sandbox_lease_key = (
        _sandbox_lease_key(stream_id, skill.name, sandbox_reuse_scope)
        if config.session_enabled and stream_id
        else ""
    )

    allowed_caps = get_allowed_caps(skill, cap_cfg, grant_store)
    denied_caps = [c for c in skill.capabilities if c not in allowed_caps]
    sandbox_available = sandbox_client is not None or bool(str(config.sandbox.endpoint_url or "").strip())

    # 构建 tools = scripts + allowed sandbox capabilities
    script_paths: Dict[str, Path] = dict(skill.scripts) if sandbox_available else {}
    tools = _build_script_tools(skill) if script_paths else []
    for cap in allowed_caps:
        if cap in CAPABILITY_SCHEMAS:
            tools.append(CAPABILITY_SCHEMAS[cap])
    cap_names = set(allowed_caps)

    # script tools 也在 sandbox 中执行，宿主机只保留脚本路径。
    needs_sandbox = bool(cap_names or script_paths)

    if cap_names and not sandbox_available:
        return _sandbox_setup_message()

    await _log_selected_llm_model(ctx, skill.name, model)

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
        system_content += (
            f"\n当前 skill 目录已同步到 sandbox: {config.sandbox.skill_mount_path}"
            f"\n使用 read 工具读取这些资源，例如 {config.sandbox.skill_mount_path}/references/<file>。"
        )

    if denied_caps:
        system_content += f"\n\n[系统提示] 以下能力因权限未开启不可用: {', '.join(denied_caps)}。请在不使用它们的前提下完成任务。"
    if skill.scripts and not sandbox_available:
        system_content += "\n\n[系统提示] sandbox 未配置，scripts/ 工具不可用。请直接完成任务，不要尝试调用脚本工具。"

    # 构建 user message：聊天上下文 + 任务
    user_content = task
    if chat_context:
        user_content = f"[最近的聊天记录，供你了解对话背景]\n{chat_context}\n\n[用户当前的需求]\n{task}"

    # 多轮会话：尝试恢复已有 session
    messages: List[Dict[str, Any]] = []
    if config.session_enabled and stream_id:
        _session_store.cleanup_expired(config.session_ttl_seconds)
        if sandbox_cleanup_policy == SANDBOX_CLEANUP_SESSION:
            await _destroy_expired_sandbox_leases(config.session_ttl_seconds, config.sandbox, sandbox_client)
        existing = _session_store.get(stream_id, skill.name, config.session_ttl_seconds)
        if existing:
            # 延续已有会话，追加新的 user message
            messages = existing.copy()
            messages.append({"role": "user", "content": user_content})

    if not messages:
        # 新建会话
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    response_text = ""
    final_result = ""
    sandbox_cm: Optional[SandboxRuntime] = None
    sandbox: Optional[SandboxRuntime] = None
    sandbox_entered = False
    sandbox_lease_lock: Optional[asyncio.Lock] = None
    sandbox_lease_locked = False
    persistent_session_sandbox = False
    try:
        if needs_sandbox:
            persistent_session_sandbox = sandbox_cleanup_policy == SANDBOX_CLEANUP_SESSION and bool(sandbox_lease_key)
            if persistent_session_sandbox:
                sandbox_lease_lock = _sandbox_lease_locks.get(sandbox_lease_key)
                await sandbox_lease_lock.acquire()
                sandbox_lease_locked = True

            reusable_sandbox_id = _sandbox_leases.get(sandbox_lease_key) if persistent_session_sandbox else ""
            destroy_on_exit = sandbox_cleanup_policy == SANDBOX_CLEANUP_SINGLE_TURN
            if sandbox_cleanup_policy == SANDBOX_CLEANUP_SESSION and not persistent_session_sandbox:
                destroy_on_exit = True
            if persistent_session_sandbox and not reusable_sandbox_id:
                # Keep setup failures from leaking a newly-created session sandbox.
                destroy_on_exit = True

            sandbox_cm = SandboxRuntime(
                config.sandbox,
                sandbox_client,
                sandbox_id=reusable_sandbox_id,
                destroy_on_exit=destroy_on_exit,
            )
            sandbox = await sandbox_cm.__aenter__()
            sandbox_entered = True
            await sandbox.sync_skill(skill)
            if persistent_session_sandbox and sandbox.sandbox_id:
                _sandbox_leases.save(sandbox_lease_key, sandbox.sandbox_id)
                sandbox_cm.destroy_on_exit = False

        for turn in range(max_turns):
            # Token 截断
            messages = _truncate_messages(messages, max_tokens)

            try:
                result = await _generate_with_model_diagnostics(ctx, messages, tools, model)
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

                # 分发：sandbox capability 还是 sandbox script
                if fn_name in cap_names:
                    tool_result = await run_capability(
                        fn_name,
                        fn_args,
                        cap_cfg,
                        sandbox,
                        ctx=ctx,
                        stream_id=stream_id,
                    )
                elif fn_name in script_paths:
                    tool_result = await run_script_tool_in_sandbox(
                        skill,
                        script_paths[fn_name],
                        fn_args,
                        sandbox,
                    )
                else:
                    tool_result = f"未知工具: {fn_name}"

                # 截断过长的 tool 结果
                if len(tool_result) > 10000:
                    tool_result = tool_result[:10000] + "\n... (结果已截断)"

                messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result})
        else:
            final_result = response_text or f"Agent 达到最大轮数 ({max_turns})"
    except SandboxMCPError as e:
        final_result = f"Sandbox 执行失败: {e}"
    finally:
        if sandbox_cm is not None and sandbox_entered:
            try:
                await sandbox_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"清理 sandbox 失败: {e}")
        if persistent_session_sandbox and sandbox_lease_key and sandbox is not None and sandbox.sandbox_id:
            _sandbox_leases.save(sandbox_lease_key, sandbox.sandbox_id)
        if sandbox_lease_locked and sandbox_lease_lock is not None:
            sandbox_lease_lock.release()

    # 保存多轮会话
    if config.session_enabled and stream_id:
        # 把最终 assistant 回复也加入 messages（如果还没加）
        if final_result and (not messages or messages[-1].get("role") != "assistant"):
            messages.append({"role": "assistant", "content": final_result})
        _session_store.save(stream_id, skill.name, messages, config.session_max_history)

    return final_result


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

        components.append({
            "name": "skill_loader_sandbox_janitor",
            "type": "HOOK_HANDLER",
            "metadata": {
                "handler_name": "_handle_sandbox_janitor_hook",
                "description": "入站消息时清理过期 session sandbox",
                "hook": "chat.receive.before_process",
                "mode": "observe",
                "order": "late",
                "timeout_ms": 30000,
                "error_policy": "log",
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

    async def _handle_sandbox_janitor_hook(self, **kwargs) -> Dict[str, str]:
        """Observe hook: opportunistically clean expired session sandboxes."""
        if _normalize_sandbox_cleanup_policy(self.config.sandbox.cleanup_policy) != SANDBOX_CLEANUP_SESSION:
            return {"action": "continue"}
        try:
            await _destroy_expired_sandbox_leases(
                self.config.session_ttl_seconds,
                self.config.sandbox,
            )
        except Exception as exc:
            logger.warning(f"消息预处理清理过期 sandbox 失败: {exc}")
        return {"action": "continue"}

    async def _get_chat_context(self, stream_id: str, limit: int = 10) -> str:
        """获取最近的聊天记录作为上下文。"""
        try:
            messages = await self.ctx.message.get_recent(stream_id, limit=limit)
            if not messages:
                return ""
            local_readable = _format_chat_context(messages)
            if local_readable:
                return local_readable
            if all(not isinstance(message, dict) for message in messages):
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
