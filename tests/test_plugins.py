from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from dingtalk_ops_bridge.action_api import (
    PLUGIN_API_VERSION,
    ActionContext,
    ActionError,
    ActionRegistry,
)
from dingtalk_ops_bridge.action_types.command import CommandActionProvider
from dingtalk_ops_bridge.action_types import build_default_registry
from dingtalk_ops_bridge.actions import execute_actions, load_action_catalog
from dingtalk_ops_bridge.commands import handle_prompt
from dingtalk_ops_bridge.plugin_loader import load_external_plugins


class PluginTests(unittest.TestCase):
    def test_duplicate_command_prefix_is_rejected_with_both_sources(self):
        class Commands:
            api_version = PLUGIN_API_VERSION
            command_prefixes = ("shared",)

            def handle(self, prompt, context):
                return None

        registry = ActionRegistry()
        registry.register_command(Commands(), source="plugin-a/action_plugin.json")
        with self.assertRaises(ActionError) as raised:
            registry.register_command(Commands(), source="plugin-b/action_plugin.json")
        message = str(raised.exception)
        self.assertIn("'shared'", message)
        self.assertIn("plugin-a/action_plugin.json", message)
        self.assertIn("plugin-b/action_plugin.json", message)

    def test_command_provider_cannot_claim_reserved_framework_prefix(self):
        class Commands:
            api_version = PLUGIN_API_VERSION
            command_prefixes = ("check",)

            def handle(self, prompt, context):
                return "must-not-run"

        with self.assertRaisesRegex(ActionError, "reserved by the framework"):
            ActionRegistry().register_command(Commands(), source="plugin/action_plugin.json")

    def test_only_one_command_fallback_is_allowed(self):
        class Commands:
            api_version = PLUGIN_API_VERSION
            command_fallback = True

            def handle(self, prompt, context):
                return None

        registry = ActionRegistry()
        registry.register_command(Commands(), source="plugin-a/action_plugin.json")
        with self.assertRaises(ActionError) as raised:
            registry.register_command(Commands(), source="plugin-b/action_plugin.json")
        message = str(raised.exception)
        self.assertIn("only one fallback", message)
        self.assertIn("plugin-a/action_plugin.json", message)
        self.assertIn("plugin-b/action_plugin.json", message)

    def test_manifest_command_prefix_conflict_fails_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_names: list[str] = []
            source_dirs: list[Path] = []
            for folder in ("a-plugin", "b-plugin"):
                module_name = f"command_conflict_{folder.replace('-', '_')}_{uuid.uuid4().hex}"
                module_names.append(module_name)
                plugin_dir = root / folder
                source_dir = plugin_dir / "src"
                source_dirs.append(source_dir)
                source_dir.mkdir(parents=True)
                (source_dir / f"{module_name}.py").write_text(
                    "\n".join(
                        [
                            "from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION",
                            "",
                            "class Commands:",
                            "    api_version = PLUGIN_API_VERSION",
                            "    command_prefixes = ('shared',)",
                            "",
                            "    def handle(self, prompt, context):",
                            "        return 'shared'",
                            "",
                            "COMMANDS = Commands()",
                        ]
                    ),
                    encoding="utf-8",
                )
                (plugin_dir / "action_plugin.json").write_text(
                    json.dumps(
                        {
                            "apiVersion": PLUGIN_API_VERSION,
                            "module": module_name,
                            "commandProvider": "COMMANDS",
                            "pythonPath": "src",
                        }
                    ),
                    encoding="utf-8",
                )

            try:
                with self.assertRaises(ActionError) as raised:
                    load_external_plugins(ActionRegistry(), root)
            finally:
                sys.path[:] = [
                    item for item in sys.path if item not in {str(path) for path in source_dirs}
                ]
                for module_name in module_names:
                    sys.modules.pop(module_name, None)

        message = str(raised.exception)
        self.assertIn("'shared'", message)
        self.assertIn("a-plugin/action_plugin.json", message)
        self.assertIn("b-plugin/action_plugin.json", message)

    def test_declared_prefix_routes_before_fallback(self):
        class Routed:
            api_version = PLUGIN_API_VERSION
            command_prefixes = ("alpha",)

            def handle(self, prompt, context):
                return "routed"

        class Fallback:
            api_version = PLUGIN_API_VERSION
            command_fallback = True

            def handle(self, prompt, context):
                return "fallback"

        registry = ActionRegistry()
        registry.register(CommandActionProvider())
        registry.register_command(Fallback(), source="fallback")
        registry.register_command(Routed(), source="routed")
        with tempfile.TemporaryDirectory() as tmp:
            actions_path = Path(tmp) / "actions.json"
            actions_path.write_text(
                json.dumps({"actions": {"hello": {"command": ["true"]}}}),
                encoding="utf-8",
            )
            catalog = load_action_catalog(actions_path, registry=registry)
            self.assertEqual(handle_prompt("alpha anything", catalog), "routed")
            self.assertEqual(handle_prompt("other", catalog), "fallback")
            self.assertIn("DingTalk Ops Bridge", handle_prompt("help", catalog))

    def test_declining_explicit_route_does_not_fall_through_to_framework(self):
        class StatusRoute:
            api_version = PLUGIN_API_VERSION
            command_prefixes = ("status",)

            def handle(self, prompt, context):
                return None

        class Fallback:
            api_version = PLUGIN_API_VERSION
            command_fallback = True

            def handle(self, prompt, context):
                return "fallback"

        registry = ActionRegistry()
        registry.register(CommandActionProvider())
        registry.register_command(StatusRoute(), source="status-route")
        registry.register_command(Fallback(), source="fallback")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actions_path = root / "actions.json"
            actions_path.write_text(
                json.dumps({"actions": {"hello": {"command": ["true"]}}}),
                encoding="utf-8",
            )
            monitors_path = root / "monitors.json"
            monitors_path.write_text(
                json.dumps(
                    {
                        "monitors": {
                            "ok": {
                                "check": {"type": "command", "command": ["true"]},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog = load_action_catalog(actions_path, registry=registry)
            output = handle_prompt("status", catalog, monitors_path=monitors_path)
        self.assertIn("插件命令 `status` 未返回结果", output)
        self.assertNotIn("监控检查结果", output)

    def test_bridge_status_remains_available_when_plugin_claims_status(self):
        class StatusRoute:
            api_version = PLUGIN_API_VERSION
            command_prefixes = ("status",)

            def handle(self, prompt, context):
                return "plugin-status"

        registry = ActionRegistry()
        registry.register(CommandActionProvider())
        registry.register_command(StatusRoute(), source="status-route")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actions_path = root / "actions.json"
            actions_path.write_text(
                json.dumps({"actions": {"hello": {"command": ["true"]}}}),
                encoding="utf-8",
            )
            monitors_path = root / "monitors.json"
            monitors_path.write_text(
                json.dumps(
                    {
                        "monitors": {
                            "ok": {
                                "check": {"type": "command", "command": ["true"]},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog = load_action_catalog(actions_path, registry=registry)
            self.assertEqual(handle_prompt("status", catalog), "plugin-status")
            bridge_output = handle_prompt(
                "bridge status", catalog, monitors_path=monitors_path
            )
        self.assertIn("监控检查结果", bridge_output)

    def test_external_repository_manifest_registers_provider(self):
        module_name = f"external_action_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_dir = root / "external-actions"
            source_dir = plugin_dir / "src"
            source_dir.mkdir(parents=True)
            module_path = source_dir / f"{module_name}.py"
            module_path.write_text(
                "\n".join(
                    [
                        "from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION, PreparedAction",
                        "",
                        "class Provider:",
                        "    api_version = PLUGIN_API_VERSION",
                        "    type_name = 'external'",
                        "",
                        "    def validate(self, definition):",
                        "        pass",
                        "",
                        "    def prepare(self, definition, values, context):",
                        "        return PreparedAction(definition=definition, command=('printf', 'external-ok'))",
                        "",
                        "PROVIDER = Provider()",
                    ]
                ),
                encoding="utf-8",
            )
            (plugin_dir / "action_plugin.json").write_text(
                json.dumps(
                    {
                        "apiVersion": PLUGIN_API_VERSION,
                        "module": module_name,
                        "provider": "PROVIDER",
                        "pythonPath": "src",
                    }
                ),
                encoding="utf-8",
            )

            try:
                with mock.patch.dict(os.environ, {"ACTION_PLUGIN_DIR": str(root)}):
                    registry = build_default_registry()
                self.assertIn("external", registry.types())
                actions_path = root / "actions.json"
                actions_path.write_text(
                    json.dumps({"actions": {"external": {"type": "external"}}}),
                    encoding="utf-8",
                )
                catalog = load_action_catalog(actions_path, registry=registry)
                execution = execute_actions(catalog, ("external",), {})
                self.assertTrue(execution.results[0].ok)
                self.assertEqual(execution.results[0].stdout, "external-ok")
            finally:
                sys.path[:] = [item for item in sys.path if item != str(source_dir)]
                sys.modules.pop(module_name, None)

    def test_plugin_api_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "old-actions"
            plugin_dir.mkdir()
            (plugin_dir / "action_plugin.json").write_text(
                json.dumps({"apiVersion": PLUGIN_API_VERSION + 1, "module": "missing"}),
                encoding="utf-8",
            )
            with self.assertRaises(ActionError):
                load_external_plugins(ActionRegistry(), Path(tmp))

    def test_manifest_services_are_loaded_in_dependency_order(self):
        provider_module = f"service_provider_{uuid.uuid4().hex}"
        consumer_module = f"service_consumer_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            order_file = root / "order.txt"
            provider_dir = root / "z-provider"
            consumer_dir = root / "a-consumer"
            for plugin_dir, module_name, label in (
                (provider_dir, provider_module, "provider"),
                (consumer_dir, consumer_module, "consumer"),
            ):
                source_dir = plugin_dir / "src"
                source_dir.mkdir(parents=True)
                (source_dir / f"{module_name}.py").write_text(
                    "\n".join(
                        [
                            "import os",
                            "from pathlib import Path",
                            "from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION",
                            "with Path(os.environ['PLUGIN_ORDER_FILE']).open('a', encoding='utf-8') as handle:",
                            f"    handle.write('{label}\\n')",
                            "",
                            "class Service:",
                            "    api_version = PLUGIN_API_VERSION",
                            "",
                            "class Provider:",
                            "    api_version = PLUGIN_API_VERSION",
                            "    type_name = 'dependency-consumer'",
                            "",
                            "    def validate(self, definition):",
                            "        pass",
                            "",
                            "    def prepare(self, definition, values, context):",
                            "        return None",
                            "",
                            "SERVICE = Service()",
                            "PROVIDER = Provider()",
                        ]
                    ),
                    encoding="utf-8",
                )
            (provider_dir / "action_plugin.json").write_text(
                json.dumps(
                    {
                        "apiVersion": PLUGIN_API_VERSION,
                        "module": provider_module,
                        "services": {"demo": "SERVICE"},
                        "pythonPath": "src",
                    }
                ),
                encoding="utf-8",
            )
            (consumer_dir / "action_plugin.json").write_text(
                json.dumps(
                    {
                        "apiVersion": PLUGIN_API_VERSION,
                        "module": consumer_module,
                        "provider": "PROVIDER",
                        "requires": ["demo"],
                        "pythonPath": "src",
                    }
                ),
                encoding="utf-8",
            )
            source_dirs = [provider_dir / "src", consumer_dir / "src"]
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "ACTION_PLUGIN_DIR": str(root),
                        "PLUGIN_ORDER_FILE": str(order_file),
                    },
                ):
                    registry = build_default_registry()
                self.assertEqual(order_file.read_text(encoding="utf-8"), "provider\nconsumer\n")
                self.assertIsNotNone(registry.service("demo"))
            finally:
                sys.path[:] = [item for item in sys.path if item not in {str(path) for path in source_dirs}]
                sys.modules.pop(provider_module, None)
                sys.modules.pop(consumer_module, None)

    def test_missing_required_service_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "consumer"
            plugin_dir.mkdir()
            (plugin_dir / "action_plugin.json").write_text(
                json.dumps(
                    {
                        "apiVersion": PLUGIN_API_VERSION,
                        "module": "missing",
                        "provider": "PROVIDER",
                        "requires": ["missing-service"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ActionError, "requires service"):
                load_external_plugins(ActionRegistry(), Path(tmp))

    def test_plugin_dependency_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, service, requirement in (("a", "service-a", "service-b"), ("b", "service-b", "service-a")):
                plugin_dir = root / name
                plugin_dir.mkdir()
                (plugin_dir / "action_plugin.json").write_text(
                    json.dumps(
                        {
                            "apiVersion": PLUGIN_API_VERSION,
                            "module": "missing",
                            "services": {service: "SERVICE"},
                            "requires": [requirement],
                        }
                    ),
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(ActionError, "cycle"):
                load_external_plugins(ActionRegistry(), root)

    def test_manifest_command_provider_is_registered(self):
        module_name = f"external_command_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_dir = root / "external-commands"
            source_dir = plugin_dir / "src"
            source_dir.mkdir(parents=True)
            (source_dir / f"{module_name}.py").write_text(
                "\n".join(
                    [
                        "from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION",
                        "",
                        "class Commands:",
                        "    api_version = PLUGIN_API_VERSION",
                        "    command_prefixes = ('plugin-check',)",
                        "",
                        "    def handle(self, prompt, context):",
                        "        return 'plugin-command-ok' if prompt == 'plugin-check' else None",
                        "",
                        "COMMANDS = Commands()",
                    ]
                ),
                encoding="utf-8",
            )
            (plugin_dir / "action_plugin.json").write_text(
                json.dumps(
                    {
                        "apiVersion": PLUGIN_API_VERSION,
                        "module": module_name,
                        "commandProvider": "COMMANDS",
                        "pythonPath": "src",
                    }
                ),
                encoding="utf-8",
            )

            try:
                with mock.patch.dict(os.environ, {"ACTION_PLUGIN_DIR": str(root)}):
                    registry = build_default_registry()
                self.assertEqual(len(registry.commands()), 1)
                self.assertEqual(registry.command_prefixes(), ("plugin-check",))
                self.assertEqual(
                    registry.commands()[0].handle("plugin-check", ActionContext()),
                    "plugin-command-ok",
                )
            finally:
                sys.path[:] = [item for item in sys.path if item != str(source_dir)]
                sys.modules.pop(module_name, None)

    def test_manifest_monitor_provider_is_registered(self):
        module_name = f"external_monitor_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_dir = root / "external-monitors"
            source_dir = plugin_dir / "src"
            source_dir.mkdir(parents=True)
            (source_dir / f"{module_name}.py").write_text(
                "\n".join(
                    [
                        "from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION, MonitorResult",
                        "",
                        "class Monitors:",
                        "    api_version = PLUGIN_API_VERSION",
                        "    type_name = 'external-monitor'",
                        "",
                        "    def monitors(self, context):",
                        "        return {}",
                        "",
                        "    def validate(self, check):",
                        "        pass",
                        "",
                        "    def check(self, check, context):",
                        "        return MonitorResult(True, 'ok')",
                        "",
                        "MONITORS = Monitors()",
                    ]
                ),
                encoding="utf-8",
            )
            (plugin_dir / "action_plugin.json").write_text(
                json.dumps(
                    {
                        "apiVersion": PLUGIN_API_VERSION,
                        "module": module_name,
                        "monitorProvider": "MONITORS",
                        "pythonPath": "src",
                    }
                ),
                encoding="utf-8",
            )
            try:
                registry = ActionRegistry()
                load_external_plugins(registry, root)
                self.assertEqual(registry.monitor_types(), ("external-monitor",))
            finally:
                sys.path[:] = [item for item in sys.path if item != str(source_dir)]
                sys.modules.pop(module_name, None)

    def test_manifest_actions_file_is_merged(self):
        module_name = f"external_actions_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_dir = root / "external-actions"
            source_dir = plugin_dir / "src"
            source_dir.mkdir(parents=True)
            (source_dir / f"{module_name}.py").write_text(
                "\n".join(
                    [
                        "from dingtalk_ops_bridge.plugin_api import PLUGIN_API_VERSION, PreparedAction",
                        "",
                        "class Provider:",
                        "    api_version = PLUGIN_API_VERSION",
                        "    type_name = 'external-file'",
                        "",
                        "    def validate(self, definition):",
                        "        pass",
                        "",
                        "    def prepare(self, definition, values, context):",
                        "        return PreparedAction(definition=definition, command=('printf', 'file-ok'))",
                        "",
                        "PROVIDER = Provider()",
                    ]
                ),
                encoding="utf-8",
            )
            actions_dir = plugin_dir / "config"
            actions_dir.mkdir()
            (actions_dir / "actions.json").write_text(
                json.dumps(
                    {
                        "actions": {
                            "external-file": {
                                "type": "external-file",
                                "description": "from plugin",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (plugin_dir / "action_plugin.json").write_text(
                json.dumps(
                    {
                        "apiVersion": PLUGIN_API_VERSION,
                        "module": module_name,
                        "provider": "PROVIDER",
                        "actionsFile": "config/actions.json",
                        "pythonPath": "src",
                    }
                ),
                encoding="utf-8",
            )
            core_actions = root / "actions.json"
            core_actions.write_text(
                json.dumps({"actions": {"hello": {"command": ["true"]}}}),
                encoding="utf-8",
            )

            try:
                with mock.patch.dict(os.environ, {"ACTION_PLUGIN_DIR": str(root)}):
                    catalog = load_action_catalog(core_actions, registry=build_default_registry())
                self.assertIn("external-file", catalog.actions)
                execution = execute_actions(catalog, ("external-file",), {})
                self.assertEqual(execution.results[0].stdout, "file-ok")
            finally:
                sys.path[:] = [item for item in sys.path if item != str(source_dir)]
                sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
