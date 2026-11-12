from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from dingtalk_ops_bridge.checks import CheckResult, MonitorCatalog, MonitorDefinition, MonitorSettings
from dingtalk_ops_bridge.monitor import (
    MonitorRunner,
    MonitorStateStore,
    should_alert,
    should_recover,
)
from dingtalk_ops_bridge.util import isoformat, utc_now


class _FakeNotifier:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send_markdown(self, title: str, text: str) -> None:
        self.messages.append((title, text))


def _catalog(monitor: MonitorDefinition) -> MonitorCatalog:
    return MonitorCatalog(settings=MonitorSettings(), monitors={monitor.name: monitor})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = MonitorDefinition(
            name="api",
            description="api health",
            check_type="http",
            check={"type": "http", "url": "https://example.com"},
            repeat_alert_seconds=300,
            severity="critical",
        )

    def test_alert_and_recovery_transitions(self):
        now = utc_now()
        failed = CheckResult(False, "boom", "status=500", 10)
        ok = CheckResult(True, "healthy", "status=200", 10)
        self.assertTrue(should_alert({}, failed, self.monitor, now))
        self.assertFalse(should_recover({}, failed, self.monitor))
        self.assertFalse(should_alert({"status": "failed", "lastAlertAt": isoformat(now)}, failed, self.monitor, now))
        later = now + timedelta(seconds=301)
        self.assertTrue(should_alert({"status": "failed", "lastAlertAt": isoformat(now)}, failed, self.monitor, later))
        self.assertTrue(should_recover({"status": "failed"}, ok, self.monitor))

    def test_state_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = MonitorStateStore(path)
            state = {"version": 1, "monitors": {"api": {"status": "failed"}}}
            store.save(state)
            self.assertEqual(store.load(), state)

    def test_runner_sends_alert_then_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MonitorStateStore(Path(tmp) / "state.json")
            notifier = _FakeNotifier()
            runner = MonitorRunner(_catalog(self.monitor), store, notifier)  # type: ignore[arg-type]
            failure = ((self.monitor, CheckResult(False, "boom", "status=500", 5)),)
            recovery = ((self.monitor, CheckResult(True, "healthy", "status=200", 5)),)
            with mock.patch("dingtalk_ops_bridge.monitor.execute_checks", return_value=failure):
                run = runner.run_once()
            self.assertEqual(run.alerts_sent, 1)
            with mock.patch("dingtalk_ops_bridge.monitor.execute_checks", return_value=recovery):
                run = runner.run_once()
            self.assertEqual(run.recoveries_sent, 1)
            self.assertEqual(len(notifier.messages), 2)
            self.assertEqual(store.load()["monitors"]["api"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
