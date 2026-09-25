"""Linux user-unit transactions with a fake manager; no system services changed."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_bridge import autostart, autostart_posix as units, cli
from codex_bridge.core import BridgeError


class UserManager:
    def __init__(self, directory):
        self.directory = directory
        self.calls = []
        self.enabled = set()
        self.active = set()
        self.fail_once = None
        self.overrides = {}

    def __call__(self, *args, check=True):
        self.calls.append(args)
        operation = args[0]
        name = args[1] if len(args) > 1 else None
        if operation == self.fail_once:
            self.fail_once = None
            raise BridgeError('fixture_failure', 'Fixture owner manager failure')
        if operation == 'show':
            exists = (self.directory / name).exists()
            info = {'LoadState': 'loaded' if exists else 'not-found',
                    'ActiveState': 'active' if name in self.active else 'inactive',
                    'UnitFileState': 'enabled' if name in self.enabled else 'disabled',
                    'FragmentPath': str(self.directory / name) if exists else '', 'DropInPaths': ''}
            info.update(self.overrides)
            return subprocess.CompletedProcess(args, 0, '\n'.join(k + '=' + v for k, v in info.items()), '')
        if operation == 'enable':
            self.enabled.add(name)
        elif operation == 'disable':
            self.enabled.discard(name)
        elif operation == 'start':
            self.active.add(name)
        return subprocess.CompletedProcess(args, 0, '', '')


class UserUnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bridge-user-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / 'config units' / 'systemd' / 'user'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.path = self.root / 'config.json'
        self.path.write_text(json.dumps({'version': 1, 'peer_id': 'local', 'listen_port': 47321,
            'state_dir': str(self.state), 'codex_path': sys.executable, 'local_token': 'fixture-' + 'x' * 32,
            'peers': {'other': {'enabled': True}}, 'projects': {},
            'ssh_transports': {'other': {'enabled': True}}}))
        self.manager = UserManager(self.directory)
        self.enterContext(mock.patch.object(units, '_require_linux'))
        self.enterContext(mock.patch.object(units, '_unit_directory', return_value=self.directory))
        self.enterContext(mock.patch.object(units, '_systemctl', side_effect=self.manager))
        self.enterContext(mock.patch.object(autostart, '_uses_windows', return_value=False))

    def unit(self, component='daemon', peer=None):
        return self.directory / units.unit_name(self.path, component, peer)

    def test_default_registers_only_waiting_owner_daemon_before_pairing(self):
        cfg = cli.read(self.path); cfg['peers'] = {}; cfg.pop('ssh_transports'); cli.save(self.path, cfg)
        before = self.path.read_bytes()
        result = autostart.enable(self.path)
        self.assertEqual(result['enabled_components'], [{'component': 'daemon', 'peer_id': None}])
        self.assertFalse(result['started_now'])
        self.assertFalse(result['linger_changed'])
        self.assertEqual(before, self.path.read_bytes())
        content = self.unit().read_text()
        self.assertIn('autostart-run', content)
        self.assertIn('"daemon"', content)
        self.assertIn('Restart=no', content)
        for absent in ('transport-run', '"transport"', 'sudo', 'User=root', 'ssh', 'linger'):
            self.assertNotIn(absent, content)
        self.assertNotIn('start', [call[0] for call in self.manager.calls])
        self.assertEqual(len(list(self.directory.glob('*.service'))), 1)

    def test_explicit_persistent_peer_has_separate_unit_and_no_restart_loop(self):
        with mock.patch('codex_bridge.transport.transport_args'):
            result = autostart.enable(self.path, 'transport', 'other')
        self.assertEqual(result['enabled_components'], [{'component': 'transport', 'peer_id': 'other'}])
        self.assertFalse(self.unit().exists())
        text = self.unit('transport', 'other').read_text()
        self.assertIn('Type=oneshot', text)
        self.assertIn('"--peer" "other"', text)
        self.assertIn('Restart=no', text)
        self.assertNotIn('transport-run', text)
        self.assertNotIn('start', [call[0] for call in self.manager.calls])

    def test_disable_preserves_active_daemon_and_remove_refuses_it(self):
        autostart.enable(self.path)
        name = units.unit_name(self.path, 'daemon')
        self.manager.active.add(name)
        result = autostart.disable(self.path, 'daemon')
        self.assertFalse(result['running_work_stopped'])
        self.assertIn(name, self.manager.active)
        self.assertTrue(self.unit().exists())
        self.assertNotIn(name, self.manager.enabled)
        self.assertNotIn('stop', [call[0] for call in self.manager.calls])
        with self.assertRaises(BridgeError) as caught:
            autostart.disable(self.path, 'daemon', remove=True)
        self.assertEqual(caught.exception.code, 'component_running')
        self.assertTrue(self.unit().exists())

    def test_removal_deletes_only_owned_unit_keeps_state_keys_projects(self):
        autostart.enable(self.path)
        unrelated = self.directory / 'unrelated.service'; unrelated.write_text('unrelated')
        results = self.state / 'saved-results.json'; results.write_text('saved work')
        before = self.path.read_bytes()
        result = autostart.disable(self.path, 'daemon', remove=True)
        self.assertTrue(result['registrations_removed'])
        self.assertFalse(self.unit().exists())
        self.assertEqual(unrelated.read_text(), 'unrelated')
        self.assertEqual(results.read_text(), 'saved work')
        self.assertEqual(before, self.path.read_bytes())

    def test_start_is_explicit_and_status_separates_unit_from_peer_readiness(self):
        autostart.enable(self.path)
        result = autostart.start(self.path, 'daemon')
        self.assertEqual(result['launch_mode'], 'owner-user-service')
        result = autostart.status(self.path)
        self.assertEqual(result['components'][0]['state'], 'active')
        self.assertFalse(result['components'][0]['runner']['running'])
        self.assertIn('does not verify SSH', result['readiness'])

    def test_repeated_enable_preserves_running_identical_unit(self):
        autostart.enable(self.path)
        self.manager.active.add(units.unit_name(self.path, 'daemon'))
        before = self.unit().read_bytes()
        autostart.enable(self.path)
        self.assertEqual(before, self.unit().read_bytes())
        self.assertNotIn('restart', [call[0] for call in self.manager.calls])

    def test_foreign_unit_or_override_is_not_modified_or_started(self):
        self.directory.mkdir(parents=True)
        self.unit().write_text('someone else owns this')
        for operation in (lambda: autostart.enable(self.path), lambda: autostart.disable(self.path, 'daemon')):
            with self.assertRaises(BridgeError) as caught:
                operation()
            self.assertEqual(caught.exception.code, 'startup_ownership_conflict')
        self.assertEqual(self.unit().read_text(), 'someone else owns this')
        self.unit().unlink()
        self.manager.overrides['DropInPaths'] = '/owner/other.conf'
        with self.assertRaises(BridgeError) as caught:
            autostart.enable(self.path)
        self.assertEqual(caught.exception.code, 'startup_ownership_conflict')
        self.assertFalse(self.unit().exists())

    def test_unit_edit_after_registration_is_preserved_until_owner_repairs_it(self):
        autostart.enable(self.path)
        self.unit().write_text('edited unit')
        with self.assertRaises(BridgeError):
            autostart.start(self.path, 'daemon')
        with self.assertRaises(BridgeError):
            autostart.disable(self.path, 'daemon', remove=True)
        self.assertEqual(self.unit().read_text(), 'edited unit')

    def test_failure_restores_preexisting_unit_and_enabled_state(self):
        autostart.enable(self.path)
        raw = self.unit().read_bytes()
        saved = (self.state / 'autostart.json').read_bytes()
        self.manager.fail_once = 'enable'
        with self.assertRaises(BridgeError) as caught:
            autostart.enable(self.path)
        self.assertEqual(caught.exception.code, 'fixture_failure')
        self.assertEqual(raw, self.unit().read_bytes())
        self.assertEqual(saved, (self.state / 'autostart.json').read_bytes())
        self.assertIn(units.unit_name(self.path, 'daemon'), self.manager.enabled)

    def test_first_install_failure_removes_only_new_unit_without_state(self):
        self.manager.fail_once = 'enable'
        with self.assertRaises(BridgeError):
            autostart.enable(self.path)
        self.assertFalse(self.unit().exists())
        self.assertFalse((self.state / 'autostart.json').exists())
        self.assertNotIn(units.unit_name(self.path, 'daemon'), self.manager.enabled)

    def test_saved_settings_write_failure_rolls_back_registered_unit(self):
        with mock.patch.object(cli, 'save', side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):
                autostart.enable(self.path)
        self.assertFalse(self.unit().exists())
        self.assertNotIn(units.unit_name(self.path, 'daemon'), self.manager.enabled)

    def test_unit_quotes_percent_dollar_quotes_backslash_and_rejects_controls(self):
        quote = units._quote('/owner/a b/%n/$HOME/"c"/\\file', command=True)
        self.assertIn('%%n', quote)
        self.assertIn('$$HOME', quote)
        self.assertIn('\\"c\\"', quote)
        self.assertIn('\\\\file', quote)
        self.assertTrue(quote.startswith('"') and quote.endswith('"'))
        for value in ('', 'line\nbreak', 'line\rbreak', 'tab\t', 'null\0', '\x7f'):
            with self.assertRaises(BridgeError):
                units._quote(value, command=True)

    def test_distinct_config_and_peer_units_never_collide(self):
        names = {units.unit_name(self.root / name, component, peer) for name in ('A.json', 'a.json')
                 for component, peer in (('daemon', None), ('transport', 'one'), ('transport', 'two'))}
        self.assertEqual(len(names), 6)

    def test_systemctl_always_uses_user_manager_without_shell(self):
        # Use the original function despite the fixture's injected fake manager.
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            ORIGINAL_SYSTEMCTL('enable', 'test.service')
        self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'enable', 'test.service'])
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_missing_unit_is_distinct_from_missing_user_manager(self):
        with mock.patch.object(units, '_systemctl', return_value=subprocess.CompletedProcess([], 4, 'LoadState=not-found\n', '')):
            self.assertEqual(units._inspect('new.service')['LoadState'], 'not-found')
        with mock.patch.object(units, '_systemctl', return_value=subprocess.CompletedProcess([], 1, '', 'private diagnostics')):
            with self.assertRaises(BridgeError) as caught:
                units._inspect('new.service')
        self.assertEqual(caught.exception.code, 'user_service_unavailable')
        self.assertNotIn('private diagnostics', str(caught.exception))

    def test_root_setup_refused_and_no_linger_operation(self):
        with mock.patch.object(sys, 'platform', 'linux'), mock.patch.object(os, 'geteuid', return_value=0, create=True):
            with self.assertRaises(BridgeError) as caught:
                ORIGINAL_REQUIRE_LINUX()
        self.assertEqual(caught.exception.code, 'owner_session_required')
        self.assertEqual(self.manager.calls, [])


ORIGINAL_SYSTEMCTL = units._systemctl
ORIGINAL_REQUIRE_LINUX = units._require_linux


if __name__ == '__main__':
    unittest.main()
