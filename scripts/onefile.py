"""Bootstrap for the generated .pyz. Python is required; accounts stay local."""
import sys

if sys.version_info < (3, 11):
    sys.stderr.write('Codex Bridge requires Python 3.11 or newer. Run this file with python3.11+ locally.\n')
    raise SystemExit(2)

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_FILES = 128
RECEIPT = '.bridge-launcher.json'


class LauncherError(ValueError):
    pass


def bundle_manifest(archive):
    names = archive.namelist()
    if len(names) != len(set(names)) or len(names) > MAX_PAYLOAD_FILES + 3:
        raise LauncherError('Invalid or duplicate launcher entries.')
    if archive.getinfo('manifest.json').file_size > 65536:
        raise LauncherError('Launcher manifest exceeds its size limit.')
    raw = archive.read('manifest.json')
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('bridge_version'), str):
        raise LauncherError('Invalid launcher manifest structure.')
    files = manifest.get('files')
    if manifest.get('format') != 1 or not isinstance(files, dict) or not 1 <= len(files) <= MAX_PAYLOAD_FILES:
        raise LauncherError('Unsupported launcher manifest.')
    expected = {'__main__.py', 'onefile.py', 'manifest.json'}
    total = 0
    for name, digest in files.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or not path.parts or any(p in ('', '.', '..') for p in path.parts)
                or '\\' in name or ':' in name or path.as_posix() != name
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise LauncherError('Invalid launcher payload path or checksum.')
        member = 'payload/' + name
        info = archive.getinfo(member)
        if info.is_dir() or ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise LauncherError('Linked or directory payload entries are not supported.')
        total += info.file_size
        if total > MAX_PAYLOAD_BYTES:
            raise LauncherError('Launcher payload exceeds its size limit.')
        expected.add(member)
    if set(names) != expected:
        raise LauncherError('Launcher contains unexpected entries.')
    for name, digest in files.items():
        if hashlib.sha256(archive.read('payload/' + name)).hexdigest() != digest:
            raise LauncherError('Launcher checksum verification failed.')
    return manifest, hashlib.sha256(raw).hexdigest()


def unpack(archive, manifest, directory):
    for name in manifest['files']:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read('payload/' + name))
        target.chmod(0o700 if name.endswith('.sh') else 0o600)


def load_installer(source):
    spec = importlib.util.spec_from_file_location('bridge_bundle_installer', source / 'scripts' / 'install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_install(archive, manifest, bundle_id, destination, config):
    with tempfile.TemporaryDirectory(prefix='codex-bridge-launcher-') as temporary:
        source = Path(temporary) / 'source'
        unpack(archive, manifest, source)
        installer = load_installer(source)
        installer.no_links(destination, destination.parent)
        if destination.exists() and any(destination.iterdir()):
            receipt = destination / RECEIPT
            installer.no_links(receipt, destination.parent)
            if not receipt.is_file() or receipt.stat().st_size > 4096:
                raise LauncherError('Existing installation was preserved. Select a separate empty --directory, or follow the documented update procedure.')
            saved = json.loads(receipt.read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or saved.get('format') != 1 or saved.get('bundle_id') != bundle_id:
                raise LauncherError('A different installation already exists. It was preserved; this launcher does not perform automatic upgrades.')
            for name, digest in manifest['files'].items():
                installed = destination / name
                installer.no_links(installed, destination.parent)
                expected_size = archive.getinfo('payload/' + name).file_size
                if not installed.is_file() or installed.stat().st_size != expected_size:
                    raise LauncherError('Installed source differs from this launcher. It was preserved; select an empty --directory for a fresh install.')
                with installed.open('rb') as handle:
                    data = handle.read(expected_size + 1)
                if len(data) != expected_size or hashlib.sha256(data).hexdigest() != digest:
                    raise LauncherError('Installed source differs from this launcher. It was preserved; select an empty --directory for a fresh install.')
            selected_config = saved_config(destination)
            if selected_config is None or selected_config.resolve() != config.resolve():
                raise LauncherError('This installation uses a different configuration. It was preserved; select a separate empty --directory or use its existing configuration.')
            return {'ok': True, 'already_installed': True, 'version': manifest['bridge_version']}
        result = installer.install(source, destination, config)
        receipt = destination / RECEIPT
        with receipt.open('x', encoding='utf-8') as handle:
            if os.name != 'nt': receipt.chmod(0o600)
            json.dump({'format': 1, 'bundle_id': bundle_id, 'version': manifest['bridge_version']}, handle)
        return {'ok': True, 'installed': True, 'version': manifest['bridge_version'],
                'registration_required': result['registration_required']}


def saved_config(destination):
    launcher = destination / '.mcp.json'
    if not launcher.is_file():
        return None
    if launcher.stat().st_size > 65536:
        raise LauncherError('Installed launcher metadata exceeds its size limit.')
    document = json.loads(launcher.read_text(encoding='utf-8-sig'))
    if not isinstance(document, dict) or not isinstance(document.get('mcpServers'), dict):
        raise LauncherError('Invalid installed launcher metadata. Existing files were preserved.')
    entry = document['mcpServers'].get('codex_bridge')
    if not isinstance(entry, dict):
        raise LauncherError('Installed launcher does not describe this Bridge.')
    arguments = entry.get('args', [])
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise LauncherError('Invalid installed launcher arguments.')
    if '--config' in arguments:
        index = arguments.index('--config')
        if index + 1 < len(arguments):
            path = Path(arguments[index + 1]).expanduser()
            if not path.is_absolute():
                raise LauncherError('The installed configuration path is not local and absolute. Run setup on this computer.')
            return path
    return None


def dependencies():
    machine = platform.machine().lower()
    return {'python': platform.python_version(), 'platform': sys.platform,
            'architecture': machine, 'python_bits': struct.calcsize('P') * 8,
            'supported_architecture': machine in ('x86_64', 'amd64', 'aarch64', 'arm64') and struct.calcsize('P') == 8,
            'codex_on_path': bool(shutil.which('codex')), 'ssh_on_path': bool(shutil.which('ssh')),
            'native_tool_execution': 'not_checked'}


def main(argv=None, archive_path=None):
    parser = argparse.ArgumentParser(description='Codex Bridge one-file launcher. Requires local Python 3.11+, Codex, and SSH. No credentials or interpreters are bundled.',
        epilog='Global --directory and --config go before COMMAND. Commands: install, setup, doctor, start, stop, status, preflight, and other Bridge CLI commands. Linux: start --background. Windows: start --interactive.')
    parser.add_argument('--directory', type=Path, default=Path.home() / 'plugins' / 'codex-bridge')
    parser.add_argument('--config', type=Path)
    parser.add_argument('command', nargs='?', default='setup')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        facts = dependencies()
        if args.command == 'doctor':
            print(json.dumps({'ok': facts['supported_architecture'], **facts,
                'note': 'Dependency hints only. Select a complete native Codex distribution, sign in locally, and run preflight after setup. Linux startup is manual unless the owner explicitly configures a user-session service.'}, indent=2))
            return 0 if facts['supported_architecture'] else 1
        if not facts['supported_architecture']:
            raise LauncherError('Use 64-bit Python on x86_64 or aarch64 with a matching native Codex installation.')
        destination = args.directory.expanduser().absolute()
        config = (args.config or os.environ.get('CODEX_BRIDGE_CONFIG') or saved_config(destination)
                  or Path.home() / '.codex-bridge' / 'config.json')
        config = Path(config).expanduser().absolute()
        arguments = list(args.arguments)
        if args.command in ('install', 'setup'):
            if args.command == 'install' and arguments:
                raise LauncherError('install takes no command options. Put --directory and --config before install.')
            with zipfile.ZipFile(archive_path or sys.argv[0]) as archive:
                manifest, bundle_id = bundle_manifest(archive)
                result = ensure_install(archive, manifest, bundle_id, destination, config)
            if args.command == 'install':
                print(json.dumps(result, indent=2))
                return 0
            skip_registration = '--skip-registration' in arguments
            arguments = [value for value in arguments if value != '--skip-registration']
            if not skip_registration and '--register-mcp' not in arguments:
                arguments.append('--register-mcp')
            if '--batch' not in arguments and '--guided' not in arguments:
                arguments.append('--guided')
        if not (destination / 'codex_bridge' / 'cli.py').is_file():
            raise LauncherError('Bridge is not installed in this directory. Run this launcher with install or setup first.')
        environment = dict(os.environ, PYTHONPATH=str(destination))
        return subprocess.run([sys.executable, '-I', str(destination / 'scripts' / 'run_bridge.py'), '--config', str(config),
            args.command, *arguments], env=environment).returncode
    except (LauncherError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        # Raw filesystem errors may contain personal paths. Expected guidance is
        # deliberate; arbitrary OS/JSON/ZIP exception text never crosses stdout.
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, LauncherError) else 'Launcher operation failed. Check the selected local files and use a complete, verified launcher download.'
        print(json.dumps({'ok': False, 'error': {'code': 'launcher_failed', 'message': message}}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
