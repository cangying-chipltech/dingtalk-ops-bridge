from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .action_api import (
    RESOURCE_CONFIG_DIR,
    ActionContext,
    ActionRegistry,
    MonitorResult,
)
from .paths import config_dir, monitors_file
from .util import ProcessResult, run_process, truncate_text


class MonitorError(Exception):
    """Base error for monitor configuration and execution."""


@dataclass(frozen=True)
class MonitorDefinition:
    name: str
    description: str
    check_type: str
    check: Mapping[str, Any]
    interval_seconds: float = 60.0
    timeout_seconds: float = 10.0
    severity: str = "warning"
    repeat_alert_seconds: float = 0.0
    notify_recovery: bool = True
    enabled: bool = True


@dataclass(frozen=True)
class MonitorSettings:
    max_concurrency: int = 8
    default_interval_seconds: float = 60.0
    max_output_chars: int = 4000


@dataclass(frozen=True)
class MonitorCatalog:
    settings: MonitorSettings
    monitors: Mapping[str, MonitorDefinition]
    registry: ActionRegistry | None = None
    context: ActionContext = field(default_factory=ActionContext)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str
    details: str
    duration_ms: int


def load_monitor_catalog(
    path: Path | None = None,
    *,
    registry: ActionRegistry | None = None,
    context: ActionContext | None = None,
) -> MonitorCatalog:
    source = path or monitors_file()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"monitors file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"invalid JSON in monitors file {source}: {exc}") from exc

    settings_raw = raw.get("settings", {})
    settings = MonitorSettings(
        max_concurrency=_env_positive_int("MONITOR_MAX_CONCURRENCY", settings_raw.get("maxConcurrency", 8)),
        default_interval_seconds=_env_positive_float(
            "MONITOR_DEFAULT_INTERVAL_SECONDS",
            settings_raw.get("defaultIntervalSeconds", 60),
        ),
        max_output_chars=_env_positive_int("MONITOR_MAX_OUTPUT_CHARS", settings_raw.get("maxOutputChars", 4000)),
    )

    monitors_raw = raw.get("monitors", {})
    if not isinstance(monitors_raw, dict):
        raise MonitorError("'monitors' must be an object")

    monitors: dict[str, MonitorDefinition] = {}
    for name, item in monitors_raw.items():
        monitors[name] = _parse_monitor(name, item, settings, registry=registry)

    if registry is not None:
        if context is None:
            context = _plugin_context(registry)
        for provider in registry.monitor_providers():
            try:
                provided = provider.monitors(context)
            except Exception as exc:
                raise MonitorError(
                    f"monitor provider {provider.type_name!r} failed to load definitions: {exc}"
                ) from exc
            if not isinstance(provided, Mapping):
                raise MonitorError(
                    f"monitor provider {provider.type_name!r} must return an object"
                )
            for name, item in provided.items():
                if name in monitors:
                    raise MonitorError(
                        f"duplicate monitor {name!r} from provider {provider.type_name!r}"
                    )
                monitors[name] = _parse_monitor(name, item, settings, registry=registry)

    return MonitorCatalog(
        settings=settings,
        monitors=monitors,
        registry=registry,
        context=context or ActionContext(),
    )


def _parse_monitor(
    name: str,
    item: Any,
    settings: MonitorSettings,
    *,
    registry: ActionRegistry | None,
) -> MonitorDefinition:
    if not isinstance(item, dict):
        raise MonitorError(f"monitor {name!r} must be an object")
    check = item.get("check")
    if not isinstance(check, dict):
        raise MonitorError(f"monitor {name!r} requires a 'check' object")
    check_type = str(check.get("type", "")).lower()
    _validate_check(name, check_type, check, registry)
    severity = str(item.get("severity", "warning")).lower()
    if severity not in {"info", "warning", "critical"}:
        raise MonitorError(f"monitor {name!r} severity must be info, warning, or critical")
    return MonitorDefinition(
        name=name,
        description=str(item.get("description", "")),
        check_type=check_type,
        check=check,
        interval_seconds=_positive_float(
            item.get("intervalSeconds", settings.default_interval_seconds),
            f"monitor {name}.intervalSeconds",
        ),
        timeout_seconds=_positive_float(
            item.get("timeoutSeconds", 10),
            f"monitor {name}.timeoutSeconds",
        ),
        severity=severity,
        repeat_alert_seconds=_non_negative_float(
            item.get("repeatAlertSeconds", 0),
            f"monitor {name}.repeatAlertSeconds",
        ),
        notify_recovery=bool(item.get("notifyRecovery", True)),
        enabled=bool(item.get("enabled", True)),
    )


def _plugin_context(registry: ActionRegistry) -> ActionContext:
    return ActionContext(
        resources={RESOURCE_CONFIG_DIR: str(config_dir())},
        services=registry.services(),
    )


def select_monitors(
    catalog: MonitorCatalog,
    names: Sequence[str] | None = None,
) -> tuple[MonitorDefinition, ...]:
    if not names:
        return tuple(monitor for monitor in catalog.monitors.values() if monitor.enabled)
    selected: list[MonitorDefinition] = []
    seen: set[str] = set()
    for name in names:
        monitor = catalog.monitors.get(name)
        if monitor is None:
            raise MonitorError(f"unknown monitor: {name}")
        if name not in seen:
            selected.append(monitor)
            seen.add(name)
    return tuple(selected)


def execute_checks(
    catalog: MonitorCatalog,
    monitors: Sequence[MonitorDefinition],
) -> tuple[tuple[MonitorDefinition, CheckResult], ...]:
    if not monitors:
        return ()
    workers = max(1, min(catalog.settings.max_concurrency, len(monitors)))
    results: list[tuple[MonitorDefinition, CheckResult] | None] = [None] * len(monitors)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="monitor") as pool:
        future_indexes = {
            pool.submit(
                run_check,
                monitor,
                catalog.settings.max_output_chars,
                registry=catalog.registry,
                context=catalog.context,
            ): index
            for index, monitor in enumerate(monitors)
        }
        for future in as_completed(future_indexes):
            index = future_indexes[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - defensive boundary
                result = CheckResult(False, f"check crashed: {exc}", "", 0)
            results[index] = (monitors[index], result)
    return tuple(item for item in results if item is not None)


def run_check(
    monitor: MonitorDefinition,
    max_output_chars: int = 4000,
    *,
    registry: ActionRegistry | None = None,
    context: ActionContext | None = None,
) -> CheckResult:
    started = time.monotonic()
    try:
        if monitor.check_type == "http":
            result = _run_http_check(monitor)
        elif monitor.check_type == "tcp":
            result = _run_tcp_check(monitor)
        elif monitor.check_type == "command":
            result = _run_command_check(monitor, max_output_chars)
        elif registry is not None and registry.has_monitor_provider(monitor.check_type):
            provider = registry.monitor_provider(monitor.check_type)
            plugin_result = provider.check(monitor.check, context or ActionContext())
            if not isinstance(plugin_result, MonitorResult):
                raise TypeError(
                    f"monitor provider {provider.type_name!r} must return MonitorResult"
                )
            result = CheckResult(
                plugin_result.ok,
                plugin_result.message,
                plugin_result.details,
                0,
            )
        else:
            result = CheckResult(False, f"unsupported check type: {monitor.check_type}", "", 0)
    except Exception as exc:
        result = CheckResult(False, f"check error: {exc}", f"type={monitor.check_type}", 0)
    elapsed = int((time.monotonic() - started) * 1000)
    return CheckResult(result.ok, result.message, result.details, elapsed)


def _run_http_check(monitor: MonitorDefinition) -> CheckResult:
    url = str(monitor.check["url"])
    method = str(monitor.check.get("method", "GET")).upper()
    headers = {str(key): str(value) for key, value in (monitor.check.get("headers") or {}).items()}
    expected_status = _as_int_set(monitor.check.get("expectedStatus", [200]))
    body_contains = _as_string_list(monitor.check.get("bodyContains", []))
    request = urllib.request.Request(url, method=method, headers=headers)

    status: int
    body: bytes
    try:
        with urllib.request.urlopen(request, timeout=monitor.timeout_seconds) as response:
            status = int(response.status)
            body = response.read(65536)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read(65536)
        charset = exc.headers.get_content_charset() if exc.headers else "utf-8"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return CheckResult(False, f"HTTP request failed: {exc}", f"url={_safe_url(url)}", 0)

    text = body.decode(charset or "utf-8", errors="replace")
    failures: list[str] = []
    if status not in expected_status:
        failures.append(f"status {status} not in {sorted(expected_status)}")
    for expected_text in body_contains:
        if expected_text not in text:
            failures.append(f"response does not contain {expected_text!r}")

    details = f"url={_safe_url(url)} status={status} bytes={len(body)}"
    if failures:
        return CheckResult(False, "; ".join(failures), details, 0)
    return CheckResult(True, f"HTTP {status} matched expectations", details, 0)


def _run_tcp_check(monitor: MonitorDefinition) -> CheckResult:
    host = str(monitor.check["host"])
    port = int(monitor.check["port"])
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=monitor.timeout_seconds):
            elapsed = int((time.monotonic() - started) * 1000)
            return CheckResult(True, f"TCP connect succeeded in {elapsed}ms", f"{host}:{port}", 0)
    except OSError as exc:
        return CheckResult(False, f"TCP connect failed: {exc}", f"{host}:{port}", 0)


def _run_command_check(monitor: MonitorDefinition, max_output_chars: int) -> CheckResult:
    command = monitor.check.get("command")
    if not isinstance(command, list) or not command:
        return CheckResult(False, "command check is empty", "", 0)
    expected_codes = _as_int_set(monitor.check.get("expectedExitCodes", [0]))
    stdout_contains = _as_string_list(monitor.check.get("stdoutContains", []))
    stderr_contains = _as_string_list(monitor.check.get("stderrContains", []))
    env = {str(key): str(value) for key, value in (monitor.check.get("env") or {}).items()}
    process: ProcessResult = run_process(
        [str(part) for part in command],
        timeout_seconds=monitor.timeout_seconds,
        cwd=monitor.check.get("workingDir"),
        env=env,
        max_output_chars=max_output_chars,
    )
    if process.timed_out:
        return CheckResult(False, f"command timed out after {monitor.timeout_seconds}s", _command_text(command), 0)

    failures: list[str] = []
    if process.exit_code not in expected_codes:
        failures.append(f"exit code {process.exit_code} not in {sorted(expected_codes)}")
    for expected_text in stdout_contains:
        if expected_text not in process.stdout:
            failures.append(f"stdout does not contain {expected_text!r}")
    for expected_text in stderr_contains:
        if expected_text not in process.stderr:
            failures.append(f"stderr does not contain {expected_text!r}")
    details = f"command={_command_text(command)} exit={process.exit_code}"
    if process.stderr.strip():
        details += f" stderr={truncate_text(process.stderr.strip(), 500)!r}"
    if failures:
        return CheckResult(False, "; ".join(failures), details, 0)
    return CheckResult(True, f"command exited {process.exit_code}", details, 0)


def _validate_check(
    name: str,
    check_type: str,
    check: Mapping[str, Any],
    registry: ActionRegistry | None,
) -> None:
    if check_type == "http":
        if not check.get("url"):
            raise MonitorError(f"monitor {name!r} HTTP check requires 'url'")
        return
    if check_type == "tcp":
        if not check.get("host"):
            raise MonitorError(f"monitor {name!r} TCP check requires 'host'")
        try:
            port = int(check.get("port"))
        except (TypeError, ValueError) as exc:
            raise MonitorError(f"monitor {name!r} TCP check requires a numeric 'port'") from exc
        if not 1 <= port <= 65535:
            raise MonitorError(f"monitor {name!r} TCP port must be between 1 and 65535")
        return
    if check_type == "command":
        command = check.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise MonitorError(f"monitor {name!r} command check requires a non-empty string array 'command'")
        return
    if registry is not None and registry.has_monitor_provider(check_type):
        provider = registry.monitor_provider(check_type)
        try:
            provider.validate(check)
        except Exception as exc:
            raise MonitorError(
                f"monitor {name!r} failed validation in provider {provider.type_name!r}: {exc}"
            ) from exc
        return
    available = ", ".join(registry.monitor_types()) if registry is not None else "-"
    raise MonitorError(
        f"monitor {name!r} has unsupported check type: {check_type!r}; "
        f"plugin monitor providers: {available or '-'}"
    )


def _safe_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _as_int_set(value: Any) -> set[int]:
    values = value if isinstance(value, list) else [value]
    return {int(item) for item in values}


def _as_string_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item) for item in values if str(item)]


def _command_text(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command)


def _positive_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"{label} must be a number") from exc
    if parsed <= 0:
        raise MonitorError(f"{label} must be greater than zero")
    return parsed


def _non_negative_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"{label} must be a number") from exc
    if parsed < 0:
        raise MonitorError(f"{label} must not be negative")
    return parsed


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise MonitorError(f"{label} must be greater than zero")
    return parsed


def _env_positive_int(name: str, default: Any) -> int:
    return _positive_int(os.getenv(name, default), name)


def _env_positive_float(name: str, default: Any) -> float:
    return _positive_float(os.getenv(name, default), name)
