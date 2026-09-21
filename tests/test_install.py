"""Install into temporary folders only; never touch a live plugin or account."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe') or shutil.which('pwsh')
RUNTIME_FILES = (
    '.codex-plugin/plugin.json',
    'codex_bridge/__init__.py',
    'codex_bridge/artifacts.py',
    'codex_bridge/autostart.py',
    'codex_bridge/cli.py',
    'codex_bridge/codex_adapter.py',
    'codex_bridge/compatibility.py',
    'codex_bridge/core.py',
    'codex_bridge/local_actions.py',
    'codex_bridge/mcp.py',
    'codex_bridge/onboarding.py',
    'codex_bridge/processes.py',
    'codex_bridge/tools.py',
    'codex_bridge/transport.py',
    'scripts/bridge.ps1',
    'scripts/install.ps1',
    'scripts/setup.ps1',
    'scripts/bridge.sh',
    'scripts/install.py',
    'scripts/install.sh',
    'skills/collaborate/SKILL.md',
)


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell installer')
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-bridge-install-')
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source-bundle' / 'codex-bridge'
        self.target = self.root / 'installed' / 'codex-bridge'
        self.config = self.root / 'private' / 'config.json'
        self.config.parent.mkdir()
        self.config.write_text('existing private configuration', encoding='utf-8')
        for relative in RUNTIME_FILES:
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / relative, target)

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, target=None):
        return subprocess.run([
            POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(self.source / 'scripts' / 'install.ps1'),
            '-PythonExe', sys.executable,
            '-InstallDirectory', str(target or self.target),
            '-ConfigPath', str(self.config),
        ], capture_output=True, text=True, timeout=40)

    def test_only_runtime_allowlist_is_copied_and_launcher_is_local(self):
        excluded = (
            '.git/config', '.git/objects/private-object', '.env', 'config.json',
            '.codex-bridge/config.json', 'state/requests.json', 'secrets.txt',
            'codex_bridge/untracked_secret.py', 'codex_bridge/config.json',
            'codex_bridge/__pycache__/core.cpython-311.pyc',
            'scripts/private-key', 'scripts/__pycache__/cached.pyc',
            'skills/collaborate/untracked-secrets.txt',
            'skills/collaborate/__pycache__/cached.pyc',
            'tests/private-fixture.json', 'docs/private-notes.md',
            'THIS-PAIR.md', 'Codex-Bridge-Tailscale-Implementation-Report.md',
            'docs/tailscale-verification.json',
        )
        for relative in excluded:
            sentinel = self.source / relative
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text('private sentinel must not be copied', encoding='utf-8')
        (self.source / '.mcp.json').write_text('personal launcher sentinel', encoding='utf-8')
        (self.source / 'README.md').write_text('Public setup instructions', encoding='utf-8')
        (self.source / 'LICENSE').write_text('Public license text', encoding='utf-8')
        (self.source / 'docs' / 'SETUP.md').write_text('Public setup guide', encoding='utf-8')
        (self.source / 'docs' / 'TAILSCALE.md').write_text('Generic Tailscale guide', encoding='utf-8')
        (self.source / 'config.tailscale.example.json').write_text('{"placeholder":true}', encoding='utf-8')

        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = {p.relative_to(self.target).as_posix() for p in self.target.rglob('*') if p.is_file()}
        self.assertEqual(installed, set(RUNTIME_FILES) | {'.mcp.json', 'README.md', 'LICENSE', 'docs/SETUP.md',
                         'docs/TAILSCALE.md', 'config.tailscale.example.json'})
        self.assertTrue(json.loads(result.stdout)['registration_required'])
        self.assertEqual((self.target / 'docs' / 'SETUP.md').read_text(encoding='utf-8'), 'Public setup guide')
        for relative in RUNTIME_FILES:
            self.assertEqual((self.target / relative).read_bytes(), (self.source / relative).read_bytes())
        launcher = json.loads((self.target / '.mcp.json').read_text(encoding='utf-8'))
        server = launcher['mcpServers']['codex_bridge']
        self.assertEqual(Path(server['command']).resolve(), Path(sys.executable).resolve())
        self.assertEqual(Path(server['args'][-1]), self.config)
        self.assertEqual(Path(server['args'][1]), self.target / 'codex_bridge' / 'mcp.py')
        self.assertEqual(self.config.read_text(encoding='utf-8'), 'existing private configuration')

    def test_existing_destination_private_files_are_preserved(self):
        private = self.target / 'state' / 'existing.json'
        private.parent.mkdir(parents=True)
        private.write_text('preserve existing state', encoding='utf-8')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(private.read_text(encoding='utf-8'), 'preserve existing state')

    def test_rejects_invalid_or_nested_destination(self):
        for target in (self.root / 'wrong-name', self.source / 'nested' / 'codex-bridge'):
            with self.subTest(target=target):
                result = self.install(target)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(target.exists())

    def test_missing_runtime_file_fails_before_copying(self):
        (self.source / 'codex_bridge' / 'core.py').unlink()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Required plugin file is missing', result.stderr)
        self.assertFalse(self.target.exists())

    def junction(self, link, destination):
        link.parent.mkdir(parents=True, exist_ok=True)
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        script = 'New-Item -ItemType Junction -Path ' + quote(link) + ' -Value ' + quote(destination) + ' -ErrorAction Stop | Out-Null'
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            self.skipTest('Directory junction creation is unavailable on this Windows runner')

    def test_destination_junction_is_rejected_before_any_copy(self):
        outside = self.root / 'unrelated-owner-data'
        outside.mkdir()
        sentinel = outside / 'owner.txt'
        sentinel.write_text('preserve', encoding='utf-8')
        self.junction(self.target, outside)
        try:
            result = self.install()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('linked files or directories', result.stderr)
            self.assertEqual(list(outside.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(), 'preserve')
        finally:
            self.target.rmdir()

    def test_nested_destination_junction_is_rejected_before_other_runtime_files_are_written(self):
        outside = self.root / 'unrelated-modules'
        outside.mkdir()
        link = self.target / 'codex_bridge'
        self.junction(link, outside)
        try:
            result = self.install()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((self.target / '.codex-plugin').exists())
        finally:
            link.rmdir()

    def test_launcher_link_is_rejected_even_when_installing_in_place(self):
        outside = self.root / 'unrelated-owner-launcher.json'
        outside.write_text('preserve owner launcher', encoding='utf-8')
        launcher = self.source / '.mcp.json'
        try:
            launcher.symlink_to(outside)
        except OSError:
            self.skipTest('File symlink creation requires privileges unavailable on this Windows runner')
        try:
            result = self.install(self.source)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('linked files or directories', result.stderr)
            self.assertEqual(outside.read_text(), 'preserve owner launcher')
        finally:
            launcher.unlink()


if __name__ == '__main__':
    unittest.main()
