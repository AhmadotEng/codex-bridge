#!/bin/sh
set -eu
bridge_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
bridge_python=${CODEX_BRIDGE_PYTHON:-python3}
exec "$bridge_python" "$bridge_root/scripts/install.py" "$@"
