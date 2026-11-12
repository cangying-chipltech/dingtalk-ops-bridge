from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .checks import CheckResult, MonitorCatalog, MonitorDefinition, execute_checks, select_monitors
from .notifier import DingTalkWebhook, NotifierError
from .paths import monitor_state_file
from .util import code_block, duration_text, isoformat, parse_iso, utc_now


LOGGER = logging.getLogger(__name__)


class MonitorStateError(Exception):
    """Raised when monitor state cannot be read or written."""


@dataclass(frozen=True)
class MonitorRun:
    checks: tuple[tuple[MonitorDefinition, CheckResult], ...]
    alerts_sent: int
    recoveries_sent: int


class MonitorStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or monitor_state_file()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "monitors": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MonitorStateError(f"cannot read monitor state {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise MonitorStateError(f"monitor state {self.path} must contain a JSON object")
        raw.setdefault("version", 1)
        raw.setdefault("monitors", {})
        if not isinstance(raw["monitors"], dict):
            raise MonitorStateError(f"monitor state {self.path} has invalid 'monitors'")
        return raw

    def save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise MonitorStateError(f"cannot write monitor state {self.path}: {exc}") from exc


class MonitorRunner:
    def __init__(
        self,
        catalog: MonitorCatalog,
        store: MonitorStateStore,
        notifier: DingTalkWebhook,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.notifier = notifier

    def run_once(self, names: Sequence[str] | None = None) -> MonitorRun:
        monitors = select_monitors(self.catalog, names)
        checks = execute_checks(self.catalog, monitors)
        state = self.store.load()
        state_monitors = state["monitors"]
        now = utc_now()
        alerts_sent = 0
        recoveries_sent = 0

        for monitor, result in checks:
            previous = state_monitors.get(monitor.name, {})
            previous_status = str(previous.get("status", "unknown"))
            current_status = "ok" if result.ok else "failed"
            last_alert_at = previous.get("lastAlertAt")
            since = previous.get("since") if previous_status == current_status else isoformat(now)

            if should_alert(previous, result, monitor, now):
                title, text = format_alert(monitor, result)
                if self._send(title, text):
                    last_alert_at = isoformat(now)
                    alerts_sent += 1

            if should_recover(previous, result, monitor):
                title, text = format_recovery(monitor, result)
                if self._send(title, text):
                    recoveries_sent += 1

            state_monitors[monitor.name] = {
                "status": current_status,
                "since": since,
                "lastCheckAt": isoformat(now),
                "lastAlertAt": last_alert_at,
                "severity": monitor.severity,
                "message": result.message,
                "details": result.details,
            }

        self.store.save(state)
        return MonitorRun(tuple(checks), alerts_sent, recoveries_sent)

    def run_forever(self, stop_event: threading.Event) -> None:
        next_due: dict[str, float] = {name: 0.0 for name in self.catalog.monitors if self.catalog.monitors[name].enabled}
        LOGGER.info("monitor scheduler started with %d enabled monitor(s)", len(next_due))
        while not stop_event.is_set():
            now = time.monotonic()
            due = [name for name, deadline in next_due.items() if deadline <= now]
            if due:
                started = time.monotonic()
                try:
                    run = self.run_once(due)
                    LOGGER.info(
                        "checked %d monitor(s), alerts=%d recoveries=%d",
                        len(run.checks),
                        run.alerts_sent,
                        run.recoveries_sent,
                    )
                except Exception:
                    LOGGER.exception("monitor cycle failed")
                completed = time.monotonic()
                for name in due:
                    next_due[name] = completed + self.catalog.monitors[name].interval_seconds
                LOGGER.debug("monitor cycle took %s", duration_text(int((completed - started) * 1000)))

            if not next_due:
                stop_event.wait(30.0)
                continue
            wake_after = max(0.1, min(next_due.values()) - time.monotonic())
            stop_event.wait(min(wake_after, 5.0))

    def _send(self, title: str, text: str) -> bool:
        try:
            self.notifier.send_markdown(title, text)
            return True
        except NotifierError as exc:
            LOGGER.error("webhook notification failed: %s", exc)
            return False


def should_alert(
    previous: Mapping[str, Any],
    result: CheckResult,
    monitor: MonitorDefinition,
    now: datetime,
) -> bool:
    if result.ok:
        return False
    if str(previous.get("status", "unknown")) != "failed":
        return True
    last_alert_at = parse_iso(previous.get("lastAlertAt"))
    if last_alert_at is None:
        return True
    if monitor.repeat_alert_seconds <= 0:
        return False
    return now - last_alert_at >= timedelta(seconds=monitor.repeat_alert_seconds)


def should_recover(previous: Mapping[str, Any], result: CheckResult, monitor: MonitorDefinition) -> bool:
    return bool(result.ok and monitor.notify_recovery and str(previous.get("status", "unknown")) == "failed")


def format_alert(monitor: MonitorDefinition, result: CheckResult) -> tuple[str, str]:
    icon = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(monitor.severity, "⚠️")
    title = f"{icon} 监控异常：{monitor.name}"
    text = "\n".join(
        [
            f"### {icon} 监控异常：{monitor.name}",
            "",
            f"- 级别：**{monitor.severity}**",
            f"- 类型：`{monitor.check_type}`",
            f"- 说明：{monitor.description or '-'}",
            f"- 结果：{result.message}",
            f"- 详情：`{result.details or '-'}`",
            f"- 检查耗时：{duration_text(result.duration_ms)}",
            f"- 时间：{isoformat(utc_now())}",
            "",
            code_block(result.details or result.message, limit=1200),
        ]
    )
    return title, text


def format_recovery(monitor: MonitorDefinition, result: CheckResult) -> tuple[str, str]:
    title = f"✅ 监控恢复：{monitor.name}"
    text = "\n".join(
        [
            f"### ✅ 监控恢复：{monitor.name}",
            "",
            f"- 级别：**{monitor.severity}**",
            f"- 类型：`{monitor.check_type}`",
            f"- 结果：{result.message}",
            f"- 时间：{isoformat(utc_now())}",
        ]
    )
    return title, text
