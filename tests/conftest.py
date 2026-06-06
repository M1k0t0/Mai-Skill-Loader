"""Skill Loader 插件测试 fixtures。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_MODULE_NAME = "skill_loader_plugin_under_test"


@pytest.fixture(scope="session")
def plugin_module():
    """加载插件模块（避免与主程序模块名冲突）。"""
    module_path = PLUGIN_DIR / "plugin.py"
    spec = importlib.util.spec_from_file_location(PLUGIN_MODULE_NAME, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin(plugin_module):
    """构造已注入配置的插件实例。"""
    instance = plugin_module.SkillLoaderPlugin()
    instance.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "skills_dir": "skills",
            "capabilities": {
                "allow_bash": True,
                "allow_read": True,
                "allow_write": False,
                "allow_edit": False,
                "admin_ids": ["qq:10001"],
            },
        }
    )
    return instance
