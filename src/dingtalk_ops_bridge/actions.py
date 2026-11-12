from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .action_api import (
    ActionContext,
    ActionDefinition,
    ActionError,
    ActionExecution,
    ActionRegistry,
    ActionValidationError,
    ActionResult,
    ParameterDefinition,
    PreparedAction,
)
from .action_types import build_default_registry
from .paths import actions_file
from .util import ProcessResult, run_process


ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DEFAULT_REGISTRY: ActionRegistry | None = None


@dataclass(frozen=True)
class BatchDefinition:
    name: str
    description: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class ActionSettings:
    max_concurrency: int = 8
    max_output_chars: int = 3600
    max_reply_chars: int = 12000


@dataclass(frozen=True)
class ActionCatalog:
    settings: ActionSettings
    actions: Mapping[str, ActionDefinition]
    batches: Mapping[str, BatchDefinition]
    registry: ActionRegistry = field(default_factory=lambda: default_action_registry())


def default_action_registry() -> ActionRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def load_action_catalog(
    path: Path | None = None,
    registry: ActionRegistry | None = None,
) -> ActionCatalog:
    source = path or actions_file()
    registry = registry or default_action_registry()
    raw = _read_action_source(source)

    settings_raw = raw.get("settings", {})
    if not isinstance(settings_raw, dict):
        raise ActionError("'settings' must be an object")
    settings = ActionSettings(
        max_concurrency=_env_positive_int("ACTION_MAX_CONCURRENCY", settings_raw.get("maxConcurrency", 8)),
        max_output_chars=_env_positive_int(
            "ACTION_MAX_OUTPUT_CHARS", settings_raw.get("maxOutputChars", 3600)
        ),
        max_reply_chars=_env_positive_int("COMMAND_MAX_REPLY_CHARS", settings_raw.get("maxReplyChars", 12000)),
    )

    sources = [(source, raw)]
    sources.extend(
        (plugin_source, _read_action_source(plugin_source))
        for plugin_source in registry.action_files()
    )
    actions: dict[str, ActionDefinition] = {}
    for action_source, action_raw in sources:
        _parse_actions(action_source, action_raw, registry, actions, required=action_source == source)

    batches: dict[str, BatchDefinition] = {}
    for _, action_raw in sources:
        _parse_batches(action_raw, actions, batches)

    return ActionCatalog(settings=settings, actions=actions, batches=batches, registry=registry)


def _read_action_source(source: Path) -> dict[str, Any]:
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ActionError(f"actions file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ActionError(f"invalid JSON in actions file {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ActionError(f"actions file {source} must contain a JSON object")
    return raw


def _parse_actions(
    source: Path,
    raw: Mapping[str, Any],
    registry: ActionRegistry,
    actions: dict[str, ActionDefinition],
    *,
    required: bool,
) -> None:
    actions_raw = raw.get("actions")
    if required and (not isinstance(actions_raw, dict) or not actions_raw):
        raise ActionError("actions file must define a non-empty 'actions' object")
    if actions_raw is None:
        actions_raw = {}
    if not isinstance(actions_raw, dict):
        raise ActionError(f"actions file {source} 'actions' must be an object")
    if not actions_raw and not raw.get("batches"):
        raise ActionError(f"actions file {source} defines no actions or batches")

    for name, item in actions_raw.items():
        if name in actions:
            raise ActionError(f"duplicate action {name!r} in {source}")
        if not isinstance(item, dict):
            raise ActionError(f"action {name!r} must be an object")
        action_type = str(item.get("type", "command")).strip().lower()
        provider = registry.get(action_type)

        parameters: dict[str, ParameterDefinition] = {}
        parameters_raw = item.get("parameters") or {}
        if not isinstance(parameters_raw, dict):
            raise ActionError(f"action {name!r} 'parameters' must be an object")
        for param_name, param_raw in parameters_raw.items():
            if not isinstance(param_raw, dict):
                raise ActionError(f"parameter {name}.{param_name} must be an object")
            pattern_text = param_raw.get("pattern")
            try:
                pattern = re.compile(pattern_text) if pattern_text else None
            except re.error as exc:
                raise ActionError(f"invalid pattern for {name}.{param_name}: {exc}") from exc
            parameters[param_name] = ParameterDefinition(
                name=param_name,
                required=bool(param_raw.get("required", False)),
                default=param_raw.get("default"),
                pattern=pattern,
                description=str(param_raw.get("description", "")),
                kind=str(param_raw.get("type", "string")).lower(),
            )

        working_dir = item.get("workingDir")
        if working_dir:
            working_dir = str((source.parent / str(working_dir)).resolve())
        env_raw = item.get("env") or {}
        if not isinstance(env_raw, dict):
            raise ActionError(f"action {name!r} 'env' must be an object")

        definition = ActionDefinition(
            name=name,
            description=str(item.get("description", "")),
            action_type=action_type,
            parameters=parameters,
            timeout_seconds=_positive_float(item.get("timeoutSeconds", 30), f"action {name}.timeoutSeconds"),
            working_dir=working_dir,
            env={str(key): str(value) for key, value in env_raw.items()},
            config=dict(item),
        )
        provider.validate(definition)
        actions[name] = definition


def _parse_batches(
    raw: Mapping[str, Any],
    actions: Mapping[str, ActionDefinition],
    batches: dict[str, BatchDefinition],
) -> None:
    batches_raw = raw.get("batches") or {}
    if not isinstance(batches_raw, dict):
        raise ActionError("'batches' must be an object")
    for name, item in batches_raw.items():
        if name in batches:
            raise ActionError(f"duplicate batch: {name}")
        if not isinstance(item, dict):
            raise ActionError(f"batch {name!r} must be an object")
        action_names = item.get("actions")
        if not isinstance(action_names, list) or not action_names:
            raise ActionError(f"batch {name!r} requires a non-empty 'actions' array")
        unknown = sorted(set(action_names) - set(actions))
        if unknown:
            raise ActionError(f"batch {name!r} references unknown action(s): {', '.join(unknown)}")
        batches[name] = BatchDefinition(
            name=name,
            description=str(item.get("description", "")),
            actions=tuple(action_names),
        )


def resolve_action_names(catalog: ActionCatalog, spec: str) -> tuple[str, ...]:
    requested = [part.strip() for part in spec.split(",") if part.strip()]
    if not requested:
        raise ActionValidationError("action name is required")

    resolved: list[str] = []
    seen: set[str] = set()
    for name in requested:
        if name in catalog.actions:
            names = (name,)
        elif name in catalog.batches:
            names = catalog.batches[name].actions
        else:
            raise ActionValidationError(f"unknown action or batch: {name}")
        for item in names:
            if item not in seen:
                seen.add(item)
                resolved.append(item)
    return tuple(resolved)


def execute_actions(
    catalog: ActionCatalog,
    action_names: Sequence[str],
    parameters: Mapping[str, str],
    *,
    context: ActionContext | None = None,
    registry: ActionRegistry | None = None,
) -> ActionExecution:
    started = time.monotonic()
    provider_registry = registry or catalog.registry
    if context is None:
        context = ActionContext(services=provider_registry.services())
    elif not context.services:
        context = ActionContext(
            resources=context.resources,
            services=provider_registry.services(),
        )
    prepared_actions: list[PreparedAction] = []
    for name in action_names:
        definition = catalog.actions.get(name)
        if definition is None:
            raise ActionValidationError(f"unknown action: {name}")
        provider = provider_registry.get(definition.action_type)
        prepared_actions.append(provider.prepare(definition, parameters, context))

    workers = max(1, min(catalog.settings.max_concurrency, len(prepared_actions)))
    results: list[ActionResult] = [None] * len(prepared_actions)  # type: ignore[list-item]

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="action") as pool:
        future_indexes = {
            pool.submit(_run_action, prepared, catalog.settings.max_output_chars): index
            for index, prepared in enumerate(prepared_actions)
        }
        for future in as_completed(future_indexes):
            index = future_indexes[future]
            prepared = prepared_actions[index]
            try:
                results[index] = future.result()
            except Exception as exc:  # pragma: no cover - defensive boundary
                results[index] = ActionResult(
                    name=prepared.definition.name,
                    command=prepared.command,
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    timed_out=False,
                    duration_ms=0,
                    error=str(exc),
                )

    elapsed = int((time.monotonic() - started) * 1000)
    return ActionExecution(results=tuple(results), elapsed_ms=elapsed)


def _run_action(prepared: PreparedAction, max_output_chars: int) -> ActionResult:
    if prepared.runner is not None:
        return prepared.runner(max_output_chars)
    definition = prepared.definition
    process: ProcessResult = run_process(
        prepared.command,
        timeout_seconds=definition.timeout_seconds,
        cwd=definition.working_dir,
        env=_expand_env(prepared.env),
        max_output_chars=max_output_chars,
    )
    return ActionResult(
        name=definition.name,
        command=prepared.command,
        exit_code=process.exit_code,
        stdout=process.stdout,
        stderr=process.stderr,
        timed_out=process.timed_out,
        duration_ms=process.duration_ms,
    )


def _expand_env(values: Mapping[str, str]) -> dict[str, str]:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise ActionValidationError(f"environment variable {name} is not set")
        return value

    return {key: ENV_PLACEHOLDER_RE.sub(replace, value) for key, value in values.items()}


def _positive_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ActionError(f"{label} must be a number") from exc
    if parsed <= 0:
        raise ActionError(f"{label} must be greater than zero")
    return parsed


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ActionError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ActionError(f"{label} must be greater than zero")
    return parsed


def _env_positive_int(name: str, default: Any) -> int:
    return _positive_int(os.getenv(name, default), f"environment variable {name}")
