"""Exact archives are deterministic, complete, and exclude unselected local files."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_release_builder', ROOT/'scripts'/'build_release.py')
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    def test_os_packages_match_manifest_and_exclude_private_local_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base/'source'
            private_sentinel = os.urandom(48)
            installer = builder.load_script(ROOT, 'install')
            names = set(installer.REQUIRED) | set(installer.OPTIONAL) | set(builder.SOURCE_EXTRA)
            for name in names:
                if (ROOT/name).is_file():
                    target = source/name; target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT/name, target)
            for name in ('.mcp.json', 'config.json', 'state/token.json', '.ssh/id_ed25519',
                         'tests/private_fixture.json', 'docs/private-note.md', 'scripts/untracked.py'):
                target = source/name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(private_sentinel)
            first = builder.build(source, base/'first', revision='fixture-revision')
            second = builder.build(source, base/'second', revision='fixture-revision')
            self.assertEqual(first['files'], second['files'])
            prefix = 'codex-bridge-'+first['version']
            with zipfile.ZipFile(base/'first'/(prefix+'-windows.zip')) as archive:
                windows = {n.removeprefix('codex-bridge/'): archive.read(n) for n in archive.namelist()}
            with tarfile.open(base/'first'/(prefix+'-linux.tar.gz'), 'r:gz') as archive:
                linux = {m.name.removeprefix('codex-bridge/'): archive.extractfile(m).read() for m in archive.getmembers()}
                for entry in archive.getmembers():
                    self.assertTrue(entry.isfile())
                    if entry.name.endswith('.sh'):
                        self.assertEqual(entry.mode, 0o755)
            self.assertEqual(windows, linux)
            manifest = json.loads(windows.pop('PAYLOAD-MANIFEST.json'))
            self.assertEqual(manifest['source_revision'], 'fixture-revision')
            self.assertEqual(set(windows), set(manifest['files']))
            for name, data in windows.items():
                self.assertEqual(hashlib.sha256(data).hexdigest(), manifest['files'][name])
                self.assertNotIn(private_sentinel, data)
                if name.endswith('.sh'): self.assertNotIn(b'\r\n', data)
            for required in ('scripts/setup.ps1', 'scripts/setup.sh', 'scripts/run_bridge.py',
                             'codex_bridge/connections.py', 'codex_bridge/autostart_posix.py'):
                self.assertIn(required, windows)
            with zipfile.ZipFile(base/'first'/(prefix+'-source.zip')) as archive:
                self.assertIn('codex-bridge/scripts/build_release.py', archive.namelist())
                self.assertFalse(any(private_sentinel in archive.read(n) for n in archive.namelist()))
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                builder.build(source, base/'first', revision='fixture-revision')
            self.assertEqual(first['files'], {name: hashlib.sha256((base/'first'/name).read_bytes()).hexdigest()
                                              for name in first['files']})


if __name__ == '__main__': unittest.main()
