"""Skill Loader 核心逻辑测试。"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


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

    def test_read_roots_include_skill_path(self, plugin_module) -> None:
        plugin_dir = Path("/opt/skill_loader")
        skill_path = Path("/opt/external-skills/demo")
        roots = plugin_module._resolve_read_roots([], plugin_dir, [skill_path])
        assert plugin_dir.resolve() in roots
        assert skill_path.resolve() in roots

    def test_write_roots_default_to_data_dir(self, plugin_module, tmp_path: Path) -> None:
        roots = plugin_module._resolve_write_roots([], tmp_path)
        assert roots == [(tmp_path / "data").resolve()]

    def test_coerce_max_lines(self, plugin_module) -> None:
        assert plugin_module._coerce_max_lines(None) == 200
        assert plugin_module._coerce_max_lines("bad") == 200
        assert plugin_module._coerce_max_lines(50) == 50


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


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_run_agent_loop_executes_read_tool(self, plugin_module, tmp_path: Path) -> None:
        data_file = tmp_path / "data" / "note.txt"
        data_file.parent.mkdir(parents=True)
        data_file.write_text("hello\nworld", encoding="utf-8")

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
                                    "arguments": f'{{"path": "{data_file}"}}',
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
        )
        assert "hello" in result or result == "完成"
