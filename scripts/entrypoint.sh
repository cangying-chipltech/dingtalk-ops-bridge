#!/usr/bin/env bash
set -euo pipefail

if ! command -v dws >/dev/null 2>&1; then
  echo "dws binary not found in PATH" >&2
  exit 1
fi

agent_cmd="${AGENT_CMD:-/app/scripts/agent-router.sh}"
if [[ ! -x "$agent_cmd" ]]; then
  echo "agent command is not executable: $agent_cmd" >&2
  exit 1
fi

args=(dev connect --channel custom --agent-cmd "$agent_cmd")

if [[ -n "${DWS_UNIFIED_APP_ID:-}" ]]; then
  args+=(--unified-app-id "$DWS_UNIFIED_APP_ID")
elif [[ -n "${ROBOT_CLIENT_ID:-}" && -n "${ROBOT_CLIENT_SECRET:-}" ]]; then
  args+=(--robot-client-id "$ROBOT_CLIENT_ID" --robot-client-secret "$ROBOT_CLIENT_SECRET")
else
  echo "set DWS_UNIFIED_APP_ID or ROBOT_CLIENT_ID/ROBOT_CLIENT_SECRET" >&2
  exit 1
fi

if [[ -n "${DWS_ALLOWED_USERS:-}" ]]; then
  args+=(--allowed-users "$DWS_ALLOWED_USERS")
fi

if [[ -n "${DWS_ALLOWED_GROUPS:-}" ]]; then
  args+=(--allowed-groups "$DWS_ALLOWED_GROUPS")
fi

if [[ -n "${DWS_NOTIFY_STAFF_ID:-}" ]]; then
  args+=(--notify-staff-id "$DWS_NOTIFY_STAFF_ID")
fi

if [[ -n "${DWS_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra_args=($DWS_EXTRA_ARGS)
  args+=("${extra_args[@]}")
fi

exec dws "${args[@]}"
