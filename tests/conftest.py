"""Skill Loader 插件测试 fixtures。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_MODULE_NAME = "skill_loader_plugin_under_test"


def _install_maibot_sdk_stub() -> None:
    """Provide the minimal SDK surface used by the unit tests."""
    if importlib.util.find_spec("maibot_sdk") is not None:
        return

    class _FieldInfo:
        def __init__(self, default=None, default_factory=None, **_kwargs):
            self.default = default
            self.default_factory = default_factory

        def make_default(self):
            if self.default_factory is not None:
                return self.default_factory()
            if isinstance(self.default, (dict, list, set)):
                return self.default.copy()
            return self.default

    def Field(default=None, default_factory=None, **kwargs):
        return _FieldInfo(default=default, default_factory=default_factory, **kwargs)

    class PluginConfigBase:
        def __init__(self, **kwargs):
            for cls in reversed(type(self).mro()):
                annotations = getattr(cls, "__annotations__", {})
                for name, field_type in annotations.items():
                    field_info = getattr(type(self), name, None)
                    if name in kwargs:
                        value = kwargs[name]
                    elif isinstance(field_info, _FieldInfo):
                        value = field_info.make_default()
                    else:
                        continue

                    if isinstance(value, dict) and isinstance(field_info, _FieldInfo):
                        default_value = field_info.make_default()
                        if isinstance(default_value, PluginConfigBase):
                            value = type(default_value)(**value)
                        elif isinstance(field_type, type) and issubclass(field_type, PluginConfigBase):
                            value = field_type(**value)
                    setattr(self, name, value)

    class MaiBotPlugin:
        config_model = PluginConfigBase

        def __init__(self):
            self._plugin_config_instance = None
            self._ctx = None

        @property
        def ctx(self):
            return self._ctx

        def set_plugin_config(self, config):
            self._plugin_config_instance = self.config_model(**config)

    module = types.ModuleType("maibot_sdk")
    module.Field = Field
    module.MaiBotPlugin = MaiBotPlugin
    module.PluginConfigBase = PluginConfigBase
    sys.modules["maibot_sdk"] = module


@pytest.fixture(scope="session")
def plugin_module():
    """加载插件模块（避免与主程序模块名冲突）。"""
    _install_maibot_sdk_stub()
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
