from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from ..action_api import ActionError, ActionProvider, ActionRegistry
from ..plugin_loader import load_external_plugins, register_provider


def _provider_from_module(module: ModuleType) -> ActionProvider | None:
    provider = getattr(module, "PROVIDER", None)
    if provider is None:
        return None
    if not getattr(provider, "type_name", ""):
        raise ActionError(f"action provider module {module.__name__} has no type_name")
    return provider


def build_default_registry() -> ActionRegistry:
    """Discover action providers from this package.

    A new provider only needs to add a module here and expose ``PROVIDER``.
    Core action loading and execution do not need to change.
    """

    registry = ActionRegistry()
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        provider = _provider_from_module(module)
        if provider is not None:
            register_provider(registry, provider, f"built-in {module.__name__}")
    load_external_plugins(registry)
    return registry
