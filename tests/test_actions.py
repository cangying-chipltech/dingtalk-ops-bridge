from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path


os.environ["ACTION_PLUGIN_DIR"] = str(
    Path(tempfile.gettempdir()) / "dingtalk-ops-bridge-tests-no-plugins"
)

from dingtalk_ops_bridge.action_api import (
    PLUGIN_API_VERSION,
    ActionContext,
    ActionDefinition,
    ActionRegistry,
    PreparedAction,
)
from dingtalk_ops_bridge.actions import (
    ActionError,
    ActionValidationError,
    execute_actions,
    load_action_catalog,
    resolve_action_names,
)


def _catalog(root: Path, actions: dict, batches: dict | None = None):
    path = root / "actions.json"
    path.write_text(
        json.dumps(
            {
                "settings": {"maxConcurrency": 4, "maxOutputChars": 1000},
                "actions": actions,
                "batches": batches or {},
            }
        ),
        encoding="utf-8",
    )
    return load_action_catalog(path)


class ActionTests(unittest.TestCase):
    def test_custom_provider_can_be_registered_without_core_changes(self):
        class CustomProvider:
            api_version = PLUGIN_API_VERSION
            type_name = "custom"

            def validate(self, definition: ActionDefinition) -> None:
                if "value" not in definition.parameters:
                    raise ActionValidationError("custom action requires 'value'")

            def prepare(self, definition, values, context: ActionContext) -> PreparedAction:
                del context
                resolved = definition.resolved_values(values)
                return PreparedAction(
                    definition=definition,
                    command=("printf", resolved["value"]),
                )

        registry = ActionRegistry()
        registry.register(CustomProvider())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.json"
            path.write_text(
                json.dumps(
                    {
                        "actions": {
                            "custom": {
                                "type": "custom",
                                "parameters": {"value": {"required": True}},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog = load_action_catalog(path, registry=registry)
            execution = execute_actions(catalog, ("custom",), {"value": "provider-ok"})
            self.assertTrue(execution.results[0].ok)
            self.assertEqual(execution.results[0].stdout, "provider-ok")

    def test_parameter_substitution_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(
                Path(tmp),
                {
                    "echo-host": {
                        "description": "echo",
                        "command": ["sh", "-c", "printf '%s' \"$1\"", "sh", "{host}"],
                        "parameters": {
                            "host": {
                                "required": True,
                                "pattern": "^[A-Za-z0-9.:-]+$",
                            }
                        },
                    }
                },
            )
            execution = execute_actions(catalog, ("echo-host",), {"host": "10.0.0.12"})
            self.assertTrue(execution.results[0].ok)
            self.assertEqual(execution.results[0].stdout, "10.0.0.12")

            with self.assertRaises(ActionValidationError):
                execute_actions(catalog, ("echo-host",), {"host": "bad host"})
            with self.assertRaises(ActionValidationError):
                execute_actions(catalog, ("echo-host",), {"other": "value"})

    def test_batch_resolution_deduplicates_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(
                Path(tmp),
                {
                    "one": {"command": ["true"]},
                    "two": {"command": ["true"]},
                },
                {"both": {"actions": ["one", "two", "one"]}},
            )
            self.assertEqual(resolve_action_names(catalog, "both,one"), ("one", "two"))

    def test_timeout_kills_the_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(
                Path(tmp),
                {"slow": {"command": ["sh", "-c", "sleep 2"], "timeoutSeconds": 0.15}},
            )
            started = time.monotonic()
            execution = execute_actions(catalog, ("slow",), {})
            elapsed = time.monotonic() - started
            self.assertTrue(execution.results[0].timed_out)
            self.assertLess(elapsed, 1.5)

    def test_actions_execute_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(
                Path(tmp),
                {
                    "a": {"command": ["sh", "-c", "sleep 0.55"]},
                    "b": {"command": ["sh", "-c", "sleep 0.55"]},
                },
            )
            started = time.monotonic()
            execution = execute_actions(catalog, ("a", "b"), {})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
            self.assertTrue(all(result.ok for result in execution.results))

    def test_large_output_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(
                Path(tmp),
                {"large": {"command": ["sh", "-c", "seq 1 10000"], "timeoutSeconds": 5}},
            )
            execution = execute_actions(catalog, ("large",), {})
            stdout = execution.results[0].stdout
            self.assertLessEqual(len(stdout), 1000)
            self.assertIn("truncated", stdout)

    def test_unknown_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _catalog(Path(tmp), {"one": {"command": ["true"]}})
            with self.assertRaises(ActionValidationError):
                resolve_action_names(catalog, "missing")
            with self.assertRaises(ActionError):
                _catalog(Path(tmp), {"bad": {"command": ["echo", "{missing}"]}})


if __name__ == "__main__":
    unittest.main()
