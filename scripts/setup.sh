#!/bin/sh
set -eu
bridge_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
bridge_python=${CODEX_BRIDGE_PYTHON:-python3}
if ! command -v "$bridge_python" >/dev/null 2>&1; then
    echo 'Python 3.11+ was not found. Install it locally or set CODEX_BRIDGE_PYTHON to its executable.' >&2
    exit 1
fi
exec "$bridge_python" "$bridge_root/scripts/setup.py" "$@"
