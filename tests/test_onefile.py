"""Real zipapp/bootstrap subprocess checks in isolated folders, without accounts."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    loaded = importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


builder = module('bridge_launcher_builder', ROOT / 'scripts' / 'build_launcher.py')
bootstrap = module('bridge_launcher_bootstrap', ROOT / 'scripts' / 'onefile.py')


class OneFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-onefile-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bundle = self.root / 'codex-bridge.pyz'
        self.destination = self.root / 'folder with spaces' / 'codex-bridge'
        self.config = self.root / 'private owner' / 'config.json'

    def build(self):
        return builder.build(ROOT, self.bundle)

    def run_bundle(self, *arguments):
        return subprocess.run([sys.executable, str(self.bundle), '--directory', str(self.destination),
            '--config', str(self.config), *arguments], capture_output=True, text=True, timeout=30)

    def test_deterministic_bundle_has_only_allowlisted_sources_and_no_accounts(self):
        first = self.build(); second_path = self.root / 'second.pyz'
        second = builder.build(ROOT, second_path)
        self.assertEqual(first['sha256'], second['sha256'])
        with zipfile.ZipFile(self.bundle) as archive:
            manifest, _ = bootstrap.bundle_manifest(archive)
            self.assertIn('scripts/setup.sh', manifest['files'])
            self.assertNotIn('.mcp.json', manifest['files'])
            self.assertFalse(any(name.startswith(('tests/', 'state/', '.git/')) for name in manifest['files']))
            self.assertFalse(any(name.endswith(('auth.json', '.key', '.pem')) for name in manifest['files']))
            for name in manifest['files']:
                if name.endswith('.sh'):
                    self.assertNotIn(b'\r\n', archive.read('payload/' + name))

    def test_help_and_dependency_hints_do_not_install_or_read_auth(self):
        self.build()
        for args in (('--help',), ('doctor',)):
            result = self.run_bundle(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        facts = json.loads(self.run_bundle('doctor').stdout)
        self.assertEqual(facts['native_tool_execution'], 'not_checked')
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.config.exists())

    def test_install_reuse_and_dispatch_keep_config_private_and_unchanged(self):
        self.build()
        self.config.parent.mkdir(); self.config.write_text('private sentinel preserved')
        first = self.run_bundle('install')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)['installed'])
        second = self.run_bundle('install')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(json.loads(second.stdout)['already_installed'])
        self.assertEqual(self.config.read_text(), 'private sentinel preserved')
        help_result = self.run_bundle('status', '--help')
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('usage:', help_result.stdout)
        self.assertNotIn('private sentinel', first.stdout + second.stdout + help_result.stdout)
        saved = json.loads((self.destination / '.mcp.json').read_text())['mcpServers']['codex_bridge']
        self.assertEqual(saved['command'], sys.executable)
        self.assertEqual(saved['args'][-1], str(self.config))
        self.assertEqual(bootstrap.saved_config(self.destination), self.config)

    def test_existing_installation_and_modified_source_are_never_overwritten(self):
        self.build()
        self.destination.mkdir(parents=True)
        sentinel = self.destination / 'owner-file.txt'; sentinel.write_text('keep')
        result = self.run_bundle('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('preserved', result.stderr)
        self.assertEqual(sentinel.read_text(), 'keep')
        sentinel.unlink()
        self.assertEqual(self.run_bundle('install').returncode, 0)
        installed = self.destination / 'codex_bridge' / 'cli.py'
        installed.write_text('owner modification')
        result = self.run_bundle('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(installed.read_text(), 'owner modification')

    def test_corrupt_payload_rejected_before_writing_installation(self):
        self.build()
        changed = self.root / 'changed.pyz'
        with zipfile.ZipFile(self.bundle) as source, zipfile.ZipFile(changed, 'w') as dest:
            for item in source.infolist():
                dest.writestr(item, b'corrupt' if item.filename == 'payload/README.md' else source.read(item))
        self.bundle = changed
        result = self.run_bundle('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum', result.stderr)
        self.assertFalse(self.destination.exists())

    def test_different_setup_config_is_refused_before_identity_or_launcher_change(self):
        self.build()
        self.assertEqual(self.run_bundle('install').returncode, 0)
        launcher = self.destination / '.mcp.json'
        before = launcher.read_bytes()
        self.config = self.root / 'different-private' / 'config.json'
        result = self.run_bundle('setup', '--batch', '--peer-id', 'different-computer', '--skip-registration')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('different configuration', result.stderr)
        self.assertEqual(launcher.read_bytes(), before)
        self.assertFalse(self.config.exists())

    def test_malformed_metadata_has_no_traceback_and_preserves_existing_files(self):
        self.build()
        self.assertEqual(self.run_bundle('install').returncode, 0)
        launcher = self.destination / '.mcp.json'
        original = launcher.read_bytes()
        for value in ([], {'mcpServers': []}, {'mcpServers': {'codex_bridge': []}},
                      {'mcpServers': {'codex_bridge': {'args': [42]}}}):
            launcher.write_text(json.dumps(value))
            result = self.run_bundle('install')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(json.loads(launcher.read_text()), value)
        launcher.write_bytes(original)
        (self.destination / bootstrap.RECEIPT).write_text('[]')
        result = self.run_bundle('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)

    def test_oversized_modified_file_is_refused_without_reading_its_contents(self):
        self.build()
        self.assertEqual(self.run_bundle('install').returncode, 0)
        installed = self.destination / 'codex_bridge' / 'cli.py'
        with installed.open('wb') as handle:
            handle.truncate(100 * 1024 * 1024)
        result = self.run_bundle('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source differs', result.stderr)
        self.assertEqual(installed.stat().st_size, 100 * 1024 * 1024)

    def test_traversal_and_extra_entries_rejected(self):
        self.build()
        with zipfile.ZipFile(self.bundle, 'a') as archive:
            archive.writestr('payload/../../private', b'fixture')
        result = self.run_bundle('install')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())

    def test_missing_installation_reports_actionable_error_without_creating_it(self):
        self.build()
        result = self.run_bundle('status')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('install or setup', result.stderr)
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
