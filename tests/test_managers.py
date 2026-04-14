from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.core.config import AppConfig, DEFAULT_CONFIG
from src.core.theme_manager import ThemeManager
from src.plugins.manager import PluginManager
from src.scripting import ScriptRuntimeError
from src.ui.editor import PvimEditor


class ThemeManagerTests(unittest.TestCase):
    def test_install_list_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            user = root / "user"
            source = root / "source"
            builtin.mkdir(parents=True, exist_ok=True)
            user.mkdir(parents=True, exist_ok=True)
            source.mkdir(parents=True, exist_ok=True)

            (builtin / "pvim.theme.builtin.json").write_text(
                json.dumps(
                    {
                        "meta": {"name": "builtin", "version": "1.0.0", "description": "built-in"},
                        "ui": {"editor": {"fg": "#ffffff", "bg": "#000000"}},
                        "syntax": {"keyword": {"fg": "#ff00ff"}},
                    }
                ),
                encoding="utf-8",
            )
            (source / "preview.ppm").write_text("P3\n1 1\n255\n255 0 0\n", encoding="utf-8")
            source_theme = source / "theme.json"
            source_theme.write_text(
                json.dumps(
                    {
                        "meta": {
                            "name": "installed",
                            "version": "2.0.0",
                            "description": "installed theme",
                            "preview": "preview.ppm",
                        },
                        "ui": {"editor": {"fg": "#ffffff", "bg": "#000000"}},
                        "syntax": {"keyword": {"fg": "#00ffff"}},
                    }
                ),
                encoding="utf-8",
            )

            manager = ThemeManager(builtin_dirs=[builtin], user_dir=user)
            initial = manager.list_themes()
            self.assertTrue(any(item.name == "builtin" for item in initial))

            installed = manager.install(source_theme)
            self.assertEqual(installed.name, "installed")
            self.assertEqual(installed.version, "2.0.0")
            self.assertTrue((user / "previews" / "preview.ppm").exists())

            listed = manager.list_themes()
            self.assertTrue(any(item.name == "installed" and item.source == "user" for item in listed))
            resolved = manager.resolve("installed")
            self.assertIsNotNone(resolved)

            message = manager.uninstall("installed")
            self.assertIn("Uninstalled theme", message)
            self.assertFalse(any(item.name == "installed" for item in manager.list_themes()))


class PluginManagerTests(unittest.TestCase):
    def test_plugin_metadata_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_dir = root / "myplugin"
            plugin_dir.mkdir(parents=True, exist_ok=True)
            (plugin_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": "demo-plugin",
                        "version": "0.7.0",
                        "description": "demo plugin",
                        "main": "main.pvi",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_dir / "main.pvi").write_text("let x = 1;\n", encoding="utf-8")

            manager = PluginManager(
                plugins_root=root,
                enabled=True,
                step_limit=100000,
                auto_load=False,
                host_api=None,
            )
            manager.discover()
            listed = manager.list_plugins()
            self.assertTrue(any(item["name"] == "demo-plugin" for item in listed))
            record = next(item for item in listed if item["name"] == "demo-plugin")
            self.assertEqual(record["version"], "0.7.0")
            self.assertEqual(record["description"], "demo plugin")

            message = manager.uninstall("demo-plugin")
            self.assertIn("Uninstalled plugin", message)
            self.assertFalse(any(item["name"] == "demo-plugin" for item in manager.list_plugins()))

    def test_plugin_sandbox_blocks_disallowed_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = PluginManager(
                plugins_root=Path(tmp),
                enabled=False,
                step_limit=100000,
                auto_load=False,
                host_api=lambda _obj, action, _args: action,
                sandbox_enabled=True,
                allowed_actions={"message"},
            )
            with self.assertRaises(ScriptRuntimeError):
                manager._native_api([1, "command", ":q"], line=1)  # type: ignore[attr-defined]
            result = manager._native_api([1, "message", "ok"], line=1)  # type: ignore[attr-defined]
            self.assertEqual(result, "message")


class ShortcutManagerTests(unittest.TestCase):
    def test_shortcut_overrides_persist_in_runtime_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "pvim.config.json"
            payload = copy.deepcopy(DEFAULT_CONFIG)
            payload["runtime"]["directory"] = str(root / "runtime")
            payload["features"]["session"]["enabled"] = False
            payload["features"]["swap"]["enabled"] = False
            payload["features"]["notifications"]["enabled"] = False
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            config = AppConfig.load(config_path)
            editor = PvimEditor(None, config)
            try:
                self.assertTrue(editor._handle_keys_command(["set", "toggle_comment", "F11"]))
                self.assertEqual(editor._shortcut("toggle_comment", "CTRL_SLASH"), "F11")
            finally:
                editor._async_runtime.close()

            reloaded = AppConfig.load(config_path)
            editor2 = PvimEditor(None, reloaded)
            try:
                self.assertEqual(editor2._shortcut("toggle_comment", "CTRL_SLASH"), "F11")
            finally:
                editor2._async_runtime.close()


if __name__ == "__main__":
    unittest.main()
