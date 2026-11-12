from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .action_api import (
    PLUGIN_API_VERSION,
    PLUGIN_COMMAND_ENTRYPOINT_GROUP,
    PLUGIN_ENTRYPOINT_GROUP,
    ActionError,
    ActionProvider,
    ActionRegistry,
    CommandProvider,
)
from .paths import plugins_dir


LOGGER = logging.getLogger(__name__)
PLUGIN_MANIFEST = "action_plugin.json"
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class _PluginManifest:
    path: Path
    plugin_id: str
    module_name: str
    provider_names: tuple[str, ...]
    command_names: tuple[str, ...]
    action_files: tuple[str, ...]
    python_paths: tuple[str, ...]
    services: Mapping[str, str]
    requires: frozenset[str]

    @property
    def provided_services(self) -> frozenset[str]:
        return frozenset(self.services)


def register_provider(
    registry: ActionRegistry,
    provider: Any,
    source: str,
) -> None:
    if isinstance(provider, type):
        provider = provider()
    if not getattr(provider, "type_name", ""):
        raise ActionError(f"{source}: provider must define type_name")
    try:
        api_version = int(provider.api_version)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ActionError(
            f"{source}: provider must define api_version={PLUGIN_API_VERSION}"
        ) from exc
    if api_version != PLUGIN_API_VERSION:
        raise ActionError(
            f"{source}: unsupported plugin API version {api_version}; "
            f"framework expects {PLUGIN_API_VERSION}"
        )
    if not callable(getattr(provider, "validate", None)) or not callable(
        getattr(provider, "prepare", None)
    ):
        raise ActionError(f"{source}: provider must implement validate() and prepare()")
    registry.register(provider)
    LOGGER.info("registered action provider %s from %s", provider.type_name, source)


def register_command_provider(
    registry: ActionRegistry,
    provider: Any,
    source: str,
) -> None:
    if isinstance(provider, type):
        provider = provider()
    try:
        api_version = int(provider.api_version)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ActionError(
            f"{source}: command provider must define api_version={PLUGIN_API_VERSION}"
        ) from exc
    if api_version != PLUGIN_API_VERSION:
        raise ActionError(
            f"{source}: unsupported plugin API version {api_version}; "
            f"framework expects {PLUGIN_API_VERSION}"
        )
    if not callable(getattr(provider, "handle", None)):
        raise ActionError(f"{source}: command provider must implement handle()")
    registry.register_command(provider)
    LOGGER.info("registered command provider from %s", source)


def load_external_plugins(
    registry: ActionRegistry,
    root: Path | None = None,
) -> None:
    for entry_point in _entry_points():
        try:
            loaded = entry_point.load()
        except Exception as exc:
            raise ActionError(f"cannot load action plugin {entry_point.name!r}: {exc}") from exc
        for provider in _as_providers(loaded):
            register_provider(registry, provider, f"entry point {entry_point.name}")

    for entry_point in _entry_points(PLUGIN_COMMAND_ENTRYPOINT_GROUP):
        try:
            loaded = entry_point.load()
        except Exception as exc:
            raise ActionError(f"cannot load command plugin {entry_point.name!r}: {exc}") from exc
        for provider in _as_providers(loaded):
            register_command_provider(registry, provider, f"entry point {entry_point.name}")

    plugin_root = root or plugins_dir()
    manifests = [_read_manifest(path) for path in _manifest_paths(plugin_root)]
    for manifest in _dependency_order(manifests, registry):
        _load_manifest(registry, manifest)


def _read_manifest(manifest_path: Path) -> _PluginManifest:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActionError(f"cannot read action plugin manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ActionError(f"action plugin manifest {manifest_path} must be an object")

    try:
        api_version = int(manifest.get("apiVersion"))
    except (TypeError, ValueError) as exc:
        raise ActionError(
            f"action plugin manifest {manifest_path} requires integer apiVersion"
        ) from exc
    if api_version != PLUGIN_API_VERSION:
        raise ActionError(
            f"action plugin manifest {manifest_path} uses API version {api_version}; "
            f"framework expects {PLUGIN_API_VERSION}"
        )

    module_name = str(manifest.get("module", "")).strip()
    if not module_name:
        raise ActionError(f"action plugin manifest {manifest_path} requires 'module'")
    plugin_id = str(manifest.get("id") or manifest_path.parent.name).strip()
    if PLUGIN_ID_RE.fullmatch(plugin_id) is None:
        raise ActionError(
            f"action plugin manifest {manifest_path} has invalid plugin id {plugin_id!r}"
        )

    services_raw = manifest.get("services", {})
    if services_raw is None:
        services_raw = {}
    if not isinstance(services_raw, dict):
        raise ActionError(f"action plugin manifest {manifest_path} has invalid services object")
    services: dict[str, str] = {}
    for service_name, attribute_name in services_raw.items():
        normalized = str(service_name).strip().lower()
        attribute = str(attribute_name).strip() if isinstance(attribute_name, str) else ""
        if not normalized or not attribute:
            raise ActionError(
                f"action plugin manifest {manifest_path} requires service names mapped to module attributes"
            )
        services[normalized] = attribute

    requires = frozenset(
        item.strip().lower()
        for item in _name_list(manifest.get("requires"), manifest_path, "requires")
        if item.strip()
    )
    has_action_provider = "providers" in manifest or "provider" in manifest
    has_command_provider = "commandProviders" in manifest or "commandProvider" in manifest
    has_action_file = "actionsFiles" in manifest or "actionsFile" in manifest
    provider_names_raw = manifest.get("providers", manifest.get("provider"))
    command_names_raw = manifest.get("commandProviders", manifest.get("commandProvider"))
    action_files_raw = manifest.get("actionsFiles", manifest.get("actionsFile"))
    if (
        not has_action_provider
        and not has_command_provider
        and not has_action_file
        and not services
    ):
        provider_names_raw = "PROVIDER"
    provider_names = _name_list(provider_names_raw, manifest_path, "provider")
    command_names = _name_list(command_names_raw, manifest_path, "commandProvider")
    action_files = _name_list(action_files_raw, manifest_path, "actionsFile")
    python_paths_raw = manifest.get("pythonPath", [])
    if isinstance(python_paths_raw, str):
        python_paths_raw = [python_paths_raw]
    if not isinstance(python_paths_raw, list):
        raise ActionError(f"action plugin manifest {manifest_path} has invalid pythonPath")
    python_paths = tuple(
        str(item).strip() for item in python_paths_raw if str(item).strip()
    )
    if not provider_names and not command_names and not action_files and not services:
        raise ActionError(
            f"action plugin manifest {manifest_path} defines no providers, services, or action files"
        )
    return _PluginManifest(
        path=manifest_path,
        plugin_id=plugin_id,
        module_name=module_name,
        provider_names=tuple(provider_names),
        command_names=tuple(command_names),
        action_files=tuple(action_files),
        python_paths=python_paths,
        services=services,
        requires=requires,
    )


def _dependency_order(
    manifests: Iterable[_PluginManifest],
    registry: ActionRegistry,
) -> list[_PluginManifest]:
    by_id: dict[str, _PluginManifest] = {}
    service_owners: dict[str, str] = {}
    for manifest in manifests:
        if manifest.plugin_id in by_id:
            raise ActionError(f"duplicate action plugin id: {manifest.plugin_id}")
        by_id[manifest.plugin_id] = manifest
        for service_name in manifest.provided_services:
            owner = service_owners.get(service_name)
            if owner is not None:
                raise ActionError(
                    f"plugin service {service_name!r} is provided by both "
                    f"{owner!r} and {manifest.plugin_id!r}"
                )
            if registry.has_service(service_name):
                raise ActionError(
                    f"plugin service {service_name!r} from {manifest.plugin_id!r} "
                    "conflicts with an already registered service"
                )
            service_owners[service_name] = manifest.plugin_id

    dependencies: dict[str, set[str]] = {plugin_id: set() for plugin_id in by_id}
    for manifest in by_id.values():
        for required in manifest.requires:
            owner = service_owners.get(required)
            if owner is None:
                if not registry.has_service(required):
                    raise ActionError(
                        f"plugin {manifest.plugin_id!r} requires service {required!r}, "
                        "but no discovered plugin provides it"
                    )
                continue
            if owner != manifest.plugin_id:
                dependencies[manifest.plugin_id].add(owner)

    ordered: list[_PluginManifest] = []
    remaining = set(by_id)
    loaded: set[str] = set()
    while remaining:
        ready = sorted(
            plugin_id
            for plugin_id in remaining
            if dependencies[plugin_id].issubset(loaded)
        )
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ActionError(f"action plugin dependency cycle detected: {cycle}")
        for plugin_id in ready:
            ordered.append(by_id[plugin_id])
            loaded.add(plugin_id)
            remaining.remove(plugin_id)
    return ordered


def _load_manifest(registry: ActionRegistry, plugin: _PluginManifest) -> None:
    manifest_path = plugin.path
    module_name = plugin.module_name
    provider_names = plugin.provider_names
    command_names = plugin.command_names
    action_files = plugin.action_files

    plugin_root = manifest_path.parent
    python_paths = [plugin_root / item for item in plugin.python_paths]
    if not python_paths:
        python_paths = [plugin_root]
    for python_path in python_paths:
        if not python_path.is_dir():
            raise ActionError(f"action plugin pythonPath does not exist: {python_path}")
        if str(python_path) not in sys.path:
            sys.path.insert(0, str(python_path))
    importlib.invalidate_caches()

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ActionError(
            f"cannot import action plugin module {module_name!r} from {manifest_path}: {exc}"
        ) from exc

    for provider_name in provider_names:
        provider = getattr(module, str(provider_name), None)
        if provider is None:
            raise ActionError(
                f"action plugin module {module_name!r} has no provider {provider_name!r}"
            )
        for item in _as_providers(provider):
            register_provider(registry, item, str(manifest_path))

    for provider_name in command_names:
        provider = getattr(module, str(provider_name), None)
        if provider is None:
            raise ActionError(
                f"action plugin module {module_name!r} has no command provider {provider_name!r}"
            )
        for item in _as_providers(provider):
            register_command_provider(registry, item, str(manifest_path))

    for service_name, attribute_name in plugin.services.items():
        service = getattr(module, str(attribute_name), None)
        if service is None:
            raise ActionError(
                f"action plugin module {module_name!r} has no service {attribute_name!r}"
            )
        if isinstance(service, type):
            service = service()
        registry.register_service(service_name, service)
        LOGGER.info(
            "registered plugin service %s from %s", service_name, manifest_path
        )

    for action_file in action_files:
        candidate = (plugin_root / action_file).resolve()
        if plugin_root.resolve() not in candidate.parents:
            raise ActionError(
                f"action plugin manifest {manifest_path} actionsFile escapes plugin root: {action_file}"
            )
        if not candidate.is_file():
            raise ActionError(
                f"action plugin manifest {manifest_path} actionsFile does not exist: {candidate}"
            )
        registry.register_action_file(candidate)


def _manifest_paths(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    paths = []
    direct = root / PLUGIN_MANIFEST
    if direct.is_file():
        paths.append(direct)
    paths.extend(
        child / PLUGIN_MANIFEST
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / PLUGIN_MANIFEST).is_file()
    )
    return paths


def _entry_points(group: str = PLUGIN_ENTRYPOINT_GROUP) -> Iterable[Any]:
    try:
        discovered = importlib.metadata.entry_points()
    except Exception:
        return ()
    if hasattr(discovered, "select"):
        return discovered.select(group=group)
    return discovered.get(group, ())


def _as_providers(value: Any) -> tuple[ActionProvider, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _name_list(value: Any, manifest_path: Path, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ActionError(f"action plugin manifest {manifest_path} has invalid {field} list")
