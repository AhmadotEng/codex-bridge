#!/usr/bin/env python3
"""Build reproducible Windows/Linux installer bundles from one explicit payload."""
import argparse
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_EXTRA = (
    '.gitignore', '.gitattributes', '.github/workflows/test.yml',
    'scripts/build_release.py', 'scripts/build_launcher.py', 'scripts/onefile.py',
    'tests/test_artifacts.py', 'tests/test_autostart.py', 'tests/test_autostart_posix.py',
    'tests/test_chat_status.py', 'tests/test_cli.py', 'tests/test_codex_adapter.py',
    'tests/test_compatibility.py', 'tests/test_connections.py', 'tests/test_connections_ssh.py', 'tests/test_core.py',
    'tests/test_connection_entrypoints.py', 'tests/test_connection_integration.py',
    'tests/test_install.py', 'tests/test_install_posix.py', 'tests/test_local_actions.py',
    'tests/test_mcp.py', 'tests/test_onboarding.py', 'tests/test_onefile.py',
    'tests/test_release.py', 'tests/test_runtime_platform.py', 'tests/test_runtime_readiness.py',
    'tests/test_setup_portable.py', 'tests/test_ssh_options.py', 'tests/test_transport.py',
    'tests/test_turn_ownership.py', 'tests/test_windows_file_io.py',
)


def load_script(source, name):
    spec = importlib.util.spec_from_file_location('bridge_release_' + name, source/'scripts'/(name+'.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def normalized(data, name):
    if name.endswith(('.py', '.sh', '.json', '.md', '.yml', '.ps1')):
        # UTF-8 BOM is unnecessary and can invalidate executable shell entries.
        data = data.removeprefix(b'\xef\xbb\xbf').replace(b'\r\n', b'\n')
    return data


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo('codex-bridge/' + name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


def tar_bytes(entries):
    buffer = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=buffer, mtime=0, compresslevel=9) as compressed:
        with tarfile.open(mode='w', fileobj=compressed, format=tarfile.PAX_FORMAT) as archive:
            for name, data in sorted(entries.items()):
                info = tarfile.TarInfo('codex-bridge/' + name)
                info.size = len(data); info.mode = 0o755 if name.endswith('.sh') else 0o644
                info.uid = info.gid = 0; info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def build(source, directory, *, revision='uncommitted', with_launcher=True):
    source = Path(source).absolute(); directory = Path(directory).absolute()
    installer = load_script(source, 'install')
    selected = list(installer.REQUIRED) + [name for name in installer.OPTIONAL if (source/name).is_file()]
    entries = {}
    for name in selected:
        installer.no_links(source/name, source)
        entries[name] = normalized((source/name).read_bytes(), name)
    version = json.loads(entries['.codex-plugin/plugin.json'])['version']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('Version is not a safe release filename component')
    manifest = {'format': 1, 'version': version, 'source_revision': revision,
                'python_required': '3.11+', 'private_configuration_included': False,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(entries.items())}}
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    entries['PAYLOAD-MANIFEST.json'] = manifest_bytes
    source_entries = dict(entries)
    for name in SOURCE_EXTRA:
        if (source/name).is_file():
            installer.no_links(source/name, source)
            source_entries[name] = normalized((source/name).read_bytes(), name)
    prefix = 'codex-bridge-' + version
    payloads = {prefix+'-windows.zip': zip_bytes(entries),
                prefix+'-linux.tar.gz': tar_bytes(entries),
                prefix+'-source.zip': zip_bytes(source_entries),
                prefix+'-manifest.json': manifest_bytes}
    expected = list(payloads) + ['SHA256SUMS']
    if with_launcher: expected += [prefix+'.pyz', prefix+'.pyz.sha256']
    installer.no_links(directory, directory.parent)
    for name in expected:
        installer.no_links(directory/name, directory.parent)
        if (directory/name).exists():
            raise ValueError('Refusing to overwrite an existing release artifact')
    directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, data in payloads.items():
        with (directory/name).open('xb') as output: output.write(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    if with_launcher:
        result = load_script(source, 'build_launcher').build(source, directory/(prefix+'.pyz'))
        hashes[prefix+'.pyz'] = result['sha256']
        hashes[prefix+'.pyz.sha256'] = hashlib.sha256((directory/(prefix+'.pyz.sha256')).read_bytes()).hexdigest()
    with (directory/'SHA256SUMS').open('x', encoding='utf-8', newline='\n') as output:
        output.write(''.join(digest+'  '+name+'\n' for name, digest in sorted(hashes.items())))
    return {'ok': True, 'version': version, 'source_revision': revision,
            'payload_files': len(manifest['files']), 'files': hashes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', required=True, type=Path)
    parser.add_argument('--revision', default='uncommitted', help='Exact source commit, or uncommitted for local candidates')
    parser.add_argument('--without-launcher', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output_directory, revision=args.revision,
                           with_launcher=not args.without_launcher), indent=2))


if __name__ == '__main__': main()
