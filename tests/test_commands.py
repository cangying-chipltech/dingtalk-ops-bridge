from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


os.environ["ACTION_PLUGIN_DIR"] = str(
    Path(tempfile.gettempdir()) / "dingtalk-ops-bridge-tests-no-plugins"
)

from dingtalk_ops_bridge.actions import load_action_catalog
from dingtalk_ops_bridge.commands import handle_prompt


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        action_path = root / "actions.json"
        action_path.write_text(
            json.dumps(
                {
                    "settings": {"maxConcurrency": 2, "maxOutputChars": 1000, "maxReplyChars": 5000},
                    "actions": {
                        "echo": {"description": "echo", "command": ["printf", "hello"]},
                        "fail": {"description": "fail", "command": ["sh", "-c", "exit 7"]},
                    },
                    "batches": {
                        "both": {"description": "both", "actions": ["echo", "fail"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.monitors_path = root / "monitors.json"
        self.monitors_path.write_text(
            json.dumps(
                {
                    "settings": {"maxConcurrency": 2},
                    "monitors": {
                        "ok-check": {
                            "description": "ok",
                            "check": {"type": "command", "command": ["true"]},
                            "enabled": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.catalog = load_action_catalog(action_path)

    def tearDown(self):
        self.temp.cleanup()

    def _handle(self, prompt: str) -> str:
        return handle_prompt(prompt, self.catalog, monitors_path=self.monitors_path)

    def test_help_and_list(self):
        help_output = self._handle("help")
        self.assertIn("DingTalk Ops Bridge", help_output)
        self.assertIn("- `status`：立即执行全部已启用监控", help_output)
        self.assertIn("- `bridge status`：显式执行框架监控检查", help_output)
        self.assertNotIn("插件未接管时执行监控检查", help_output)
        self.assertIn("`echo`", self._handle("list"))
        self.assertIn("`both`", self._handle("actions"))

    def test_run_and_batch(self):
        run_output = self._handle("run echo")
        self.assertIn("成功：**1**", run_output)
        self.assertIn("hello", run_output)
        batch_output = self._handle("BATCH both")
        self.assertIn("成功：**1**", batch_output)
        self.assertIn("失败：**1**", batch_output)

    def test_monitor_list_and_check(self):
        self.assertIn("`ok-check`", self._handle("monitor list"))
        output = self._handle("status")
        self.assertIn("监控检查结果", output)
        self.assertIn("✅ ok-check", output)
        bridge_output = self._handle("bridge status")
        self.assertIn("监控检查结果", bridge_output)
        self.assertIn("✅ ok-check", bridge_output)

    def test_unknown_and_invalid_parameter(self):
        self.assertIn("未知命令", self._handle("nope"))
        self.assertIn("invalid parameter", self._handle("run echo bad"))
        self.assertIn("unknown action", self._handle("run missing"))

    def test_optional_prefix_and_empty_prompt(self):
        self.assertIn("DingTalk Ops Bridge", self._handle("ops help"))
        self.assertIn("DingTalk Ops Bridge", self._handle(""))


if __name__ == "__main__":
    unittest.main()
