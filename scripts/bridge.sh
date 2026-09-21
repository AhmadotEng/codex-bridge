#!/bin/sh
set -eu
bridge_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
bridge_python=${CODEX_BRIDGE_PYTHON:-python3}
export PYTHONPATH="$bridge_root"
if [ "$#" -eq 0 ]; then
    set -- --help
fi
exec "$bridge_python" -c '
import json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
arguments = sys.argv[2:]
launcher_path = root / ".mcp.json"
launcher = json.loads(launcher_path.read_text()) if launcher_path.is_file() else {}
entry = launcher.get("mcpServers", {}).get("codex_bridge", {})
runtime = os.environ.get("CODEX_BRIDGE_PYTHON") or entry.get("command") or sys.executable
config = os.environ.get("CODEX_BRIDGE_CONFIG")
saved = entry.get("args", [])
if not config and "--config" in saved:
    index = saved.index("--config")
    if index + 1 < len(saved): config = saved[index + 1]
if config and not any(arg == "--config" or arg.startswith("--config=") for arg in arguments):
    arguments = ["--config", config, *arguments]
os.execvpe(runtime, [runtime, "-m", "codex_bridge.cli", *arguments], os.environ)
' "$bridge_root" "$@"
