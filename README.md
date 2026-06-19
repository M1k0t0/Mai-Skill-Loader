# Mai-Skill-Loader

为MaiBot（MaiSaka）提供标准的[Agent Skills](https://agentskills.io)兼容支持，允许独立配置skill调用的LLM以获得更好的效果。agent 运行时的 Bash 和文件读写能力通过 MCP sandbox 执行。

## 快速开始

### 1. 安装插件

把本仓库克隆到 MaiBot 的 `plugins/` 目录：

```bash
cd your-maibot/plugins
git clone https://github.com/CharTyr/Mai-Skill-Loader.git skill_loader
```

重启 MaiBot，插件会自动加载。

### 2. 安装 Skill

**方式一：从 GitHub 下载**

找到你想要的 skill（比如在 [skills.sh](https://skills.sh) 上浏览），把它的文件夹放进 `plugins/skill_loader/skills/` 目录：

```bash
cd plugins/skill_loader/skills
git clone https://github.com/someone/some-skill.git my-skill
# 或者直接复制文件夹进来
```

确保目录结构是这样的：
```
plugins/skill_loader/skills/my-skill/SKILL.md
```

**方式二：用 npx skills 命令**

在 `plugins/skill_loader/skills/` 目录下执行（注意必须在这个目录下）：

```bash
cd plugins/skill_loader/skills
npx skills add https://github.com/vercel-labs/skills --skill find-skills -y
```

这会安装到 `.agents/skills/` 子目录，插件会自动扫描到。

**方式三：手动创建**

直接在 `skills/` 下新建文件夹，写一个 `SKILL.md`：

```
plugins/skill_loader/skills/
└── my-skill/
    └── SKILL.md
```

安装完成后使用 `/skill reload` 让 bot 加载新 skill。

### 3. 使用

安装好的 skill 会自动注册为 bot 的工具。当用户的对话匹配到 skill 的描述时，bot 会自动调用它。

skill 的执行结果会直接发送到聊天中，不需要额外等待。

#### Agent 模式的工作方式

大部分 skill 使用 agent 模式。当 bot 决定调用某个 skill 时，会启动一个独立的 AI agent 来完成任务：

1. bot（Maisaka）收到用户消息，判断需要使用某个 skill
2. 插件启动一个独立 AI，把 skill 的指令和用户的需求交给它
3. 这个 AI 可以多轮调用工具，在独立 MCP sandbox 中执行命令、读写文件、发请求等来完成任务
4. 完成后，结果直接发送到聊天中

举个例子，假设安装了 `code-analyzer` skill：

```
用户: 帮我看看这个项目有多少行代码
Bot:  [自动调用 code-analyzer]

文件统计结果：
总文件数: 42
Python 文件: 28 个，共 5,230 行
配置文件: 8 个
文档: 6 个
```

整个过程对用户来说是透明的 —— 发消息，等结果，就这么简单。

#### 超时处理

如果 skill 执行时间超过配置的超时（默认 60 秒），任务会自动转入后台继续运行。bot 会告诉用户正在处理中，下次用户再提起相关话题时会返回结果。

#### 指定模型

agent 模式的 skill 可以使用独立的模型，不影响 bot 主对话的模型配置。在 SKILL.md 中通过 `metadata.maibot-model` 指定：

```yaml
metadata:
  maibot-model: "deepseek-v4-flash"
```

这里填的是 MaiBot `model_config.toml` 中已配置模型的 `name` 字段值。只能使用主程序模型列表中已有的模型，不能填任意模型名。

这样你可以给简单 skill 用便宜快速的模型，给复杂 skill 用更强的模型。如果不指定，使用系统默认模型。

## 管理命令

在聊天中发送以下命令管理 skill：

| 命令 | 说明 |
|------|------|
| `/skill list` | 查看已加载的所有 skill |
| `/skill caps` | 查看能力权限状态 |
| `/skill enable bash` | 全局开启 bash 能力 |
| `/skill enable bash code-analyzer` | 仅为 code-analyzer 开启 bash |
| `/skill enable all` | 开启所有能力 |
| `/skill disable write code-analyzer` | 关闭 code-analyzer 的 write 能力 |
| `/skill reload` | 重新加载 skill（添加新 skill 后使用） |

## 能力权限

部分 skill 需要特定能力才能工作（比如执行命令、读写文件）。这些运行时能力只作用于每次 skill 调用创建的 MCP sandbox：

| 能力 | 默认 | 说明 |
|------|------|------|
| `bash` | 开启（需管理员审批） | 在 sandbox 中执行 shell 命令 |
| `read` | 开启 | 读取 sandbox 文件 |
| `write` | 关闭 | 写入 sandbox 文件 |
| `edit` | 关闭 | 查找替换 sandbox 文件 |

当 skill 需要的能力未开启时，bot 会直接告诉你需要执行什么命令来开启。

未单独配置的 skill 会继承全局 capability 开关；使用 `/skill enable <cap> <skill>` 后会切换为该 skill 的独立授权模式。

每次 skill 调用创建 sandbox 后，当前 skill 目录会同步到 sandbox 的 `skill_mount_path`（默认 `/workspace/skill`）。agent 可以使用 `read` 工具读取 `references/`、`assets/`、`scripts/` 等资源。

`scripts/` 中声明的工具不会在插件宿主进程中 import 或执行；插件只静态读取 `TOOL_SCHEMA`。配置 `sandbox.endpoint_url` 后，调用时会通过 sandbox 的 `execute_code` 执行已同步到 sandbox 的脚本；未配置 sandbox 时，这些 script tools 不会暴露给 agent。

## 编写自己的 Skill

创建一个文件夹，写一个 `SKILL.md`，就是一个 skill：

```
my-skill/
├── SKILL.md          # 必须：描述和指令
├── scripts/          # 可选：脚本
├── references/       # 可选：参考文档
└── assets/           # 可选：资源文件
```

### SKILL.md 格式

```markdown
---
name: my-skill
description: 简要描述这个 skill 做什么，以及什么时候应该使用它。
allowed-tools: Bash Read
metadata:
  maibot-mode: agent
  maibot-max-turns: "10"
---

这里写给 AI 的详细指令。
告诉它具体怎么完成任务、注意什么、输出什么格式。
```

### 标准字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 小写字母、数字和连字符，需与文件夹名一致 |
| `description` | 是 | 描述功能和触发条件，bot 根据这个决定何时调用 |
| `allowed-tools` | 否 | 需要的能力，如 `Bash Read Write Edit` |
| `license` | 否 | 许可证 |
| `compatibility` | 否 | 环境要求 |
| `metadata` | 否 | 扩展字段 |

### MaiBot 扩展字段（放在 metadata 里）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `maibot-mode` | `agent` | 仅支持 `agent`；`direct` 会在加载时跳过 |
| `maibot-model` | 系统默认 | 指定使用的模型 |
| `maibot-max-turns` | `10` | agent 模式最大对话轮数 |

### 执行模式

**agent 模式**（默认）：skill 的指令会交给一个独立的 AI 来执行，它可以使用 allowed-tools 中声明的能力多轮完成任务。适合复杂任务。

`direct` 模式不再支持。声明 `metadata.maibot-mode: direct` 的 skill 会在加载阶段跳过。

## 兼容性

本插件完全兼容 [Agent Skills 规范](https://agentskills.io/specification)。从 VS Code Copilot、Claude Code、Codex 等平台获取的标准 skill 可以直接使用，无需任何修改。

## 配置

插件配置通过 MaiBot 的插件配置系统管理，可在 WebUI 中修改：

- 默认模型、最大轮数、超时时间
- MCP sandbox endpoint、transport、工作目录和请求超时
- 各项 sandbox runtime capability 开关
- skill 目录同步路径和大小上限

`sandbox.endpoint_url` 可以配置为 Streamable HTTP 端点（例如 `http://localhost:18080/mcp`）或 SSE 端点。`sandbox.transport` 默认 `auto`，会对 `/mcp` 自动使用 `streamable_http`，其他路径默认使用 `sse`。

`sandbox.cleanup_policy` 控制 sandbox 容器的自动清理时机：

| 值 | 说明 |
|------|------|
| `single_turn` | 每次 skill 调用结束后销毁容器（默认） |
| `session` | 按 `sandbox.reuse_scope` 复用容器，TTL 过期时销毁 |
| `never` | 插件不自动销毁容器，只关闭 MCP 连接 |

`sandbox.reuse_scope` 仅在 `cleanup_policy = "session"` 时生效：

| 值 | 说明 |
|------|------|
| `skill` | 同一 `stream_id + skill` 复用容器（默认） |
| `stream` | 同一 `stream_id` 下多个 skill 共享容器，适合交替使用多个 skill |

复用容器每次命中都会刷新 TTL，TTL 使用 `session_ttl_seconds`。
过期容器会在下一次 skill 调用或入站消息预处理 Hook (`chat.receive.before_process`) 触发时清理。

## 许可证

MIT
