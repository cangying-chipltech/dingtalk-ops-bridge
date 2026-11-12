#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_src="$script_dir/../src"

if [[ -d "$project_src" ]]; then
  export PYTHONPATH="$project_src${PYTHONPATH:+:$PYTHONPATH}"
fi

exec python3 -m dingtalk_ops_bridge command "$@"
