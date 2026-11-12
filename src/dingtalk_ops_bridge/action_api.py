from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RESOURCE_CONFIG_DIR = "config-dir"
PLUGIN_API_VERSION = 1
PLUGIN_ENTRYPOINT_GROUP = "dingtalk_ops_bridge.actions"
PLUGIN_COMMAND_ENTRYPOINT_GROUP = "dingtalk_ops_bridge.commands"


class ActionError(Exception):
    """Base error for action configuration and execution."""


class ActionValidationError(ActionError):
    """Raised when an action or its parameters are invalid."""


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    required: bool = False
    default: str | None = None
    pattern: re.Pattern[str] | None = None
    description: str = ""
    kind: str = "string"

    def validate(self, value: str) -> None:
        if self.pattern is not None and self.pattern.fullmatch(value) is None:
            raise ActionValidationError(
                f"parameter {self.name!r} does not match {self.pattern.pattern!r}: {value!r}"
            )


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    description: str
    action_type: str
    parameters: Mapping[str, ParameterDefinition] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    working_dir: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)

    def resolved_values(self, values: Mapping[str, str]) -> dict[str, str]:
        unknown = sorted(set(values) - set(self.parameters))
        if unknown:
            raise ActionValidationError(
                f"action {self.name!r} does not accept parameter(s): {', '.join(unknown)}"
            )

        resolved: dict[str, str] = {}
        for name, definition in self.parameters.items():
            if name in values:
                value = values[name]
            elif definition.default is not None:
                value = definition.default
            else:
                continue
            definition.validate(value)
            resolved[name] = value

        missing_required = sorted(
            name
            for name, definition in self.parameters.items()
            if definition.required and name not in resolved
        )
        if missing_required:
            raise ActionValidationError(
                f"action {self.name!r} requires parameter(s): {', '.join(missing_required)}"
            )
        return resolved


@dataclass(frozen=True)
class ActionResult:
    name: str
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        if self.timed_out:
            return "timeout"
        if self.error:
            return "error"
        return "failed"


@dataclass(frozen=True)
class ActionExecution:
    results: tuple[ActionResult, ...]
    elapsed_ms: int


@dataclass(frozen=True)
class ActionContext:
    resources: Mapping[str, Any] = field(default_factory=dict)
    services: Mapping[str, Any] = field(default_factory=dict)

    def require_service(self, name: str) -> Any:
        service = self.services.get(name.strip().lower())
        if service is None:
            raise ActionError(f"plugin service {name!r} is not available")
        return service


@dataclass(frozen=True)
class PreparedAction:
    definition: ActionDefinition
    command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    runner: Callable[[int], ActionResult] | None = None


class ActionProvider(Protocol):
    api_version: int
    type_name: str

    def validate(self, definition: ActionDefinition) -> None:
        """Validate type-specific configuration before execution."""

    def prepare(
        self,
        definition: ActionDefinition,
        values: Mapping[str, str],
        context: ActionContext,
    ) -> PreparedAction:
        """Resolve parameters and prepare an executable action."""


class CommandProvider(Protocol):
    api_version: int

    def handle(self, prompt: str, context: ActionContext) -> str | None:
        """Handle a command prompt, or return None when it does not apply."""


class ActionRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ActionProvider] = {}
        self._commands: list[CommandProvider] = []
        self._action_files: list[Path] = []
        self._services: dict[str, Any] = {}

    def register(self, provider: ActionProvider) -> None:
        name = provider.type_name.strip().lower()
        if not name:
            raise ActionError("action provider type_name must not be empty")
        if name in self._providers:
            raise ActionError(f"duplicate action provider: {name}")
        self._providers[name] = provider

    def get(self, action_type: str) -> ActionProvider:
        name = action_type.strip().lower()
        provider = self._providers.get(name)
        if provider is None:
            available = ", ".join(sorted(self._providers)) or "-"
            raise ActionError(f"unknown action type {action_type!r}; available: {available}")
        return provider

    def types(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def register_command(self, provider: CommandProvider) -> None:
        self._commands.append(provider)

    def commands(self) -> tuple[CommandProvider, ...]:
        return tuple(self._commands)

    def register_action_file(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved not in self._action_files:
            self._action_files.append(resolved)

    def action_files(self) -> tuple[Path, ...]:
        return tuple(self._action_files)

    def register_service(self, name: str, service: Any) -> None:
        service_name = name.strip().lower()
        if not service_name:
            raise ActionError("plugin service name must not be empty")
        if service_name in self._services:
            raise ActionError(f"duplicate plugin service: {service_name}")
        try:
            api_version = int(service.api_version)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ActionError(
                f"plugin service {service_name!r} must define api_version={PLUGIN_API_VERSION}"
            ) from exc
        if api_version != PLUGIN_API_VERSION:
            raise ActionError(
                f"plugin service {service_name!r} uses API version {api_version}; "
                f"framework expects {PLUGIN_API_VERSION}"
            )
        self._services[service_name] = service

    def service(self, name: str) -> Any:
        service = self._services.get(name.strip().lower())
        if service is None:
            available = ", ".join(sorted(self._services)) or "-"
            raise ActionError(f"unknown plugin service {name!r}; available: {available}")
        return service

    def has_service(self, name: str) -> bool:
        return name.strip().lower() in self._services

    def services(self) -> Mapping[str, Any]:
        return dict(self._services)
