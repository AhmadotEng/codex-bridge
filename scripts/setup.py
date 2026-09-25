#!/usr/bin/env python3
"""Install the local connector and run its guided setup with this Python."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


def error(code, message):
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}),
          file=sys.stderr)
    return 1


def load_installer(source):
    spec = importlib.util.spec_from_file_location(
        "codex_bridge_portable_install", source / "scripts" / "install.py")
    if spec is None or spec.loader is None:
        raise ValueError("Installer unavailable")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    return installer.install


def main(argv=None, *, source=None, installer=None, runner=None):
    # Keep this file parseable on older Python 3 versions so the dependency
    # error precedes importing the connector's Python 3.11 modules.
    if sys.version_info < (3, 11):
        return error("python_unsupported",
                     "Codex Bridge requires Python 3.11 or newer. Select it with CODEX_BRIDGE_PYTHON or run setup.py with its executable.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        default=Path.home() / "plugins" / "codex-bridge")
    parser.add_argument("--config", type=Path,
                        default=Path.home() / ".codex-bridge" / "config.json")
    parser.add_argument("--codex", help="The owner's local Codex executable")
    parser.add_argument("--peer-id", help="This computer's Bridge identifier")
    parser.add_argument("--batch", action="store_true",
                        help="Use supplied/saved values without guided questions")
    parser.add_argument("--skip-registration", action="store_true",
                        help="Preserve MCP registration without adding a new entry")
    parser.add_argument("--upgrade", action="store_true",
                        help="Replace a recognized installation after stopping its workers; preserve local configuration")
    args = parser.parse_args(argv)
    source = Path(source) if source is not None else Path(__file__).resolve().parents[1]
    try:
        install = installer or load_installer(source)
        installation = install(source, args.directory, args.config, **({"upgrade": True} if args.upgrade else {}))
    except (OSError, ValueError, ImportError, SyntaxError) as exc:
        return error("install_failed",
                     getattr(exc, "public_message", None) or
                     "Connector installation failed. Use a complete source bundle and a writable directory named codex-bridge outside the source bundle; avoid linked install paths.")
    destination = str(installation["installed_directory"])
    arguments = [sys.executable, "-I", str(Path(destination) / "scripts" / "run_bridge.py"),
                 "--config", str(installation["config_path"]), "setup"]
    if args.codex:
        arguments.extend(["--codex", args.codex])
    if args.peer_id:
        arguments.extend(["--peer-id", args.peer_id])
    arguments.append("--batch" if args.batch else "--guided")
    if not args.skip_registration:
        arguments.append("--register-mcp")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = destination
    try:
        # Inherit stdin/stdout/stderr so real interactive prompts remain usable.
        # The isolated entry pins installed code; retain the owner's cwd for
        # relative Codex paths, invitations, and project selections.
        result = (runner or subprocess.run)(
            arguments, env=environment, check=False)
    except (OSError, ValueError):
        return error("setup_launch_failed",
                     "The connector was installed, but local setup could not start. Run setup.py again with the selected Python 3.11+ executable.")
    except KeyboardInterrupt:
        error("setup_interrupted",
              "Setup was interrupted. Existing pairing and project state were not reset; rerun setup to continue.")
        return 130
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
