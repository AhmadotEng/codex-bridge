#!/usr/bin/env python3
"""Explicit-file portable installer. Source trees may contain private local files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

REQUIRED = (
    ".codex-plugin/plugin.json", "codex_bridge/__init__.py", "codex_bridge/cli.py",
    "codex_bridge/codex_adapter.py", "codex_bridge/compatibility.py", "codex_bridge/core.py",
    "codex_bridge/artifacts.py", "codex_bridge/local_actions.py", "codex_bridge/mcp.py",
    "codex_bridge/onboarding.py", "codex_bridge/tools.py", "codex_bridge/transport.py",
    "scripts/bridge.ps1", "scripts/install.ps1", "scripts/setup.ps1", "scripts/bridge.sh", "scripts/install.sh",
    "scripts/install.py", "skills/collaborate/SKILL.md",
)
OPTIONAL = ("README.md", "LICENSE", "LICENSE.md", "NOTICES", "NOTICES.md", "config.example.json",
            "docs/SETUP.md", "docs/ONBOARDING-PLAN.md", "docs/ADVANCED.md", "docs/SECURITY.md",
            "docs/TESTING.md", "examples/computer-a.example.json", "examples/computer-b.example.json")


def no_links(path: Path, stop: Path | None = None):
    for candidate in (path, *path.parents):
        if stop is not None and candidate == stop:
            break
        try:
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        # Python 3.11 has no Path.is_junction(); the Windows reparse bit also
        # catches junctions and dangling links without following their targets.
        if candidate.is_symlink() or attributes & 1024:
            raise ValueError("Install source and target must not contain linked files or directories")


def install(source: Path, destination: Path, config: Path):
    if sys.version_info < (3, 11):
        raise ValueError("Codex Bridge requires Python 3.11 or newer")
    source = source.absolute(); destination = destination.expanduser().absolute(); config = config.expanduser().absolute()
    if destination.name != "codex-bridge":
        raise ValueError("The install directory must be named codex-bridge")
    if destination.resolve() != source.resolve() and destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("The install directory cannot be inside the source bundle")
    selected = list(REQUIRED) + [name for name in OPTIONAL if (source/name).is_file()]
    for name in selected:
        item = source/name
        no_links(item, source)
        # Check the selected install root and its contents. OS ancestors can be
        # legitimate links (macOS /var -> /private/var); they are not bundle data.
        no_links(destination/name, destination.parent)
        if not item.is_file():
            raise ValueError("Required connector file is missing: " + name)
    no_links(destination/".mcp.json", destination.parent)
    if source != destination:
        for name in selected:
            target = destination/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source/name, target)
    launcher = {"mcpServers": {"codex_bridge": {"command": sys.executable,
        "args": ["-u", str(destination/"codex_bridge"/"mcp.py"), "--config", str(config)]}}}
    (destination/".mcp.json").write_text(json.dumps(launcher, indent=2) + "\n", encoding="utf-8")
    for name in ("scripts/bridge.sh", "scripts/install.sh"):
        (destination/name).chmod((destination/name).stat().st_mode | 0o111)
    return {"ok": True, "installed_directory": str(destination), "python": sys.executable,
            "config_path": str(config), "config_exists": config.exists(), "registration_required": True,
            "note": "Register the MCP server or activate the plugin, then run setup. Existing configuration is preserved."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.home()/"plugins"/"codex-bridge")
    parser.add_argument("--config", type=Path, default=Path.home()/".codex-bridge"/"config.json")
    args = parser.parse_args(argv)
    try:
        result = install(Path(__file__).resolve().parents[1], args.directory, args.config)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "install_failed", "message": str(exc)}}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
