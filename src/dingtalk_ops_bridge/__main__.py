from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .actions import ActionError, default_action_registry, load_action_catalog
from .checks import MonitorError, load_monitor_catalog
from .commands import (
    format_help,
    handle_prompt,
)
from .monitor import MonitorRunner, MonitorStateStore
from .notifier import DingTalkWebhook, NotifierError
from .paths import actions_file, monitor_state_file, monitors_file


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dingtalk-ops-bridge")
    subparsers = parser.add_subparsers(dest="mode")

    command_parser = subparsers.add_parser("command", help="handle one DingTalk command prompt")
    command_parser.add_argument("prompt", nargs=argparse.REMAINDER)
    command_parser.add_argument("--actions-file", type=Path, default=None)
    command_parser.add_argument("--monitors-file", type=Path, default=None)

    monitor_parser = subparsers.add_parser("monitor", help="run the monitoring scheduler")
    monitor_parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    monitor_parser.add_argument("--dry-run", action="store_true", help="log webhook payloads without sending")
    monitor_parser.add_argument("--name", action="append", default=[], help="limit to a monitor name; repeatable")
    monitor_parser.add_argument("--monitors-file", type=Path, default=None)
    monitor_parser.add_argument("--state-file", type=Path, default=None)

    validate_parser = subparsers.add_parser("validate", help="validate action and monitor configuration")
    validate_parser.add_argument("--actions-file", type=Path, default=None)
    validate_parser.add_argument("--monitors-file", type=Path, default=None)

    webhook_parser = subparsers.add_parser("webhook-test", help="send a test webhook message")
    webhook_parser.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode in {None, "help"}:
        try:
            catalog = load_action_catalog()
            print(format_help(catalog))
        except ActionError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.mode == "command":
        prompt = " ".join(args.prompt)
        try:
            catalog = load_action_catalog(args.actions_file or actions_file())
            print(
                handle_prompt(
                    prompt,
                    catalog,
                    monitors_path=args.monitors_file or monitors_file(),
                )
            )
        except (ActionError, MonitorError) as exc:
            print(
                "\n".join(
                    [
                        "### 服务配置错误",
                        "",
                        f"- 原因：{exc}",
                        "- 请联系管理员检查 `config/actions.json`、`config/monitors.json` 和动作插件目录。",
                    ]
                )
            )
        return 0

    if args.mode == "monitor":
        return _run_monitor(args)

    if args.mode == "validate":
        return _validate(
            args.actions_file,
            args.monitors_file,
        )

    if args.mode == "webhook-test":
        return _webhook_test(dry_run=args.dry_run)

    parser.print_help()
    return 1


def _run_monitor(args: argparse.Namespace) -> int:
    try:
        registry = default_action_registry()
        catalog = load_monitor_catalog(
            args.monitors_file or monitors_file(),
            registry=registry,
        )
        notifier = DingTalkWebhook.from_env(dry_run=args.dry_run)
        store = MonitorStateStore(args.state_file or monitor_state_file())
        runner = MonitorRunner(catalog, store, notifier)
    except (ActionError, MonitorError, NotifierError) as exc:
        LOGGER.error("monitor configuration error: %s", exc)
        return 1

    if args.once:
        try:
            run = runner.run_once(args.name or None)
        except Exception:
            LOGGER.exception("monitor cycle failed")
            return 1
        LOGGER.info(
            "checked %d monitor(s), alerts=%d recoveries=%d",
            len(run.checks),
            run.alerts_sent,
            run.recoveries_sent,
        )
        return 0

    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        LOGGER.info("shutdown requested")
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    runner.run_forever(stop_event)
    return 0


def _validate(
    action_file: Path | None,
    monitor_file: Path | None,
) -> int:
    failed = False
    registry = None
    try:
        action_catalog = load_action_catalog(action_file or actions_file())
        registry = action_catalog.registry
        print(
            f"actions: ok ({len(action_catalog.actions)} actions, "
            f"{len(action_catalog.batches)} batches; "
            f"types: {', '.join(action_catalog.registry.types())}; "
            f"commands: {len(action_catalog.registry.commands())}; "
            f"command-routes: {', '.join(action_catalog.registry.command_prefixes()) or '-'}; "
            f"command-fallback: "
            f"{'yes' if action_catalog.registry.command_fallback() else 'no'}; "
            f"monitor-types: {', '.join(action_catalog.registry.monitor_types()) or '-'}; "
            f"services: {', '.join(sorted(action_catalog.registry.services())) or '-'})"
        )
    except ActionError as exc:
        failed = True
        print(f"actions: failed ({exc})", file=sys.stderr)
    try:
        monitor_catalog = load_monitor_catalog(
            monitor_file or monitors_file(),
            registry=registry,
        )
        enabled = sum(1 for monitor in monitor_catalog.monitors.values() if monitor.enabled)
        print(f"monitors: ok ({len(monitor_catalog.monitors)} configured, {enabled} enabled)")
    except MonitorError as exc:
        failed = True
        print(f"monitors: failed ({exc})", file=sys.stderr)
    webhook = os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
    print(f"webhook: {'configured' if webhook else 'not configured'}")
    return 1 if failed else 0


def _webhook_test(*, dry_run: bool) -> int:
    try:
        notifier = DingTalkWebhook.from_env(dry_run=dry_run)
        notifier.send_markdown(
            "✅ DingTalk Ops Bridge Webhook 测试",
            "### ✅ Webhook 链路测试成功\n\n如果你收到这条消息，说明异常上报通道可用。",
        )
    except NotifierError as exc:
        LOGGER.error("webhook test failed: %s", exc)
        return 1
    print("webhook test sent")
    return 0


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
