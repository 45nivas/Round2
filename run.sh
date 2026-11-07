#!/usr/bin/env bash
# Entrypoint for automated evaluation.
# Usage: ./run.sh <SIM_URL> [extra controller args...]
#
# Starts the Outcome-First Controller against the simulator at <SIM_URL>
# and runs until GET /sim/state returns done: true.

set -euo pipefail

SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
shift || true

export PYTHONUTF8=1

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
else
    PYTHON_BIN="python"
fi

exec "$PYTHON_BIN" -m controller --url "$SIM_URL" "$@"
