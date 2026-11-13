#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"

python3 -m unittest discover -s "$project_dir/tests" -v
python3 -m unittest discover -s "$project_dir/plugins/nodes/tests" -v
python3 -m unittest discover -s "$project_dir/plugins/model-manager/tests" -v
python3 -m unittest discover -s "$project_dir/plugins/swe-runner/tests" -v
