"""Focused local administration regressions; never start a live Codex daemon."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_bridge import cli
from codex_bridge.core import BridgeError


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-bridge-cli-')
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / 'config.json'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config = {'version': 1, 'peer_id': 'local-test', 'listen_port': 47321,
            'state_dir': str(self.state), 'codex_path': sys.executable,
            'local_token': 'test-only-local-token-' + 'x' * 32, 'peers': {}, 'projects': {}}
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

    def tearDown(self):
        self.temporary.cleanup()

    def transport_config(self):
        identity = self.root / 'selected-key'
        known_hosts = self.root / 'known-hosts'
        identity.write_text('test fixture, not a private key', encoding='utf-8')
        known_hosts.write_text('test fixture', encoding='utf-8')
        return {**self.config, 'ssh_transport': {'enabled': True, 'ssh_exe': sys.executable,
            'ssh_host': '127.0.0.1', 'ssh_port': 12222, 'username': 'local-user',
            'identity_file': str(identity), 'known_hosts_file': str(known_hosts),
            'host_key_alias': 'paired-computer', 'local_peer_port': 47322,
            'remote_bridge_port': 47321, 'remote_peer_port': 47322}}

    def test_start_uses_local_probe_and_does_not_spawn_when_peer_is_offline(self):
        def probe(path, method, params, timeout):
            self.assertEqual(method, 'session_list')
            return {'ok': True, 'result': {'sessions': []}}
        with mock.patch.object(cli, 'call', side_effect=probe), mock.patch.object(cli, 'launch') as launch:
            result = cli.start_daemon(self.path)
        self.assertTrue(result['already_running'])
        launch.assert_not_called()

    def test_start_rejects_unauthorized_endpoint_instead_of_claiming_readiness(self):
        with mock.patch.object(cli, 'call', return_value={'ok': False, 'error': {
                'code': 'unauthorized', 'message': 'Other configuration owns this port'}}), \
                mock.patch.object(cli, 'launch') as launch:
            with self.assertRaises(BridgeError) as caught:
                cli.start_daemon(self.path)
        self.assertEqual(caught.exception.code, 'unauthorized')
        launch.assert_not_called()

    def test_daemon_lifetime_lock_prevents_second_journal_open(self):
        with cli.process_lock(self.state / 'serve.lock'):
            with mock.patch.object(cli, 'Bridge') as bridge, contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(['--config', str(self.path), 'serve'])
            self.assertEqual(code, 1)
            bridge.assert_not_called()
        with cli.process_lock(self.state / 'serve.lock'):
            pass

    def test_init_protects_its_own_state_and_file_not_existing_parent(self):
        self.path.unlink()
        with mock.patch.object(cli, 'protect', wraps=cli.protect) as protect, \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(['--config', str(self.path), 'init', '--peer-id', 'new-computer',
                '--codex', sys.executable])
        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in protect.call_args_list], [self.state])
        self.assertGreaterEqual(len(json.loads(self.path.read_text())['local_token']), 32)

    def test_transport_args_pin_host_and_force_both_forwards_to_loopback(self):
        cfg = self.transport_config()
        args = cli.transport_args(cfg)
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertIn('BatchMode=yes', args)
        self.assertEqual(args[args.index('-L') + 1], '127.0.0.1:47322:127.0.0.1:47321')
        self.assertEqual(args[args.index('-R') + 1], '127.0.0.1:47322:127.0.0.1:47321')
        for key, value in [('ssh_port', 0), ('local_peer_port', '47322:0.0.0.0'),
                ('remote_peer_port', True), ('ssh_host', '-ProxyCommand=unexpected')]:
            broken = {**cfg, 'ssh_transport': {**cfg['ssh_transport'], key: value}}
            with self.subTest(key=key), self.assertRaises(BridgeError):
                cli.transport_args(broken)

    def test_disabled_transport_is_not_reported_as_started(self):
        cfg = self.transport_config()
        cfg['ssh_transport']['enabled'] = False
        self.path.write_text(json.dumps(cfg), encoding='utf-8')
        with mock.patch.object(cli, 'launch') as launch, contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(['--config', str(self.path), 'transport-start'])
        self.assertEqual(result, 1)
        launch.assert_not_called()

    def test_transport_stop_waits_before_immediate_restart(self):
        cfg = self.transport_config()
        self.path.write_text(json.dumps(cfg), encoding='utf-8')
        (self.state / 'transport.stop').touch()
        with mock.patch.object(cli, 'wait_for_exit', return_value=False), \
                mock.patch.object(cli, 'launch') as launch, contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(['--config', str(self.path), 'transport-start'])
        self.assertEqual(result, 1)
        self.assertTrue((self.state / 'transport.stop').exists())
        launch.assert_not_called()

    def test_transport_status_distinguishes_process_from_verified_connection(self):
        cli.save(self.state / 'transport-run.pid.json', {'pid': 12345})
        cli.save(self.state / 'transport-child.pid.json', {'pid': 12346})
        with mock.patch.object(cli, 'process_alive', side_effect=lambda pid: pid == 12345):
            result = cli.transport_status(self.path)
        self.assertTrue(result['supervisor']['running'])
        self.assertFalse(result['ssh_child']['running'])
        self.assertIn('does not verify SSH', result['note'])

    @unittest.skipUnless(os.name == 'nt', 'Windows interactive launcher')
    def test_interactive_launcher_has_no_credentials_or_trigger_and_cleans_exact_task(self):
        with mock.patch.object(cli, 'powershell') as powershell:
            result = cli.launch_interactive(self.path)
        script = powershell.call_args.args[0]
        self.assertIn('-LogonType Interactive -RunLevel Limited', script)
        self.assertIn('-ExecutionTimeLimit ([TimeSpan]::Zero)', script)
        self.assertNotIn('-Password', script)
        self.assertNotIn('-Trigger', script)
        self.assertNotIn('-RunLevel Highest', script)
        wrapper = self.state / 'interactive-serve.ps1'
        text = wrapper.read_text(encoding='utf-8-sig')
        self.assertIn('$child.WaitForExit()', text)
        self.assertIn('Unregister-ScheduledTask -TaskName ' + cli.ps_literal(result['task_name']), text)
        self.assertIn('-WindowStyle Hidden', text)
        # Parse generated scripts with the installed PowerShell parser without executing them.
        registration = self.state / 'registration.ps1'
        registration.write_text(script, encoding='utf-8-sig')
        parser_script = '\n'.join([
            "$ErrorActionPreference='Stop'",
            'foreach ($path in @(' + cli.ps_literal(wrapper) + ',' + cli.ps_literal(registration) + ')) {',
            '  $parseTokens=$null; $parseErrors=$null',
            '  [Management.Automation.Language.Parser]::ParseFile($path,[ref]$parseTokens,[ref]$parseErrors) | Out-Null',
            '  if ($parseErrors.Count) { throw ($parseErrors | Out-String) }',
            '}',
        ])
        cli.powershell(parser_script)

    @unittest.skipUnless(os.name == 'nt', 'Windows interactive launcher')
    def test_interactive_mode_is_persisted_for_subsequent_starts(self):
        success = {'ok': True, 'result': {'sessions': []}}
        with mock.patch.object(cli, 'call', side_effect=[ConnectionRefusedError(), success]), \
                mock.patch.object(cli, 'launch_interactive', return_value={'launch_mode': 'interactive', 'task_name': 'test'}) as launch:
            result = cli.start_daemon(self.path, interactive=True)
        self.assertTrue(result['ready'])
        launch.assert_called_once()
        self.assertEqual(cli.read(self.path)['launch_mode'], 'interactive')
        with mock.patch.object(cli, 'call', return_value=success):
            cli.start_daemon(self.path, interactive=False)
        self.assertEqual(cli.read(self.path)['launch_mode'], 'background')


if __name__ == '__main__':
    unittest.main()
