from __future__ import annotations

from typing import Mapping

from ..action_api import (
    ActionContext,
    ActionDefinition,
    ActionValidationError,
    PLUGIN_API_VERSION,
    PLACEHOLDER_RE,
    PreparedAction,
)


class CommandActionProvider:
    api_version = PLUGIN_API_VERSION
    type_name = "command"

    def validate(self, definition: ActionDefinition) -> None:
        command = definition.config.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) for part in command
        ):
            raise ActionValidationError(
                f"command action {definition.name!r} requires a non-empty string array 'command'"
            )
        placeholders = {
            match for part in command for match in PLACEHOLDER_RE.findall(part)
        }
        unknown = sorted(placeholders - set(definition.parameters))
        if unknown:
            raise ActionValidationError(
                f"action {definition.name!r} uses undeclared parameter(s): {', '.join(unknown)}"
            )

    def prepare(
        self,
        definition: ActionDefinition,
        values: Mapping[str, str],
        context: ActionContext,
    ) -> PreparedAction:
        del context
        resolved = definition.resolved_values(values)
        command = tuple(
            PLACEHOLDER_RE.sub(lambda match: resolved[match.group(1)], part)
            for part in definition.config["command"]
        )
        return PreparedAction(definition=definition, command=command, env=definition.env)


PROVIDER = CommandActionProvider()
