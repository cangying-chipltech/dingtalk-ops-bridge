"""Stable API for action plugins stored in separate repositories.

Plugins should import from this module, not from internal implementation modules.
Only additive changes are allowed within one ``PLUGIN_API_VERSION``.
"""

from .action_api import (
    PLACEHOLDER_RE,
    PLUGIN_API_VERSION,
    PLUGIN_COMMAND_ENTRYPOINT_GROUP,
    PLUGIN_ENTRYPOINT_GROUP,
    RESOURCE_CONFIG_DIR,
    RESERVED_COMMAND_PREFIXES,
    ActionContext,
    ActionDefinition,
    ActionError,
    ActionExecution,
    ActionProvider,
    ActionResult,
    ActionValidationError,
    CommandHelpProvider,
    CommandProvider,
    MonitorProvider,
    MonitorResult,
    ParameterDefinition,
    PreparedAction,
)


__all__ = [
    "PLACEHOLDER_RE",
    "PLUGIN_API_VERSION",
    "PLUGIN_COMMAND_ENTRYPOINT_GROUP",
    "PLUGIN_ENTRYPOINT_GROUP",
    "RESOURCE_CONFIG_DIR",
    "RESERVED_COMMAND_PREFIXES",
    "ActionContext",
    "ActionDefinition",
    "ActionError",
    "ActionExecution",
    "ActionProvider",
    "ActionResult",
    "ActionValidationError",
    "CommandHelpProvider",
    "CommandProvider",
    "MonitorProvider",
    "MonitorResult",
    "ParameterDefinition",
    "PreparedAction",
]
