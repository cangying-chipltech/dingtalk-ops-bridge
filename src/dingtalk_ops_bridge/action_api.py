from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol


PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RESOURCE_CONFIG_DIR = "config-dir"
PLUGIN_API_VERSION = 1
PLUGIN_ENTRYPOINT_GROUP = "dingtalk_ops_bridge.actions"
PLUGIN_COMMAND_ENTRYPOINT_GROUP = "dingtalk_ops_bridge.commands"
RESERVED_COMMAND_PREFIXES = frozenset(
    {
        "help",
        "?",
        "/help",
        "h",
        "帮助",
        "list",
        "actions",
        "动作",
        "列表",
        "run",
        "exec",
        "execute",
        "执行",
        "batch",
        "批处理",
        "monitor",
        "监控",
        "bridge",
        "check",
        "检查",
    }
)


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
class MonitorResult:
    ok: bool
    message: str
    details: str = ""


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


class CommandHelpProvider(Protocol):
    """Optional command-provider extension for detailed topic help."""

    def help(self, topic: str, context: ActionContext) -> str | None:
        """Return help for a full command topic, or None when unsupported."""


class MonitorProvider(Protocol):
    api_version: int
    type_name: str

    def monitors(self, context: ActionContext) -> Mapping[str, Mapping[str, Any]]:
        """Return plugin-owned monitor definitions keyed by monitor name."""

    def validate(self, check: Mapping[str, Any]) -> None:
        """Validate one check object handled by this provider."""

    def check(self, check: Mapping[str, Any], context: ActionContext) -> MonitorResult:
        """Execute one check and return its outcome."""


@dataclass(frozen=True)
class CommandRegistration:
    provider: CommandProvider
    source: str
    prefixes: tuple[str, ...] = ()
    fallback: bool = False


class ActionRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ActionProvider] = {}
        self._commands: list[CommandRegistration] = []
        self._command_prefixes: dict[str, CommandRegistration] = {}
        self._fallback_command: CommandRegistration | None = None
        self._action_files: list[Path] = []
        self._services: dict[str, Any] = {}
        self._monitor_providers: dict[str, MonitorProvider] = {}
        self._monitor_sources: dict[str, str] = {}

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

    def register_command(
        self,
        provider: CommandProvider,
        *,
        source: str = "unknown",
        prefixes: Iterable[str] | str | None = None,
        fallback: bool | None = None,
    ) -> None:
        if isinstance(provider, type):
            provider = provider()

        declared_prefixes = (
            getattr(provider, "command_prefixes", None)
            if prefixes is None
            else prefixes
        )
        normalized_prefixes = _normalize_command_prefixes(
            declared_prefixes, source=source
        )

        has_fallback_setting = fallback is not None or hasattr(provider, "command_fallback")
        declared_fallback = (
            getattr(provider, "command_fallback", False) if fallback is None else fallback
        )
        if not isinstance(declared_fallback, bool):
            raise ActionError(f"{source}: command_fallback must be a boolean")
        if not normalized_prefixes and not declared_fallback:
            if has_fallback_setting:
                raise ActionError(
                    f"{source}: command provider must declare command_prefixes "
                    "or set command_fallback = True"
                )
            # Providers written before declarative routing existed remain usable
            # when they are the only fallback in the process.
            declared_fallback = True

        registration = CommandRegistration(
            provider=provider,
            source=source,
            prefixes=normalized_prefixes,
            fallback=declared_fallback,
        )
        for prefix in normalized_prefixes:
            if prefix in RESERVED_COMMAND_PREFIXES:
                raise ActionError(
                    f"{source}: command prefix {prefix!r} is reserved by the framework"
                )
            existing = self._command_prefixes.get(prefix)
            if existing is not None:
                raise ActionError(
                    f"{source}: command prefix {prefix!r} conflicts with {existing.source}"
                )
        if declared_fallback:
            if self._fallback_command is not None:
                raise ActionError(
                    f"{source}: command fallback conflicts with "
                    f"{self._fallback_command.source}; only one fallback provider is allowed"
                )
            self._fallback_command = registration

        self._commands.append(registration)
        for prefix in normalized_prefixes:
            self._command_prefixes[prefix] = registration

    def commands(self) -> tuple[CommandProvider, ...]:
        return tuple(registration.provider for registration in self._commands)

    def command_registrations(self) -> tuple[CommandRegistration, ...]:
        return tuple(self._commands)

    def command_prefixes(self) -> tuple[str, ...]:
        return tuple(sorted(self._command_prefixes))

    def command_fallback(self) -> CommandRegistration | None:
        return self._fallback_command

    def command_route(self, prompt: str) -> CommandRegistration | None:
        parts = (prompt or "").strip().split(maxsplit=1)
        if not parts:
            return None
        return self._command_prefixes.get(parts[0].casefold())

    def command_for(self, prompt: str) -> CommandRegistration | None:
        registration = self.command_route(prompt)
        if registration is not None:
            return registration
        return self._fallback_command

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

    def register_monitor(self, provider: MonitorProvider, *, source: str = "unknown") -> None:
        if isinstance(provider, type):
            provider = provider()
        name = str(getattr(provider, "type_name", "")).strip().lower()
        if not name:
            raise ActionError(f"{source}: monitor provider must define type_name")
        existing_source = self._monitor_sources.get(name)
        if existing_source is not None:
            raise ActionError(
                f"{source}: duplicate monitor provider {name!r}; "
                f"already registered by {existing_source}"
            )
        try:
            api_version = int(provider.api_version)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ActionError(
                f"{source}: monitor provider must define api_version={PLUGIN_API_VERSION}"
            ) from exc
        if api_version != PLUGIN_API_VERSION:
            raise ActionError(
                f"{source}: monitor provider uses API version {api_version}; "
                f"framework expects {PLUGIN_API_VERSION}"
            )
        for method in ("monitors", "validate", "check"):
            if not callable(getattr(provider, method, None)):
                raise ActionError(f"{source}: monitor provider must implement {method}()")
        self._monitor_providers[name] = provider
        self._monitor_sources[name] = source

    def monitor_provider(self, check_type: str) -> MonitorProvider:
        name = check_type.strip().lower()
        provider = self._monitor_providers.get(name)
        if provider is None:
            available = ", ".join(sorted(self._monitor_providers)) or "-"
            raise ActionError(
                f"unknown monitor provider {check_type!r}; available: {available}"
            )
        return provider

    def has_monitor_provider(self, check_type: str) -> bool:
        return check_type.strip().lower() in self._monitor_providers

    def monitor_providers(self) -> tuple[MonitorProvider, ...]:
        return tuple(self._monitor_providers.values())

    def monitor_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._monitor_providers))

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


def _normalize_command_prefixes(
    value: Iterable[str] | str | None,
    *,
    source: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates = (value,)
    else:
        try:
            candidates = tuple(value)
        except TypeError as exc:
            raise ActionError(f"{source}: command_prefixes must be a string or sequence") from exc

    prefixes: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise ActionError(f"{source}: command_prefixes must contain only strings")
        prefix = candidate.strip().casefold()
        if not prefix:
            raise ActionError(f"{source}: command prefix must not be empty")
        if any(character.isspace() for character in prefix):
            raise ActionError(
                f"{source}: command prefix {candidate!r} must be a single token"
            )
        if prefix not in prefixes:
            prefixes.append(prefix)
    return tuple(prefixes)
