"""Skill Loader 核心逻辑测试。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

import pytest


class FakeSandboxClient:
    def __init__(self, files: Optional[dict[str, str]] = None) -> None:
        self.files = files or {}
        self.calls: list[tuple[str, dict]] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "create_sandbox":
            return {"structuredContent": {"sandbox_id": "sbx-1"}}
        if name == "destroy_sandbox":
            return {"content": json.dumps({"success": True})}
        if name == "read_file":
            return {"content": json.dumps({"content": self.files.get(arguments["path"], "")})}
        if name == "write_file":
            self.files[arguments["path"]] = arguments["content"]
            return {"content": json.dumps({"success": True})}
        if name == "run_command":
            return {"content": json.dumps({"exit_code": 0, "stdout": f"ran: {arguments['command']}\n", "stderr": ""})}
        if name == "execute_code":
            return {"content": json.dumps({"exit_code": 0, "stdout": "script-result\n", "stderr": ""})}
        return {"content": ""}


class ErrorReadSandboxClient(FakeSandboxClient):
    async def call_tool(self, name: str, arguments: dict):
        if name == "read_file":
            self.calls.append((name, arguments))
            return {"isError": True, "content": "read denied"}
        return await super().call_tool(name, arguments)


def _make_maibot_ctx(llm, recent_messages: Optional[list[dict]] = None, readable_context: str = ""):
    sent: list[tuple[str, str]] = []

    async def send_text(content: str, stream_id: str) -> None:
        sent.append((content, stream_id))

    return SimpleNamespace(
        llm=llm,
        sent=sent,
        send=SimpleNamespace(text=AsyncMock(side_effect=send_text)),
        message=SimpleNamespace(
            get_recent=AsyncMock(return_value=recent_messages or []),
            build_readable=AsyncMock(return_value=readable_context),
        ),
    )


def _make_skill(plugin_module, name: str, capabilities: list[str]) -> object:
    return plugin_module.SkillDefinition(
        name=name,
        description=f"{name} skill",
        mode="agent",
        model="",
        max_turns=3,
        instructions="test",
        scripts={},
        skill_path=Path("/tmp/skills") / name,
        capabilities=capabilities,
    )


class TestSecurityHelpers:
    def test_is_admin_requires_platform_for_scoped_id(self, plugin_module) -> None:
        assert plugin_module._is_admin("10001", ["qq:10001"], "qq")
        assert not plugin_module._is_admin("10001", ["qq:10001"], "telegram")
        assert not plugin_module._is_admin("10001", ["qq:10001"], "")
        assert plugin_module._is_admin("10001", ["10001"], "")

    def test_extract_message_fields(self, plugin_module) -> None:
        msg = {
            "timestamp": "1710000000.5",
            "platform": "qq",
            "message_info": {"user_info": {"user_id": "10001"}},
            "raw_message": [{"type": "text", "data": "Y"}],
        }
        assert plugin_module._extract_message_timestamp(msg) == 1710000000.5
        assert plugin_module._extract_message_user_id(msg) == "10001"
        assert plugin_module._extract_message_text(msg).upper() == "Y"

    def test_coerce_max_lines(self, plugin_module) -> None:
        assert plugin_module._coerce_max_lines(None) == 200
        assert plugin_module._coerce_max_lines("bad") == 200
        assert plugin_module._coerce_max_lines(50) == 50

    def test_hardcoded_bash_blocklist_removed(self, plugin_module) -> None:
        assert not hasattr(plugin_module.CapabilitiesConfig(), "bash_blocked_commands")


class TestSkillParsing:
    def test_parse_skill_invalid_max_turns(self, plugin_module, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad-turns"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "\n".join(
                [
                    "---",
                    "name: bad-turns",
                    "description: bad turns",
                    "metadata:",
                    "  maibot-max-turns: not-a-number",
                    "---",
                    "instructions",
                ]
            ),
            encoding="utf-8",
        )
        skill = plugin_module.parse_skill(skill_dir)
        assert skill is not None
        assert skill.max_turns == 10

    def test_scan_skills_skips_broken_skill(self, plugin_module, tmp_path: Path) -> None:
        good = tmp_path / "good-skill"
        good.mkdir()
        (good / "SKILL.md").write_text(
            "\n".join(
                [
                    "---",
                    "name: good-skill",
                    "description: ok",
                    "---",
                    "ok",
                ]
            ),
            encoding="utf-8",
        )
        broken = tmp_path / "broken-skill"
        broken.mkdir()
        (broken / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
        result = plugin_module.scan_skills(tmp_path)
        assert list(result.keys()) == ["good-skill"]

    def test_parse_skill_skips_direct_mode(self, plugin_module, tmp_path: Path) -> None:
        skill_dir = tmp_path / "direct-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "\n".join(
                [
                    "---",
                    "name: direct-skill",
                    "description: direct",
                    "metadata:",
                    "  maibot-mode: direct",
                    "---",
                    "instructions",
                ]
            ),
            encoding="utf-8",
        )
        assert plugin_module.parse_skill(skill_dir) is None


class TestPerSkillCapabilities:
    def test_inherit_global_when_not_configured(self, plugin_module) -> None:
        skill = _make_skill(plugin_module, "code-analyzer", ["bash", "read"])
        cap_cfg = plugin_module.CapabilitiesConfig()
        store = plugin_module.SkillCapGrantStore()
        assert plugin_module.get_allowed_caps(skill, cap_cfg, store) == ["bash", "read"]

    def test_per_skill_grant_restricts_capabilities(self, plugin_module) -> None:
        skill = _make_skill(plugin_module, "code-analyzer", ["bash", "read"])
        cap_cfg = plugin_module.CapabilitiesConfig()
        store = plugin_module.SkillCapGrantStore()
        store.ensure_initialized(skill.name, ["bash", "read"])
        store.get_granted(skill.name).discard("bash")
        assert plugin_module.get_allowed_caps(skill, cap_cfg, store) == ["read"]

    def test_set_skill_cap_enable_disable(self, plugin, plugin_module) -> None:
        plugin._skills = {
            "code-analyzer": _make_skill(plugin_module, "code-analyzer", ["bash", "read"]),
        }
        assert plugin._set_skill_cap("code-analyzer", "bash", False).startswith("已为 code-analyzer 关闭")
        assert plugin_module.get_allowed_caps(
            plugin._skills["code-analyzer"],
            plugin.config.capabilities,
            plugin._cap_grants,
        ) == ["read"]
        assert plugin._set_skill_cap("code-analyzer", "bash", True).startswith("已为 code-analyzer 开启")
        assert "bash" in plugin_module.get_allowed_caps(
            plugin._skills["code-analyzer"],
            plugin.config.capabilities,
            plugin._cap_grants,
        )


class TestSkillCommands:
    @pytest.mark.asyncio
    async def test_get_chat_context_formats_dict_messages_without_build_readable(self, plugin) -> None:
        plugin._ctx = SimpleNamespace(
            message=SimpleNamespace(
                get_recent=AsyncMock(
                    return_value=[
                        {
                            "message_info": {"user_info": {"user_id": "10001"}},
                            "raw_message": [{"type": "text", "data": "hello"}],
                        }
                    ]
                ),
                build_readable=AsyncMock(side_effect=AssertionError("should not call build_readable for dicts")),
            )
        )

        result = await plugin._get_chat_context("stream-1")

        assert result == "10001: hello"
        plugin._ctx.message.get_recent.assert_awaited_once_with("stream-1", limit=10)
        plugin._ctx.message.build_readable.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_skill_command_sends_reply(self, plugin) -> None:
        plugin._ctx = SimpleNamespace(send=SimpleNamespace(text=AsyncMock()))
        plugin._skills = {}

        result = await plugin._handle_skill_command(
            matched_groups={"action": "list"},
            stream_id="stream-1",
            user_id="10001",
            platform="qq",
        )

        assert "skill" in result.lower()
        plugin._ctx.send.text.assert_awaited_once_with(result, "stream-1")

    @pytest.mark.asyncio
    async def test_enable_requires_admin(self, plugin) -> None:
        plugin._ctx = SimpleNamespace(send=SimpleNamespace(text=AsyncMock()))

        result = await plugin._handle_skill_command(
            matched_groups={"action": "enable", "target": "bash", "skill_name": "code-analyzer"},
            stream_id="stream-1",
            user_id="99999",
            platform="qq",
        )

        assert "仅管理员" in result

    @pytest.mark.asyncio
    async def test_enable_skill_cap_for_admin(self, plugin, plugin_module) -> None:
        plugin._ctx = SimpleNamespace(send=SimpleNamespace(text=AsyncMock()))
        plugin._skills = {
            "code-analyzer": _make_skill(plugin_module, "code-analyzer", ["bash", "read"]),
        }
        plugin._cap_grants.ensure_initialized("code-analyzer", ["read"])

        result = await plugin._handle_skill_command(
            matched_groups={"action": "enable", "target": "bash", "skill_name": "code-analyzer"},
            stream_id="stream-1",
            user_id="10001",
            platform="qq",
        )

        assert "已为 code-analyzer 开启 bash" in result

    def test_command_pattern_matches_skill_name(self, plugin_module) -> None:
        pattern = re.compile(plugin_module.SKILL_COMMAND_PATTERN)
        match = pattern.search("/skill enable bash code-analyzer")
        assert match is not None
        assert match.group("action") == "enable"
        assert match.group("target") == "bash"
        assert match.group("skill_name") == "code-analyzer"


class TestBashApproval:
    @pytest.mark.asyncio
    async def test_wait_admin_approval_accepts_yes(self, plugin_module) -> None:
        cfg = plugin_module.CapabilitiesConfig(admin_ids=["qq:10001"])
        ctx = SimpleNamespace(
            send=SimpleNamespace(text=AsyncMock()),
            message=SimpleNamespace(get_recent=AsyncMock()),
        )

        async def fake_sleep(_seconds: float) -> None:
            ctx.message.get_recent.return_value = [
                {
                    "timestamp": str(time.time() + 5),
                    "platform": "qq",
                    "message_info": {"user_info": {"user_id": "10001"}},
                    "raw_message": [{"type": "text", "data": "Y"}],
                }
            ]

        with patch.object(asyncio, "sleep", new=fake_sleep):
            approved = await plugin_module._wait_admin_approval("echo hi", cfg, ctx, "stream-1")

        assert approved is True


class TestSandboxCapabilities:
    @pytest.mark.asyncio
    async def test_bash_routes_to_mcp_without_local_blocklist(self, plugin_module) -> None:
        cfg = plugin_module.CapabilitiesConfig(bash_require_approval=False)
        sandbox = SimpleNamespace(
            cfg=plugin_module.SandboxConfig(workdir="/workspace"),
            call_tool=AsyncMock(return_value="ran"),
        )

        result = await plugin_module.run_capability(
            "bash",
            {"command": "rm -rf /"},
            cfg,
            sandbox,
        )

        assert result == "ran"
        sandbox.call_tool.assert_awaited_once_with(
            "run_command",
            {"command": "cd /workspace && rm -rf /"},
        )

    @pytest.mark.asyncio
    async def test_edit_reads_and_writes_sandbox_file(self, plugin_module) -> None:
        cfg = plugin_module.CapabilitiesConfig(write_max_size_kb=1)
        sandbox = SimpleNamespace(
            cfg=plugin_module.SandboxConfig(workdir="/workspace"),
            call_tool_checked=AsyncMock(side_effect=["hello old", "written"]),
        )

        result = await plugin_module.run_capability(
            "edit",
            {"path": "/workspace/a.txt", "old_str": "old", "new_str": "new"},
            cfg,
            sandbox,
        )

        assert result == "已替换 1 处匹配"
        assert sandbox.call_tool_checked.await_args_list[0].args == (
            "read_file",
            {"path": "/workspace/a.txt"},
        )
        assert sandbox.call_tool_checked.await_args_list[1].args == (
            "write_file",
            {"path": "/workspace/a.txt", "content": "hello new"},
        )

    @pytest.mark.asyncio
    async def test_edit_reports_failed_sandbox_write(self, plugin_module) -> None:
        cfg = plugin_module.CapabilitiesConfig(write_max_size_kb=1)
        sandbox = SimpleNamespace(
            cfg=plugin_module.SandboxConfig(workdir="/workspace"),
            call_tool_checked=AsyncMock(
                side_effect=[
                    "hello old",
                    plugin_module.SandboxMCPError("MCP 工具 write_file 执行失败: denied"),
                ]
            ),
        )

        result = await plugin_module.run_capability(
            "edit",
            {"path": "/workspace/a.txt", "old_str": "old", "new_str": "new"},
            cfg,
            sandbox,
        )

        assert result == "写入失败: MCP 工具 write_file 执行失败: denied"

    @pytest.mark.asyncio
    async def test_runtime_unwraps_read_file_json_content(self, plugin_module) -> None:
        fake_sandbox = FakeSandboxClient({"/workspace/a.txt": "hello"})

        async with plugin_module.SandboxRuntime(plugin_module.SandboxConfig(), fake_sandbox) as sandbox:
            result = await sandbox.call_tool("read_file", {"path": "/workspace/a.txt"})

        assert result == "hello"

    @pytest.mark.asyncio
    async def test_runtime_formats_process_json_and_omits_cwd(self, plugin_module) -> None:
        fake_sandbox = FakeSandboxClient()

        async with plugin_module.SandboxRuntime(plugin_module.SandboxConfig(), fake_sandbox) as sandbox:
            result = await sandbox.call_tool(
                "execute_code",
                {"language": "python", "code": "print('hello')"},
            )

        assert result == "script-result"
        assert fake_sandbox.calls[1] == (
            "execute_code",
            {"sandbox_id": "sbx-1", "language": "python", "code": "print('hello')"},
        )

    @pytest.mark.asyncio
    async def test_edit_uses_unwrapped_file_content(self, plugin_module) -> None:
        cfg = plugin_module.CapabilitiesConfig(write_max_size_kb=1)
        fake_sandbox = FakeSandboxClient({"/workspace/a.txt": "hello old"})

        async with plugin_module.SandboxRuntime(plugin_module.SandboxConfig(), fake_sandbox) as sandbox:
            result = await plugin_module.run_capability(
                "edit",
                {"path": "/workspace/a.txt", "old_str": "old", "new_str": "new"},
                cfg,
                sandbox,
            )

        assert result == "已替换 1 处匹配"
        assert fake_sandbox.files["/workspace/a.txt"] == "hello new"


class TestSandboxTransport:
    def test_auto_transport_uses_streamable_http_for_mcp_endpoint(self, plugin_module) -> None:
        assert (
            plugin_module._resolve_mcp_transport("http://localhost:18080/mcp", "auto")
            == "streamable_http"
        )

    def test_legacy_sse_transport_uses_streamable_http_for_mcp_endpoint(self, plugin_module) -> None:
        assert (
            plugin_module._resolve_mcp_transport("http://localhost:18080/mcp", "sse")
            == "streamable_http"
        )

    def test_auto_transport_keeps_sse_for_sse_endpoint(self, plugin_module) -> None:
        assert plugin_module._resolve_mcp_transport("http://localhost:18080/sse", "auto") == "sse"

    def test_rejects_unknown_transport(self, plugin_module) -> None:
        with pytest.raises(plugin_module.SandboxMCPError, match="不支持"):
            plugin_module._resolve_mcp_transport("http://localhost:18080/mcp", "websocket")


class TestSkillSync:
    def test_collect_skill_sync_files_skips_symlink(self, plugin_module, tmp_path: Path) -> None:
        (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (tmp_path / "link.txt").symlink_to(outside)
        skill = _make_skill(plugin_module, "demo", [])
        skill.skill_path = tmp_path

        files = plugin_module._collect_skill_sync_files(skill, 1024)

        assert [item["path"] for item in files] == ["notes.md"]

    @pytest.mark.asyncio
    async def test_sync_skill_uses_execute_code(self, plugin_module, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text("instructions", encoding="utf-8")
        skill = _make_skill(plugin_module, "demo", [])
        skill.skill_path = tmp_path
        fake_sandbox = FakeSandboxClient()

        async with plugin_module.SandboxRuntime(plugin_module.SandboxConfig(), fake_sandbox) as sandbox:
            await sandbox.sync_skill(skill)

        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "destroy_sandbox",
        ]
        assert "/workspace/skill" in fake_sandbox.calls[1][1]["code"]

    @pytest.mark.asyncio
    async def test_sync_skill_raises_on_failed_execute_code(self, plugin_module, tmp_path: Path) -> None:
        class FailingExecuteClient(FakeSandboxClient):
            async def call_tool(self, name: str, arguments: dict):
                self.calls.append((name, arguments))
                if name == "execute_code":
                    return {"content": json.dumps({"exit_code": 2, "stdout": "", "stderr": "sync failed"})}
                if name == "create_sandbox":
                    return {"structuredContent": {"sandbox_id": "sbx-1"}}
                if name == "destroy_sandbox":
                    return {"content": json.dumps({"success": True})}
                return {"content": ""}

        (tmp_path / "SKILL.md").write_text("instructions", encoding="utf-8")
        skill = _make_skill(plugin_module, "demo", [])
        skill.skill_path = tmp_path
        fake_sandbox = FailingExecuteClient()

        with pytest.raises(plugin_module.SandboxMCPError, match="退出码 2"):
            async with plugin_module.SandboxRuntime(plugin_module.SandboxConfig(), fake_sandbox) as sandbox:
                await sandbox.sync_skill(skill)

        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "destroy_sandbox",
        ]

    @pytest.mark.asyncio
    async def test_create_sandbox_error_result_closes_client(self, plugin_module) -> None:
        class ErrorSandboxClient(FakeSandboxClient):
            async def call_tool(self, name: str, arguments: dict):
                self.calls.append((name, arguments))
                if name == "create_sandbox":
                    return {"isError": True, "content": "create failed"}
                return await super().call_tool(name, arguments)

        fake_sandbox = ErrorSandboxClient()
        runtime = plugin_module.SandboxRuntime(plugin_module.SandboxConfig(), fake_sandbox)

        with pytest.raises(plugin_module.SandboxMCPError, match="create_sandbox 返回错误"):
            await runtime.__aenter__()

        assert [name for name, _args in fake_sandbox.calls] == ["create_sandbox"]
        assert fake_sandbox.exited is True

    @pytest.mark.asyncio
    async def test_run_agent_loop_destroys_sandbox_when_sync_fails(self, plugin_module, tmp_path: Path) -> None:
        (tmp_path / "SKILL.md").write_text("instructions", encoding="utf-8")
        skill = plugin_module.SkillDefinition(
            name="reader",
            description="read test",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read file",
            scripts={},
            skill_path=tmp_path,
            capabilities=["read"],
        )
        config = plugin_module.SkillLoaderConfig()
        config.sandbox.skill_sync_max_size_kb = 0
        fake_sandbox = FakeSandboxClient()
        ctx = SimpleNamespace(llm=SimpleNamespace())

        result = await plugin_module.run_agent_loop(
            skill,
            "读取文件",
            ctx,
            config,
            plugin_dir=tmp_path,
            skills_dir=tmp_path,
            sandbox_client=fake_sandbox,
        )

        assert "Sandbox 执行失败" in result
        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "destroy_sandbox",
        ]
        assert fake_sandbox.exited is True


class TestScriptTools:
    def test_build_script_tools_does_not_import_script(self, plugin_module, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "danger.py"
        script.write_text(
            "\n".join(
                [
                    "raise RuntimeError('should not run on host')",
                    "TOOL_SCHEMA = {'type': 'function', 'function': {'name': 'danger', 'description': 'x', 'parameters': {'type': 'object', 'properties': {}}}}",
                    "def run():",
                    "    return 'ok'",
                ]
            ),
            encoding="utf-8",
        )
        skill = plugin_module.SkillDefinition(
            name="demo",
            description="demo",
            mode="agent",
            model="",
            max_turns=1,
            instructions="",
            scripts={"danger": script},
            skill_path=tmp_path,
            capabilities=[],
        )

        tools = plugin_module._build_script_tools(skill)

        assert tools[0]["function"]["name"] == "danger"

    def test_build_script_tools_reads_annotated_tool_schema(self, plugin_module, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "typed_schema.py"
        script.write_text(
            "\n".join(
                [
                    "TOOL_SCHEMA: dict = {'type': 'function', 'function': {'name': 'typed_schema', 'description': 'x', 'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}}",
                    "def run(text=''):",
                    "    return text",
                ]
            ),
            encoding="utf-8",
        )
        skill = plugin_module.SkillDefinition(
            name="demo",
            description="demo",
            mode="agent",
            model="",
            max_turns=1,
            instructions="",
            scripts={"typed_schema": script},
            skill_path=tmp_path,
            capabilities=[],
        )

        tools = plugin_module._build_script_tools(skill)

        assert tools[0]["function"]["parameters"]["properties"] == {
            "text": {"type": "string"}
        }

    @pytest.mark.asyncio
    async def test_run_agent_loop_executes_script_tool_in_sandbox(self, plugin_module, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "word_count.py"
        script.write_text(
            "\n".join(
                [
                    "TOOL_SCHEMA = {'type': 'function', 'function': {'name': 'word_count', 'description': 'x', 'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}}",
                    "def run(text=''):",
                    "    return str(len(text))",
                ]
            ),
            encoding="utf-8",
        )
        skill = plugin_module.SkillDefinition(
            name="demo",
            description="demo",
            mode="agent",
            model="",
            max_turns=2,
            instructions="",
            scripts={"word_count": script},
            skill_path=tmp_path,
            capabilities=[],
        )
        config = plugin_module.SkillLoaderConfig()
        fake_sandbox = FakeSandboxClient()
        calls = {"count": 0}

        class FakeLLM:
            async def generate_with_tools(self, prompt, tools, model):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "success": True,
                        "response": "",
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "function": {
                                    "name": "word_count",
                                    "arguments": '{"text": "hello"}',
                                },
                            }
                        ],
                    }
                return {"success": True, "response": "完成", "tool_calls": []}

        ctx = SimpleNamespace(llm=FakeLLM())
        result = await plugin_module.run_agent_loop(
            skill,
            "统计",
            ctx,
            config,
            plugin_dir=tmp_path,
            skills_dir=tmp_path,
            sandbox_client=fake_sandbox,
        )

        assert result == "完成"
        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "execute_code",
            "destroy_sandbox",
        ]

    @pytest.mark.asyncio
    async def test_run_agent_loop_skips_script_tools_without_sandbox(self, plugin_module, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "helper.py"
        script.write_text(
            "\n".join(
                [
                    "TOOL_SCHEMA = {'type': 'function', 'function': {'name': 'helper', 'description': 'x', 'parameters': {'type': 'object', 'properties': {}}}}",
                    "def run():",
                    "    return 'helper'",
                ]
            ),
            encoding="utf-8",
        )
        skill = plugin_module.SkillDefinition(
            name="demo",
            description="demo",
            mode="agent",
            model="",
            max_turns=1,
            instructions="answer directly",
            scripts={"helper": script},
            skill_path=tmp_path,
            capabilities=[],
        )
        config = plugin_module.SkillLoaderConfig()

        class FakeLLM:
            async def generate(self, prompt, model):
                assert "scripts/ 工具不可用" in prompt[0]["content"]
                return {"success": True, "response": "完成", "tool_calls": []}

            async def generate_with_tools(self, prompt, tools, model):
                raise AssertionError("script tools should not be exposed without sandbox")

        result = await plugin_module.run_agent_loop(
            skill,
            "直接回答",
            SimpleNamespace(llm=FakeLLM()),
            config,
            plugin_dir=tmp_path,
            skills_dir=tmp_path,
        )

        assert result == "完成"

    @pytest.mark.asyncio
    async def test_script_wrapper_binds_before_calling_run(self, plugin_module, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "echo.py"
        script.write_text("def run(input=''):\n    return input\n", encoding="utf-8")
        skill = plugin_module.SkillDefinition(
            name="demo",
            description="demo",
            mode="agent",
            model="",
            max_turns=1,
            instructions="",
            scripts={"echo": script},
            skill_path=tmp_path,
            capabilities=[],
        )
        sandbox = SimpleNamespace(
            skill_path=lambda rel: f"/workspace/skill/{rel}",
            call_tool=AsyncMock(return_value="ok"),
        )

        result = await plugin_module.run_script_tool_in_sandbox(
            skill,
            script,
            {"input": "hello"},
            sandbox,
        )

        assert result == "ok"
        code = sandbox.call_tool.await_args.args[1]["code"]
        assert "__sig.bind(**__args)" in code
        run_call_block = code.split("if __call_mode == 'input_positional':", 1)[1]
        assert "except TypeError" not in run_call_block


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_run_agent_loop_reports_missing_sandbox_endpoint_before_llm(self, plugin_module, tmp_path: Path) -> None:
        skill = plugin_module.SkillDefinition(
            name="reader",
            description="read test",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read file",
            scripts={},
            skill_path=tmp_path,
            capabilities=["read"],
        )
        config = plugin_module.SkillLoaderConfig()

        class ExplodingLLM:
            async def generate_with_tools(self, prompt, tools, model):
                raise AssertionError("LLM should not be called without sandbox endpoint")

        result = await plugin_module.run_agent_loop(
            skill,
            "读取文件",
            SimpleNamespace(llm=ExplodingLLM()),
            config,
            plugin_dir=tmp_path,
            skills_dir=tmp_path,
        )

        assert "sandbox.endpoint_url" in result

    @pytest.mark.asyncio
    async def test_run_agent_loop_uses_one_sandbox_for_read_tool(self, plugin_module, tmp_path: Path) -> None:
        (tmp_path / "note.txt").write_text("hello\nworld", encoding="utf-8")
        skill = plugin_module.SkillDefinition(
            name="reader",
            description="read test",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read file",
            scripts={},
            skill_path=tmp_path,
            capabilities=["read"],
        )
        config = plugin_module.SkillLoaderConfig()
        config.capabilities.bash_require_approval = False
        fake_sandbox = FakeSandboxClient({"/workspace/skill/note.txt": "hello\nworld"})
        calls = {"count": 0}

        class FakeLLM:
            async def generate_with_tools(self, prompt, tools, model):
                calls["count"] += 1
                if calls["count"] == 1:
                    return {
                        "success": True,
                        "response": "",
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "function": {
                                    "name": "read",
                                    "arguments": '{"path": "/workspace/skill/note.txt"}',
                                },
                            }
                        ],
                    }
                return {"success": True, "response": "完成", "tool_calls": []}

        ctx = SimpleNamespace(llm=FakeLLM())
        result = await plugin_module.run_agent_loop(
            skill,
            "读取文件",
            ctx,
            config,
            plugin_dir=tmp_path,
            skills_dir=tmp_path,
            sandbox_client=fake_sandbox,
        )
        assert result == "完成"
        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "read_file",
            "destroy_sandbox",
        ]
        assert fake_sandbox.calls[2][1]["sandbox_id"] == "sbx-1"
        assert fake_sandbox.exited is True

    @pytest.mark.asyncio
    async def test_run_agent_loop_logs_available_models_when_configured_model_missing(
        self,
        plugin_module,
        tmp_path: Path,
        caplog,
    ) -> None:
        skill = plugin_module.SkillDefinition(
            name="reader",
            description="read test",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read file",
            scripts={},
            skill_path=tmp_path,
            capabilities=["read"],
        )
        config = plugin_module.SkillLoaderConfig()
        config.default_model = "gemini-pro-agent"
        fake_sandbox = FakeSandboxClient({"/workspace/skill/note.txt": "hello"})

        class FakeLLM:
            def __init__(self) -> None:
                self.models: list[str] = []

            async def get_available_models(self):
                return ["gemini-pro-agent", "fallback-agent"]

            async def generate_with_tools(self, prompt, tools, model):
                self.models.append(model)
                raise RuntimeError("未找到名为 `gemini-pro-agent` 的模型配置")

        llm = FakeLLM()
        ctx_logger = Mock()
        caplog.set_level(logging.WARNING, logger="skill_loader")

        with caplog.at_level(logging.WARNING, logger="skill_loader"):
            result = await plugin_module.run_agent_loop(
                skill,
                "读取文件",
                SimpleNamespace(llm=llm, logger=ctx_logger),
                config,
                plugin_dir=tmp_path,
                skills_dir=tmp_path,
                sandbox_client=fake_sandbox,
            )

        assert "Agent LLM 调用失败" in result
        assert "MaiBot 可用模型" not in result
        assert llm.models == ["gemini-pro-agent"]
        assert "MaiBot LLM 模型选择" in caplog.text
        assert "available_models=gemini-pro-agent, fallback-agent" in caplog.text
        assert ctx_logger.warning.call_count == 2

    @pytest.mark.asyncio
    async def test_run_agent_loop_destroys_sandbox_on_llm_error(self, plugin_module, tmp_path: Path) -> None:
        skill = plugin_module.SkillDefinition(
            name="reader",
            description="read test",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read file",
            scripts={},
            skill_path=tmp_path,
            capabilities=["read"],
        )
        config = plugin_module.SkillLoaderConfig()
        fake_sandbox = FakeSandboxClient()

        class FailingLLM:
            async def generate_with_tools(self, prompt, tools, model):
                raise RuntimeError("boom")

        ctx = SimpleNamespace(llm=FailingLLM())
        result = await plugin_module.run_agent_loop(
            skill,
            "读取文件",
            ctx,
            config,
            plugin_dir=tmp_path,
            skills_dir=tmp_path,
            sandbox_client=fake_sandbox,
        )

        assert "Agent LLM 调用失败" in result
        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "destroy_sandbox",
        ]
        assert fake_sandbox.exited is True


class TestMaiBotToMCPFlow:
    @pytest.mark.asyncio
    async def test_invoke_component_reads_from_mcp_and_sends_final_result(
        self,
        plugin,
        plugin_module,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "e2e-reader"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("reader instructions", encoding="utf-8")
        skill = plugin_module.SkillDefinition(
            name="e2e-reader",
            description="end-to-end reader",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read the requested file",
            scripts={},
            skill_path=skill_dir,
            capabilities=["read"],
        )
        plugin._skills = {skill.name: skill}
        plugin._last_loaded_skills_dir = str(tmp_path)
        plugin.config.sandbox.endpoint_url = "http://sandbox.invalid/mcp"
        fake_sandbox = FakeSandboxClient({"/workspace/skill/note.txt": "hello from sandbox"})

        class FakeLLM:
            def __init__(self) -> None:
                self.calls = 0

            async def generate_with_tools(self, prompt, tools, model):
                self.calls += 1
                if self.calls == 1:
                    assert "[最近的聊天记录" in prompt[1]["content"]
                    assert any(tool["function"]["name"] == "read" for tool in tools)
                    return {
                        "success": True,
                        "response": "",
                        "tool_calls": [
                            {
                                "id": "read-1",
                                "function": {
                                    "name": "read",
                                    "arguments": '{"path": "/workspace/skill/note.txt"}',
                                },
                            }
                        ],
                    }
                assert any(
                    msg.get("role") == "tool" and msg.get("content") == "hello from sandbox"
                    for msg in prompt
                )
                return {"success": True, "response": "读取完成: hello from sandbox", "tool_calls": []}

        ctx = _make_maibot_ctx(
            FakeLLM(),
            recent_messages=[{"content": "上一条消息"}],
            readable_context="用户: 上一条消息",
        )
        plugin._ctx = ctx

        with patch.object(plugin_module, "MCPSandboxClient", return_value=fake_sandbox):
            result = await plugin.invoke_component(
                "e2e-reader",
                task="读取 note.txt",
                stream_id="stream-e2e-read",
            )

        assert result == {
            "name": "e2e-reader",
            "content": "[e2e-reader] 已将结果直接发送给用户。",
        }
        assert ctx.sent == [("读取完成: hello from sandbox", "stream-e2e-read")]
        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "read_file",
            "destroy_sandbox",
        ]
        assert fake_sandbox.calls[2][1]["sandbox_id"] == "sbx-1"
        ctx.message.get_recent.assert_awaited_once_with("stream-e2e-read", limit=10)
        ctx.message.build_readable.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invoke_component_runs_script_tool_through_mcp(
        self,
        plugin,
        plugin_module,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "e2e-script"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("script instructions", encoding="utf-8")
        script = scripts_dir / "word_count.py"
        script.write_text(
            "\n".join(
                [
                    "TOOL_SCHEMA = {'type': 'function', 'function': {'name': 'word_count', 'description': 'count', 'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}}",
                    "def run(text=''):",
                    "    return str(len(text))",
                ]
            ),
            encoding="utf-8",
        )
        skill = plugin_module.SkillDefinition(
            name="e2e-script",
            description="end-to-end script",
            mode="agent",
            model="",
            max_turns=2,
            instructions="count words",
            scripts={"word_count": script},
            skill_path=skill_dir,
            capabilities=[],
        )
        plugin._skills = {skill.name: skill}
        plugin._last_loaded_skills_dir = str(tmp_path)
        plugin.config.sandbox.endpoint_url = "http://sandbox.invalid/mcp"
        fake_sandbox = FakeSandboxClient()

        class FakeLLM:
            def __init__(self) -> None:
                self.calls = 0

            async def generate_with_tools(self, prompt, tools, model):
                self.calls += 1
                if self.calls == 1:
                    assert [tool["function"]["name"] for tool in tools] == ["word_count"]
                    return {
                        "success": True,
                        "response": "",
                        "tool_calls": [
                            {
                                "id": "script-1",
                                "function": {
                                    "name": "word_count",
                                    "arguments": '{"text": "hello"}',
                                },
                            }
                        ],
                    }
                assert any(
                    msg.get("role") == "tool" and msg.get("content") == "script-result"
                    for msg in prompt
                )
                return {"success": True, "response": "统计完成: script-result", "tool_calls": []}

        ctx = _make_maibot_ctx(FakeLLM())
        plugin._ctx = ctx

        with patch.object(plugin_module, "MCPSandboxClient", return_value=fake_sandbox):
            result = await plugin.invoke_component(
                "e2e-script",
                task="统计 hello",
                stream_id="stream-e2e-script",
            )

        assert result["content"] == "[e2e-script] 已将结果直接发送给用户。"
        assert ctx.sent == [("统计完成: script-result", "stream-e2e-script")]
        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "execute_code",
            "destroy_sandbox",
        ]
        assert fake_sandbox.calls[2][1]["language"] == "python"
        assert "/workspace/skill/scripts/word_count.py" in fake_sandbox.calls[2][1]["code"]

    @pytest.mark.asyncio
    async def test_invoke_component_sends_sandbox_setup_error_before_llm(
        self,
        plugin,
        plugin_module,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "e2e-missing-sandbox"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("reader instructions", encoding="utf-8")
        skill = plugin_module.SkillDefinition(
            name="e2e-missing-sandbox",
            description="missing sandbox reader",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read",
            scripts={},
            skill_path=skill_dir,
            capabilities=["read"],
        )
        plugin._skills = {skill.name: skill}
        plugin._last_loaded_skills_dir = str(tmp_path)
        plugin.config.sandbox.endpoint_url = ""

        class ExplodingLLM:
            async def generate_with_tools(self, prompt, tools, model):
                raise AssertionError("LLM should not run without sandbox")

        ctx = _make_maibot_ctx(ExplodingLLM())
        plugin._ctx = ctx

        result = await plugin.invoke_component(
            "e2e-missing-sandbox",
            task="读取文件",
            stream_id="stream-e2e-missing-sandbox",
        )

        assert result["content"] == "[e2e-missing-sandbox] 已将结果直接发送给用户。"
        assert len(ctx.sent) == 1
        assert "sandbox.endpoint_url" in ctx.sent[0][0]
        assert ctx.sent[0][1] == "stream-e2e-missing-sandbox"

    @pytest.mark.asyncio
    async def test_invoke_component_passes_mcp_error_to_llm_and_sends_result(
        self,
        plugin,
        plugin_module,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "e2e-read-error"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("reader instructions", encoding="utf-8")
        skill = plugin_module.SkillDefinition(
            name="e2e-read-error",
            description="read error",
            mode="agent",
            model="",
            max_turns=2,
            instructions="read",
            scripts={},
            skill_path=skill_dir,
            capabilities=["read"],
        )
        plugin._skills = {skill.name: skill}
        plugin._last_loaded_skills_dir = str(tmp_path)
        plugin.config.sandbox.endpoint_url = "http://sandbox.invalid/mcp"
        fake_sandbox = ErrorReadSandboxClient()

        class FakeLLM:
            def __init__(self) -> None:
                self.calls = 0

            async def generate_with_tools(self, prompt, tools, model):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "success": True,
                        "response": "",
                        "tool_calls": [
                            {
                                "id": "read-error-1",
                                "function": {
                                    "name": "read",
                                    "arguments": '{"path": "/workspace/skill/secret.txt"}',
                                },
                            }
                        ],
                    }
                assert any(
                    msg.get("role") == "tool"
                    and "读取失败: MCP 工具 read_file 返回错误: read denied" in msg.get("content", "")
                    for msg in prompt
                )
                return {"success": True, "response": "读取失败: read denied", "tool_calls": []}

        ctx = _make_maibot_ctx(FakeLLM())
        plugin._ctx = ctx

        with patch.object(plugin_module, "MCPSandboxClient", return_value=fake_sandbox):
            result = await plugin.invoke_component(
                "e2e-read-error",
                task="读取 secret.txt",
                stream_id="stream-e2e-read-error",
            )

        assert result["content"] == "[e2e-read-error] 已将结果直接发送给用户。"
        assert ctx.sent == [("读取失败: read denied", "stream-e2e-read-error")]
        assert [name for name, _args in fake_sandbox.calls] == [
            "create_sandbox",
            "execute_code",
            "read_file",
            "destroy_sandbox",
        ]
