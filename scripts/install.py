#!/usr/bin/env python3
"""Explicit-file portable installer. Source trees may contain private local files."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

REQUIRED = (
    ".codex-plugin/plugin.json", "codex_bridge/__init__.py", "codex_bridge/cli.py",
    "codex_bridge/codex_adapter.py", "codex_bridge/compatibility.py", "codex_bridge/core.py",
    "codex_bridge/artifacts.py", "codex_bridge/autostart.py", "codex_bridge/autostart_posix.py", "codex_bridge/processes.py",
    "codex_bridge/connections.py",
    "codex_bridge/local_actions.py", "codex_bridge/mcp.py",
    "codex_bridge/onboarding.py", "codex_bridge/tools.py", "codex_bridge/transport.py",
    "scripts/bridge.ps1", "scripts/install.ps1", "scripts/setup.ps1", "scripts/bridge.sh", "scripts/install.sh",
    "scripts/setup.sh", "scripts/setup.py", "scripts/install.py", "scripts/run_bridge.py", "skills/collaborate/SKILL.md",
)
OPTIONAL = ("README.md", "LICENSE", "LICENSE.md", "NOTICES", "NOTICES.md", "config.example.json",
            "docs/SETUP.md", "docs/ONBOARDING-PLAN.md", "docs/ADVANCED.md", "docs/SECURITY.md",
            "docs/TESTING.md", "docs/TAILSCALE.md", "docs/RUNTIME.md", "docs/LINUX.md", "docs/CONNECTIONS.md", "docs/RECEIVING-SSH.md", "docs/PERMISSIONS.md", "config.tailscale.example.json",
            "examples/computer-a.example.json", "examples/computer-b.example.json")


class InstallError(ValueError):
    """Deliberately written setup guidance, safe to surface without OS details."""
    def __init__(self, message):
        super().__init__(message)
        self.public_message = message


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


def install(source: Path, destination: Path, config: Path, *, upgrade=False):
    if sys.version_info < (3, 11):
        raise ValueError("Codex Bridge requires Python 3.11 or newer")
    source = source.absolute(); destination = destination.expanduser().absolute(); config = config.expanduser().absolute()
    if destination.name != "codex-bridge":
        raise ValueError("The install directory must be named codex-bridge")
    if destination.resolve() != source.resolve() and destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("The install directory cannot be inside the source bundle")
    no_links(destination, destination.parent)
    selected = list(REQUIRED) + [name for name in OPTIONAL if (source/name).is_file()]
    for name in selected:
        item = source/name
        no_links(item, source)
        # Check the selected install root and its contents. OS ancestors can be
        # legitimate links (macOS /var -> /private/var); they are not bundle data.
        no_links(destination/name, destination.parent)
        if not item.is_file():
            raise ValueError("Required plugin file is missing: " + name)
    no_links(destination/".mcp.json", destination.parent)
    launcher = {"mcpServers": {"codex_bridge": {"command": sys.executable,
        "args": ["-u", str(destination/"codex_bridge"/"mcp.py"), "--config", str(config)]}}}
    # Read and validate all selected bytes before the first destination write.
    payload = {name: (source/name).read_bytes() for name in selected}
    for name, data in payload.items():
        if name.endswith('.py'):
            ast.parse(data, filename=name)
        elif name.endswith('.json'):
            json.loads(data.decode('utf-8-sig'))
    manifest = json.loads(payload['.codex-plugin/plugin.json'].decode('utf-8-sig'))
    if not isinstance(manifest, dict) or manifest.get('name') != 'codex-bridge':
        raise ValueError('Source manifest does not identify Codex Bridge')
    if config.resolve() in [(destination/name).resolve() for name in (*selected, '.mcp.json')]:
        raise ValueError('Private configuration must not replace an installed source or launcher file')
    if destination.exists() and any(destination.iterdir()):
        existing_manifest = destination/'.codex-plugin/plugin.json'
        try:
            recognized = json.loads(existing_manifest.read_text(encoding='utf-8-sig')).get('name') == 'codex-bridge'
        except (OSError, ValueError, AttributeError):
            recognized = False
        if not recognized:
            raise InstallError('Nonempty destination is not a recognized Bridge installation; select an empty directory')
        changed = any((destination/name).exists() and (destination/name).read_bytes() != data
                      for name, data in payload.items())
        if source.resolve() != destination.resolve() and changed and not upgrade:
            raise InstallError('Existing source differs; stop its workers and explicitly use --upgrade (PowerShell: -Upgrade), or select an empty directory')
    launcher_path = destination/'.mcp.json'
    if launcher_path.exists():
        try:
            existing = json.loads(launcher_path.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError):
            raise InstallError('Existing .mcp.json is invalid; preserve and inspect it before setup') from None
        if not isinstance(existing, dict) or not isinstance(existing.get('mcpServers'), dict):
            raise InstallError('Existing .mcp.json is invalid; preserve and inspect it before setup')
        entry = existing['mcpServers'].get('codex_bridge')
        if entry is not None and entry != launcher['mcpServers']['codex_bridge']:
            raise InstallError('Existing .mcp.json selects different settings; preserve it and resolve the conflict explicitly')
        if entry is None:
            existing['mcpServers']['codex_bridge'] = launcher['mcpServers']['codex_bridge']
            payload['.mcp.json'] = (json.dumps(existing, indent=2) + '\n').encode()
        # An already matching local launcher is kept byte-for-byte.
    else:
        payload['.mcp.json'] = (json.dumps(launcher, indent=2) + '\n').encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bridge-install-', dir=destination.parent) as staging:
        stage = Path(staging)
        for name, data in payload.items():
            file = stage/name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(data)
        previous = {}; written = []
        try:
            for name in payload:
                target = destination/name
                if target.exists() and target.read_bytes() == payload[name]:
                    continue
                previous[name] = target.read_bytes() if target.exists() else None
                target.parent.mkdir(parents=True, exist_ok=True)
                no_links(target, destination.parent)
                os.replace(stage/name, target)
                written.append(name)
        except OSError:
            # Roll back only the files this invocation replaced. Never remove
            # unknown files, state directories, credentials, or the install root.
            for name in reversed(written):
                target = destination/name
                if previous[name] is None: target.unlink()
                else: target.write_bytes(previous[name])
            raise
    for name in ("scripts/bridge.sh", "scripts/install.sh", "scripts/setup.sh"):
        (destination/name).chmod((destination/name).stat().st_mode | 0o111)
    return {"ok": True, "installed_directory": str(destination), "python": sys.executable,
            "config_path": str(config), "config_exists": config.exists(), "registration_required": True,
            "note": "Register the MCP server or activate the plugin, then run setup. Existing configuration is preserved."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.home()/"plugins"/"codex-bridge")
    parser.add_argument("--config", type=Path, default=Path.home()/".codex-bridge"/"config.json")
    parser.add_argument("--upgrade", action='store_true', help='Explicitly replace recognized installed source; stop its workers first')
    args = parser.parse_args(argv)
    try:
        result = install(Path(__file__).resolve().parents[1], args.directory, args.config, upgrade=args.upgrade)
    except (OSError, ValueError, SyntaxError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "install_failed", "message": str(exc)}}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
