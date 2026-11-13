from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dingtalk_ops_bridge.action_api import ActionContext
from dingtalk_ops_bridge.action_types import build_default_registry
from dingtalk_ops_bridge.actions import ActionValidationError, execute_actions, load_action_catalog
from dingtalk_ops_bridge.commands import format_help, handle_prompt
from dingtalk_ops_bridge.util import ProcessResult


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = PROJECT_ROOT / "plugins" / "nodes"


class NodePluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {"ACTION_PLUGIN_DIR": str(PLUGIN_DIR)}):
            cls.registry = build_default_registry()
        cls.plugin = importlib.import_module("nodes_plugin")

    def test_password_node_builds_sshpass_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_nodes(Path(tmp))
            with mock.patch.dict(os.environ, {"NODES_CONFIG": str(path)}):
                catalog = self.plugin._load_catalog(ActionContext())
            command, env = catalog.nodes["web-01"].ssh_command("hostname")
            self.assertEqual(command[:3], ("sshpass", "-e", "ssh"))
            self.assertIn("ops@10.0.0.11", command)
            self.assertEqual(env, {"SSHPASS": "${TEST_NODE_PASSWORD}"})

    def test_plaintext_password_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodes.json"
            path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "bad": {
                                "host": "10.0.0.11",
                                "user": "ops",
                                "password": "secret",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"NODES_CONFIG": str(path)}):
                with self.assertRaises(ActionValidationError):
                    self.plugin._load_catalog(ActionContext())

    def test_legacy_nodes_file_environment_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_nodes(Path(tmp))
            with mock.patch.dict(
                os.environ,
                {"NODES_CONFIG": "", "NODES_FILE": str(path)},
            ):
                catalog = self.plugin._load_catalog(ActionContext())
            self.assertIn("web-01", catalog.nodes)

    def test_node_action_uses_password_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_path = self._write_nodes(root)
            actions_path = root / "actions.json"
            actions_path.write_text(
                json.dumps(
                    {
                        "actions": {
                            "node-check": {
                                "type": "node",
                                "remoteCommand": "hostname",
                                "parameters": {"node": {"type": "node", "required": True}},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            action_catalog = load_action_catalog(actions_path, registry=self.registry)
            process_result = ProcessResult(0, "web-01\n", "", False, 5)
            with mock.patch.dict(
                os.environ,
                {"NODES_CONFIG": str(nodes_path), "TEST_NODE_PASSWORD": "secret-value"},
            ), mock.patch(
                "dingtalk_ops_bridge.actions.run_process", return_value=process_result
            ) as run_process:
                execution = execute_actions(
                    action_catalog,
                    ("node-check",),
                    {"node": "web-01"},
                    context=ActionContext(),
                )
            self.assertTrue(execution.results[0].ok)
            called_command = run_process.call_args.args[0]
            called_env = run_process.call_args.kwargs["env"]
            self.assertEqual(called_command[:3], ("sshpass", "-e", "ssh"))
            self.assertEqual(called_env["SSHPASS"], "secret-value")
            self.assertNotIn("secret-value", " ".join(called_command))

    def test_node_action_rejects_missing_password_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_path = self._write_nodes(root)
            actions_path = root / "actions.json"
            actions_path.write_text(
                json.dumps(
                    {
                        "actions": {
                            "node-check": {
                                "type": "node",
                                "remoteCommand": "hostname",
                                "parameters": {"node": {"type": "node", "required": True}},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"NODES_CONFIG": str(nodes_path)}, clear=True):
                with self.assertRaises(ActionValidationError):
                    execute_actions(
                        load_action_catalog(actions_path, registry=self.registry),
                        ("node-check",),
                        {"node": "web-01"},
                        context=ActionContext(),
                    )

    def test_nodes_command_lists_nodes_without_password_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_path = self._write_nodes(root)
            actions_path = root / "actions.json"
            actions_path.write_text(
                json.dumps({"actions": {"hello": {"command": ["true"]}}}),
                encoding="utf-8",
            )
            catalog = load_action_catalog(actions_path, registry=self.registry)
            with mock.patch.dict(os.environ, {"NODES_CONFIG": str(nodes_path)}):
                output = handle_prompt("nodes", catalog)
            self.assertIn("web-01", output)
            self.assertIn("password-env", output)
            self.assertNotIn("TEST_NODE_PASSWORD", output)

    def test_help_lists_nodes_plugin_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            actions_path = Path(tmp) / "actions.json"
            actions_path.write_text(
                json.dumps({"actions": {"hello": {"command": ["true"]}}}),
                encoding="utf-8",
            )
            catalog = load_action_catalog(actions_path, registry=self.registry)
        output = format_help(catalog)
        self.assertIn("**插件命令**", output)
        self.assertIn("节点插件中的节点", output)

    def test_plugin_contributes_node_actions_and_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            actions_path = Path(tmp) / "actions.json"
            actions_path.write_text(
                json.dumps({"actions": {"hello": {"command": ["true"]}}}),
                encoding="utf-8",
            )
            catalog = load_action_catalog(actions_path, registry=self.registry)
        self.assertIn("node-login", catalog.actions)
        self.assertIn("node-info", catalog.actions)
        self.assertIn("node-disk", catalog.actions)
        self.assertIn("node-health", catalog.batches)

    def test_plugin_registers_nodes_service(self):
        self.assertIs(self.registry.service("nodes"), self.plugin.SERVICE)

    def _write_nodes(self, root: Path) -> Path:
        path = root / "nodes.json"
        path.write_text(
            json.dumps(
                {
                    "nodes": {
                        "web-01": {
                            "description": "Web node",
                            "host": "10.0.0.11",
                            "user": "ops",
                            "passwordEnv": "TEST_NODE_PASSWORD",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
