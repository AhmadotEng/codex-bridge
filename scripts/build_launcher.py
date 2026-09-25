#!/usr/bin/env python3
"""Build a deterministic, allowlisted Python zipapp without local configuration."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(source, output):
    source = Path(source).absolute(); output = Path(output).absolute()
    spec = importlib.util.spec_from_file_location('bridge_build_installer', source / 'scripts' / 'install.py')
    installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
    selected = list(installer.REQUIRED) + [name for name in installer.OPTIONAL if (source / name).is_file()]
    contents = {}
    for name in selected:
        item = source / name
        installer.no_links(item, source)
        data = item.read_bytes()
        # Source checkouts on Windows may have CRLF; shell entry points must not.
        if name.endswith(('.py', '.sh', '.json', '.md')):
            data = data.replace(b'\r\n', b'\n')
        elif name.endswith('.ps1'):
            data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        contents[name] = data
    manifest = {'format': 1,
        'bridge_version': json.loads(contents['.codex-plugin/plugin.json'])['version'],
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())}}
    bootstrap = source / 'scripts' / 'onefile.py'
    installer.no_links(bootstrap, source)
    installer.no_links(output, output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(b'#!/usr/bin/env python3\n')
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            entries = {'__main__.py': b'from onefile import main\nraise SystemExit(main())\n',
                'onefile.py': bootstrap.read_bytes().replace(b'\r\n', b'\n'),
                'manifest.json': (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode(),
                **{'payload/' + name: value for name, value in contents.items()}}
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + '.sha256')
    with checksum.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(digest + '  ' + output.name + '\n')
    return {'ok': True, 'file': str(output), 'sha256': digest, 'version': manifest['bridge_version'],
            'payload_files': len(contents), 'python_required': '3.11+', 'accounts_bundled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output), indent=2))


if __name__ == '__main__':
    main()
