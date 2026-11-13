from __future__ import annotations

import shlex
from pathlib import Path
from typing import Mapping, Sequence

from .action_api import (
    RESERVED_COMMAND_PREFIXES,
    RESOURCE_CONFIG_DIR,
    ActionContext,
    ActionRegistry,
)
from .actions import (
    ActionCatalog,
    ActionError,
    ActionExecution,
    ActionResult,
    execute_actions,
    resolve_action_names,
)
from .checks import (
    CheckResult,
    MonitorCatalog,
    MonitorDefinition,
    MonitorError,
    execute_checks,
    load_monitor_catalog,
    select_monitors,
)
from .paths import config_dir
from .util import code_block, duration_text, truncate_text


HELP_COMMANDS = {"help", "?", "/help", "h", "帮助"}
LIST_COMMANDS = {"list", "actions", "动作", "列表"}
RUN_COMMANDS = {"run", "exec", "execute", "执行"}
BATCH_COMMANDS = {"batch", "批处理"}
MONITOR_COMMANDS = {"monitor", "监控"}
STATUS_COMMANDS = {"status", "check", "状态", "检查"}
PROMPT_PREFIXES = {"dingtalk-ops", "ops"}


def handle_prompt(
    prompt: str,
    action_catalog: ActionCatalog,
    *,
    monitors_path: Path | None = None,
) -> str:
    normalized = _normalize_prompt(prompt)
    if not normalized:
        return format_help(action_catalog)

    parts = normalized.split(maxsplit=1)
    command = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if command not in RESERVED_COMMAND_PREFIXES:
        plugin_output = _handle_plugin_command(normalized, action_catalog)
        if plugin_output is not None:
            return plugin_output

    if command in HELP_COMMANDS:
        return format_help(action_catalog)
    if command in LIST_COMMANDS:
        return format_action_list(action_catalog)
    if command in RUN_COMMANDS:
        return _handle_run(rest, action_catalog)
    if command in BATCH_COMMANDS:
        return _handle_batch(rest, action_catalog)
    if command in MONITOR_COMMANDS:
        return _handle_monitor(rest, monitors_path, action_catalog.registry)
    if command == "bridge":
        return _handle_bridge(rest, monitors_path, action_catalog.registry)
    if command in STATUS_COMMANDS:
        return _handle_monitor(
            f"check {rest}".strip(),
            monitors_path,
            action_catalog.registry,
        )
    return "\n".join(
        [
            f"### 未知命令：`{_inline(command)}`",
            "",
            "使用 `help` 查看可用命令，或直接执行 `status` 检查全部监控项。",
        ]
    )


def format_help(catalog: ActionCatalog) -> str:
    actions = ", ".join(f"`{name}`" for name in sorted(catalog.actions))
    batches = ", ".join(f"`{name}`" for name in sorted(catalog.batches)) or "-"
    plugin_help = _plugin_help_lines(catalog)
    commands = [
        "- `help`：显示帮助",
        "- `list`：列出动作与批处理",
        "- `run <动作>[,<动作>] [key=value ...]`：并发执行动作",
        "- `batch <批处理> [key=value ...]`：并发执行预置动作组",
        "- `monitor list`：列出监控项",
        "- `monitor check [名称,...]`：立即执行监控检查",
        "- `check`：立即执行全部已启用监控",
        "- `bridge status`：显式执行框架监控检查，插件无法占用",
    ]
    if catalog.registry.command_route("status") is None:
        commands.append("- `status`：立即执行全部已启用监控")
    return "\n".join(
        [
            "### DingTalk Ops Bridge",
            "",
            "接收钉钉指令，并发执行预置动作，并返回汇总结果。",
            "",
            "**命令**",
            *commands,
            *(["", "**插件命令**", *plugin_help] if plugin_help else []),
            "",
            f"**动作**：{actions}",
            f"**批处理**：{batches}",
            "",
            "**示例**",
            "```text",
            "run disk,memory,uptime",
            "run node-login node=web-01",
            "batch node-health node=web-01",
            "run ping host=10.0.0.12",
            "batch health",
            "monitor check api-health,redis",
            "check",
            "```",
        ]
    )


def _plugin_help_lines(catalog: ActionCatalog) -> list[str]:
    lines: list[str] = []
    for provider in catalog.registry.commands():
        getter = getattr(provider, "help_lines", None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except Exception:
            continue
        if isinstance(value, str):
            lines.append(value)
        else:
            lines.extend(str(line) for line in value)
    return lines


def format_action_list(catalog: ActionCatalog) -> str:
    lines = ["### 可用动作", "", "| 名称 | 类型 | 说明 | 参数 |", "| --- | --- | --- | --- |"]
    for name, action in sorted(catalog.actions.items()):
        parameters = ", ".join(
            f"`{parameter.name}`{'*' if parameter.required else ''}"
            for parameter in action.parameters.values()
        ) or "-"
        lines.append(
            f"| `{name}` | `{action.action_type}` | {_table(action.description or '-')} | {parameters} |"
        )

    if catalog.batches:
        lines.extend(["", "### 批处理", "", "| 名称 | 动作 | 说明 |", "| --- | --- | --- |"])
        for name, batch in sorted(catalog.batches.items()):
            actions = ", ".join(f"`{item}`" for item in batch.actions)
            lines.append(f"| `{name}` | {actions} | {_table(batch.description or '-')} |")
    lines.extend(["", "带 `*` 的参数为必填；参数格式为 `key=value`。"])
    return "\n".join(lines)


def format_action_execution(execution: ActionExecution, max_reply_chars: int) -> str:
    ok_count = sum(1 for result in execution.results if result.ok)
    failed_count = len(execution.results) - ok_count
    lines = [
        "### 动作执行结果",
        "",
        f"- 总数：**{len(execution.results)}**",
        f"- 成功：**{ok_count}**",
        f"- 失败：**{failed_count}**",
        f"- 总耗时：**{duration_text(execution.elapsed_ms)}**",
    ]
    for result in execution.results:
        lines.extend(["", *_format_action_result(result)])
    return truncate_text("\n".join(lines), max_reply_chars)


def format_monitor_checks(
    checks: Sequence[tuple[MonitorDefinition, CheckResult]],
    max_reply_chars: int = 12000,
) -> str:
    if not checks:
        return "### 监控检查\n\n没有可执行的监控项。"
    ok_count = sum(1 for _, result in checks if result.ok)
    lines = [
        "### 监控检查结果",
        "",
        f"- 总数：**{len(checks)}**",
        f"- 正常：**{ok_count}**",
        f"- 异常：**{len(checks) - ok_count}**",
    ]
    for monitor, result in checks:
        icon = "✅" if result.ok else "❌"
        lines.extend(
            [
                "",
                f"#### {icon} {monitor.name}",
                "",
                f"- 级别：**{monitor.severity}**",
                f"- 类型：`{monitor.check_type}`",
                f"- 结果：{result.message}",
                f"- 详情：`{_inline(result.details or '-')}`",
                f"- 耗时：**{duration_text(result.duration_ms)}**",
            ]
        )
    return truncate_text("\n".join(lines), max_reply_chars)


def format_monitor_list(catalog: MonitorCatalog) -> str:
    if not catalog.monitors:
        return "### 监控项\n\n尚未配置监控项。"
    lines = ["### 监控项", "", "| 名称 | 类型 | 级别 | 间隔 | 启用 |", "| --- | --- | --- | --- | --- |"]
    for name, monitor in sorted(catalog.monitors.items()):
        lines.append(
            f"| `{name}` | `{monitor.check_type}` | {monitor.severity} | "
            f"{duration_text(int(monitor.interval_seconds * 1000))} | "
            f"{'是' if monitor.enabled else '否'} |"
        )
    return "\n".join(lines)


def _handle_run(
    rest: str,
    catalog: ActionCatalog,
) -> str:
    try:
        tokens = _split(rest)
        if not tokens:
            return _usage_error("`run` 需要动作名称，例如 `run disk,uptime`。")
        action_names = resolve_action_names(catalog, tokens[0])
        parameters = _parse_parameters(tokens[1:])
        execution = execute_actions(
            catalog,
            action_names,
            parameters,
            context=_action_context(catalog),
        )
        return format_action_execution(execution, catalog.settings.max_reply_chars)
    except ActionError as exc:
        return _usage_error(str(exc))


def _handle_batch(
    rest: str,
    catalog: ActionCatalog,
) -> str:
    try:
        tokens = _split(rest)
        if not tokens:
            return _usage_error("`batch` 需要批处理名称，例如 `batch health`。")
        batch = catalog.batches.get(tokens[0])
        if batch is None:
            raise ActionError(f"unknown batch: {tokens[0]}")
        parameters = _parse_parameters(tokens[1:])
        execution = execute_actions(
            catalog,
            batch.actions,
            parameters,
            context=_action_context(catalog),
        )
        return format_action_execution(execution, catalog.settings.max_reply_chars)
    except ActionError as exc:
        return _usage_error(str(exc))


def _handle_monitor(
    rest: str,
    monitors_path: Path | None,
    registry: ActionRegistry | None = None,
) -> str:
    try:
        catalog = load_monitor_catalog(monitors_path, registry=registry)
        tokens = _split(rest)
        subcommand = tokens[0].lower() if tokens else "check"
        if subcommand in {"list", "列表"}:
            return format_monitor_list(catalog)
        if subcommand not in {"check", "run", "检查", "执行"}:
            return _usage_error("monitor 支持 `list` 或 `check [名称,...]`。")
        names: list[str] = []
        for token in tokens[1:]:
            names.extend(part.strip() for part in token.split(",") if part.strip())
        monitors = select_monitors(catalog, names)
        checks = execute_checks(catalog, monitors)
        return format_monitor_checks(checks)
    except (ActionError, MonitorError, ValueError) as exc:
        return _usage_error(str(exc))


def _handle_bridge(
    rest: str,
    monitors_path: Path | None,
    registry: ActionRegistry | None = None,
) -> str:
    tokens = _split(rest)
    subcommand = tokens[0].lower() if tokens else "status"
    if subcommand not in {"status", "check", "状态", "检查"}:
        return _usage_error("bridge 支持 `bridge status` 或 `bridge check`。")
    return _handle_monitor("check", monitors_path, registry)


def _normalize_prompt(prompt: str) -> str:
    normalized = (prompt or "").strip()
    if not normalized:
        return ""
    parts = normalized.split(maxsplit=1)
    if parts[0].lower() in PROMPT_PREFIXES:
        return parts[1].strip() if len(parts) > 1 else ""
    return normalized


def _handle_plugin_command(
    prompt: str,
    catalog: ActionCatalog,
) -> str | None:
    context = _action_context(catalog)
    registration = catalog.registry.command_route(prompt)
    required = registration is not None
    if registration is None:
        registration = catalog.registry.command_fallback()
        if registration is None:
            return None
    try:
        result = registration.provider.handle(prompt, context)
    except ActionError as exc:
        return _usage_error(str(exc))
    except Exception as exc:
        return _usage_error(f"插件命令执行失败：{exc}")
    if result is None and required:
        prefix = prompt.strip().split(maxsplit=1)[0]
        return _usage_error(f"插件命令 `{prefix}` 未返回结果，请检查插件配置。")
    return result


def _action_context(catalog: ActionCatalog) -> ActionContext:
    return ActionContext(
        resources={RESOURCE_CONFIG_DIR: str(config_dir())},
        services=catalog.registry.services(),
    )


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise ActionError(f"cannot parse command: {exc}") from exc


def _parse_parameters(tokens: Sequence[str]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise ActionError(f"invalid parameter {token!r}; expected key=value")
        key, value = token.split("=", 1)
        if not key:
            raise ActionError(f"invalid parameter {token!r}; key must not be empty")
        parameters[key] = value
    return parameters


def _format_action_result(result: ActionResult) -> list[str]:
    icon = "✅" if result.ok else "❌"
    lines = [
        f"#### {icon} {result.name}",
        "",
        f"- 状态：**{result.status}**",
        f"- 退出码：`{result.exit_code}`",
        f"- 耗时：**{duration_text(result.duration_ms)}**",
        f"- 命令：`{_inline(' '.join(result.command))}`",
    ]
    if result.error:
        lines.append(f"- 错误：{result.error}")
    if result.stdout.strip():
        lines.extend(["", "**输出**", "", code_block(result.stdout)])
    if result.stderr.strip():
        lines.extend(["", "**错误输出**", "", code_block(result.stderr)])
    if not result.stdout.strip() and not result.stderr.strip() and not result.error:
        lines.extend(["", "(no output)"])
    return lines


def _usage_error(message: str) -> str:
    return "\n".join(
        [
            "### 命令未执行",
            "",
            f"- 原因：{message}",
            "- 帮助：发送 `help` 查看命令和参数。",
        ]
    )


def _inline(text: str) -> str:
    return text.replace("`", "'").replace("\n", " ")


def _table(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
