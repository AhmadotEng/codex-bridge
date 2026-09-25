"""Fresh-owner onboarding and diagnostics without live Codex accounts or SSH."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from codex_bridge import cli, onboarding
from codex_bridge.core import BridgeError


def compatible(_):
    return {'ready': True, 'codex_version': '0.200.0', 'compatibility': 'schema-validated'}


def local_runner(argv, **kwargs):
    if argv[1:3] == ['login', 'status']:
        return subprocess.CompletedProcess(argv, 0, '', 'fixture-account-that-must-never-be-returned')
    if argv[1:3] == ['mcp', 'list']:
        return subprocess.CompletedProcess(argv, 0, '[]', '')
    return subprocess.CompletedProcess(argv, 0, '', '')


@contextlib.contextmanager
def endpoint(config_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            config = cli.read(config_path)
            tokens = [p['incoming_token'] for p in config['peers'].values() if p.get('enabled')]
            token = self.headers.get('Authorization', '').removeprefix('Bearer ')
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if token not in tokens:
                payload = {'ok': False, 'error': {'code': 'unauthorized', 'message': 'private-error-details'}}
            else:
                payload = {'ok': True, 'result': {'peer_id': config['peer_id'], 'codex': {'ready': True},
                    'projects': [{'project_id': name, 'allowed_ops': value['allowed_ops']}
                                 for name, value in config['projects'].items()]}}
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-fresh-owners-')
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / 'a' / 'config.json'
        self.b = self.root / 'b' / 'config.json'
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self, path=None, peer='computer-a', **kwargs):
        return onboarding.setup(path or self.a, peer_id=peer, codex=sys.executable,
            batch=True, runtime_probe=compatible, runner=local_runner, **kwargs)

    def paired(self):
        self.initialize()
        self.initialize(self.b, 'computer-b')
        a_export = self.root / 'exchange-a.json'
        b_export = self.root / 'exchange-b.json'
        onboarding.pair_setup(self.a, peer_id='computer-b', export_file=a_export, batch=True)
        onboarding.pair_setup(self.b, peer_id='computer-a', export_file=b_export, batch=True)
        onboarding.pair_setup(self.a, import_file=b_export, batch=True, consume_import=True)
        onboarding.pair_setup(self.b, import_file=a_export, batch=True, consume_import=True)

    def test_two_fresh_owners_generate_distinct_local_identity_and_private_pairing(self):
        self.paired()
        a, b = cli.read(self.a), cli.read(self.b)
        self.assertNotEqual(a['local_token'], b['local_token'])
        self.assertNotEqual(a['peers']['computer-b']['incoming_token'], b['peers']['computer-a']['incoming_token'])
        self.assertEqual(a['peers']['computer-b']['outgoing_token'], b['peers']['computer-a']['incoming_token'])
        self.assertEqual(b['peers']['computer-a']['outgoing_token'], a['peers']['computer-b']['incoming_token'])
        self.assertFalse((self.root / 'exchange-a.json').exists())
        self.assertFalse((self.root / 'exchange-b.json').exists())

    def test_setup_preserves_existing_private_state_actions_and_scopes(self):
        first = self.initialize(port=49010)
        cfg = cli.read(self.a)
        cfg['projects']['existing'] = {'local_actions': {'trusted': {'argv': ['fixed']}}, 'owner_extension': 1}
        cfg['peers']['retained-peer'] = {'incoming_token': 'private-token', 'enabled': False}
        cfg['owner_extension'] = {'keep': True}
        cli.save(self.a, cfg)
        result = onboarding.setup(self.a, batch=True, runtime_probe=compatible, runner=local_runner)
        self.assertFalse(result['initialized'])
        self.assertEqual(cli.read(self.a), cfg)
        self.assertTrue(first['initialized'])
        encoded = json.dumps(result)
        self.assertNotIn(cfg['local_token'], encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn('fixture-account', encoded)

    def test_runtime_failure_and_identity_change_do_not_modify_configuration(self):
        self.initialize()
        before = self.a.read_bytes()
        with self.assertRaisesRegex(BridgeError, 'identity'):
            onboarding.setup(self.a, peer_id='different-owner', batch=True, runtime_probe=compatible)
        with self.assertRaises(BridgeError) as failure:
            onboarding.setup(self.a, batch=True, runtime_probe=lambda _: {'ready': False})
        self.assertEqual(failure.exception.code, 'runtime_incompatible')
        self.assertEqual(before, self.a.read_bytes())

    def test_missing_batch_identity_fails_before_writing_any_configuration(self):
        with self.assertRaises(BridgeError) as failure:
            onboarding.setup(self.a, codex=sys.executable, batch=True, runtime_probe=compatible)
        self.assertEqual(failure.exception.code, 'missing_value')
        self.assertFalse(self.a.exists())

    def test_nonempty_state_is_not_reused_for_a_new_identity(self):
        state = self.a.parent / 'state'
        state.mkdir(parents=True)
        (state / 'history').write_text('preserve', encoding='utf-8')
        with self.assertRaises(BridgeError) as failure:
            self.initialize()
        self.assertEqual(failure.exception.code, 'already_initialized')
        self.assertFalse(self.a.exists())
        self.assertEqual((state / 'history').read_text(), 'preserve')

    def test_matching_invitation_is_reusable_but_other_computer_import_is_rejected(self):
        self.initialize()
        invitation = self.root / 'invitation.json'
        one = onboarding.pair_setup(self.a, peer_id='computer-b', export_file=invitation, batch=True)
        before = invitation.read_bytes()
        two = onboarding.pair_setup(self.a, peer_id='computer-b', export_file=invitation, batch=True)
        self.assertEqual(before, invitation.read_bytes())
        cfg = self.a.read_bytes()
        with self.assertRaises(BridgeError) as failure:
            onboarding.pair_setup(self.a, peer_id='computer-b', import_file=invitation, batch=True)
        self.assertEqual(failure.exception.code, 'peer_mismatch')
        self.assertEqual(cfg, self.a.read_bytes())
        token = cli.read(invitation)['receive_token']
        self.assertNotIn(token, json.dumps([one, two]))

    def test_invalid_invitation_and_nonloopback_url_leave_configuration_unchanged(self):
        self.initialize()
        invalid = self.root / 'bad.json'
        invalid.write_text('{bad secret data', encoding='utf-8')
        before = self.a.read_bytes()
        with self.assertRaises(BridgeError) as failure:
            onboarding.pair_setup(self.a, import_file=invalid, batch=True)
        self.assertNotIn('secret data', str(failure.exception))
        with self.assertRaises(BridgeError):
            onboarding.pair_setup(self.a, peer_id='computer-b', url='http://example.invalid:99', batch=True)
        self.assertEqual(before, self.a.read_bytes())

    def test_additional_peer_gets_own_port_and_preserves_legacy_route_owner(self):
        self.paired()
        cfg = cli.read(self.a)
        cfg['ssh_transport'] = {'enabled': True, 'owner_marker': 'keep-existing-route'}
        cli.save(self.a, cfg)
        onboarding.pair_setup(self.a, peer_id='computer-c', batch=True)
        updated = cli.read(self.a)
        self.assertEqual(updated['peers']['computer-c']['url'], 'http://127.0.0.1:47323')
        self.assertEqual(updated['peers']['computer-b'], cfg['peers']['computer-b'])
        self.assertEqual(updated['ssh_transport'], {**cfg['ssh_transport'], 'peer_id': 'computer-b'})

    def test_project_update_preserves_unrelated_projects_peers_and_fixed_actions(self):
        self.paired()
        onboarding.project_select(self.a, project_id='sample', name='Example', workspace=self.workspace,
                                  peer_id='computer-b', batch=True)
        cfg = cli.read(self.a)
        cfg['projects']['sample']['local_actions'] = {'build': {'argv': ['fixed-executable']}}
        cfg['projects']['sample']['owner_extension'] = {'retained': 1}
        cfg['projects']['second'] = {'marker': 'unchanged'}
        cli.save(self.a, cfg)
        result = onboarding.project_select(self.a, project_id='sample', name='New name', batch=True)
        updated = cli.read(self.a)
        self.assertEqual(updated['projects']['sample']['local_actions'], cfg['projects']['sample']['local_actions'])
        self.assertEqual(updated['projects']['sample']['owner_extension'], {'retained': 1})
        self.assertEqual(updated['projects']['second'], {'marker': 'unchanged'})
        self.assertEqual(updated['peers'], cfg['peers'])
        self.assertFalse(result['new_session_required'])
        other = self.root / 'other-project'
        other.mkdir()
        result = onboarding.project_select(self.a, project_id='sample', workspace=other,
                                           policy='workspace-write', batch=True)
        self.assertTrue(result['new_session_required'])
        self.assertEqual(cli.read(self.a)['projects']['sample']['export_root'], str(other / 'bridge-export'))

    def test_outside_transfer_root_does_not_change_project_or_create_location(self):
        self.paired()
        before = self.a.read_bytes()
        outside = self.root / 'outside'
        with self.assertRaises(BridgeError) as failure:
            onboarding.project_select(self.a, project_id='sample', workspace=self.workspace,
                                      peer_id='computer-b', export_root=outside, batch=True)
        self.assertEqual(failure.exception.code, 'scope_denied')
        self.assertEqual(before, self.a.read_bytes())
        self.assertFalse(outside.exists())

    def test_mcp_registration_refuses_to_overwrite_a_different_existing_entry(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps([{'name': 'codex_bridge',
                'transport': {'command': sys.executable, 'args': ['different-config']}}]), '')
        with self.assertRaises(BridgeError) as failure:
            onboarding.register_mcp(sys.executable, self.a, runner=runner)
        self.assertEqual(failure.exception.code, 'registration_conflict')
        self.assertEqual(len(calls), 1)

    def test_mcp_registration_adds_only_explicit_argv_and_matching_entry_is_idempotent(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return local_runner(argv, **kwargs)
        result = onboarding.register_mcp(sys.executable, self.a, runner=runner)
        self.assertTrue(result['registered'])
        self.assertEqual(calls[-1][1:5], ['mcp', 'add', 'codex_bridge', '--'])
        matching = {'name': 'codex_bridge', 'enabled': True, 'transport': {
            'command': sys.executable, 'args': calls[-1][6:]}}
        result = onboarding.register_mcp(sys.executable, self.a, runner=lambda argv, **kwargs:
            subprocess.CompletedProcess(argv, 0, json.dumps([matching]), ''))
        self.assertTrue(result['already_registered'])

    def test_fresh_two_computer_pairing_preflight_and_project_scope_over_real_loopback_http(self):
        self.paired()
        second = self.root / 'b-workspace'
        second.mkdir()
        for path, workspace, peer in ((self.a, self.workspace, 'computer-b'), (self.b, second, 'computer-a')):
            onboarding.project_select(path, project_id='sample', workspace=workspace, peer_id=peer, batch=True)
        with endpoint(self.a) as a_url, endpoint(self.b) as b_url:
            a, b = cli.read(self.a), cli.read(self.b)
            a['peers']['computer-b']['url'] = b_url
            b['peers']['computer-a']['url'] = a_url
            cli.save(self.a, a); cli.save(self.b, b)
            for path in (self.a, self.b):
                result = onboarding.preflight(path, project_id='sample', runtime_probe=compatible,
                    runner=local_runner, local_call=lambda *a, **k: {'ok': True})
                self.assertTrue(result['ok'], result)
                self.assertNotIn(str(self.root), json.dumps(result))
            b['projects'].clear()
            cli.save(self.b, b)
            result = onboarding.preflight(self.a, project_id='sample', runtime_probe=compatible,
                runner=local_runner, local_call=lambda *a, **k: {'ok': True})
            self.assertFalse(result['ok'])
            self.assertIn('peer_project_scope_missing', [item['code'] for item in result['checks']])
            b['peers']['computer-a']['enabled'] = False
            cli.save(self.b, b)
            result = onboarding.preflight(self.a, runtime_probe=compatible,
                runner=local_runner, local_call=lambda *a, **k: {'ok': True})
            self.assertIn('peer_identity_or_credential', [item['code'] for item in result['checks']])
            self.assertNotIn('private-error-details', json.dumps(result))

    def test_preflight_distinguishes_peer_runtime_failure_from_forwarding(self):
        self.paired()
        result = onboarding.preflight(self.a, runtime_probe=compatible, runner=local_runner,
            probe=lambda _: {'ok': False, 'code': 'unsupported_codex_schema'},
            local_call=lambda *a, **k: {'ok': True})
        failures = [item['category'] for item in result['checks'] if item['state'] == 'fail']
        self.assertIn('peer_runtime', failures)
        self.assertNotIn('forwarding', failures)

    def test_missing_runtime_companions_preserve_existing_configuration_and_scopes(self):
        self.paired()
        before = self.a.read_bytes()
        with self.assertRaises(BridgeError) as failure:
            onboarding.setup(self.a, codex=sys.executable, batch=True, runner=local_runner,
                runtime_probe=lambda _: {'ready': False, 'schema_ready': True,
                    'compatibility': 'schema-validated',
                    'runtime_bundle': {'status': 'incomplete'},
                    'errors': [{'message': 'PRIVATE-PATH'}]})
        self.assertEqual(failure.exception.code, 'runtime_bundle_incomplete')
        self.assertNotIn('PRIVATE-PATH', str(failure.exception))
        self.assertEqual(self.a.read_bytes(), before)

    def test_native_runtime_permission_and_format_errors_are_actionable_and_sanitized(self):
        self.initialize()
        before = self.a.read_bytes()
        for code in ('runtime_not_executable', 'runtime_exec_format'):
            probe = lambda _: {'ready': False, 'errors': [{'code': code, 'message': 'PRIVATE-PATH'}]}
            result = onboarding.preflight(self.a, runtime_probe=probe, runner=local_runner,
                local_call=lambda *a, **k: {'ok': True})
            self.assertIn(code, [row['code'] for row in result['checks']])
            self.assertNotIn('PRIVATE-PATH', json.dumps(result))
            with self.assertRaises(BridgeError) as failure:
                onboarding.setup(self.a, codex=sys.executable, batch=True, runtime_probe=probe)
            self.assertEqual(failure.exception.code, code)
            self.assertEqual(self.a.read_bytes(), before)

    def test_runtime_path_repair_changes_only_selected_path(self):
        self.paired()
        cfg = cli.read(self.a)
        cfg['codex_path'] = str(self.root / 'old-incomplete-runtime')
        cfg['listen_port'] = 49990
        cfg['projects']['existing'] = {'local_actions': {'fixture': {'argv': ['fixed']}}}
        cfg['ssh_transports'] = {'computer-b': {'enabled': False, 'owner_extension': 'preserved'}}
        cli.save(self.a, cfg)
        result = onboarding.setup(self.a, codex=sys.executable, batch=True,
            runtime_probe=lambda _: {**compatible(None), 'runtime_bundle': {'status': 'complete'}},
            runner=local_runner)
        repaired = cli.read(self.a)
        self.assertEqual(Path(repaired.pop('codex_path')), Path(sys.executable).resolve())
        cfg.pop('codex_path')
        self.assertEqual(repaired, cfg)
        self.assertEqual(result['runtime_bundle_status'], 'complete')
        self.assertEqual(result['native_tool_execution'], 'not_checked')
        self.assertNotIn('registration', result)

    def test_preflight_separates_schema_bundle_and_native_execution(self):
        self.paired()
        for status, ok in (('complete', True), ('incomplete', False), ('unknown', True), ('not_applicable', True)):
            with self.subTest(status=status):
                result = onboarding.preflight(self.a,
                    runtime_probe=lambda _: {'schema_ready': True, 'ready': status != 'incomplete',
                        'codex_version': 'PRIVATE-PATH', 'runtime_bundle': {
                            'status': status, 'missing_files': ['PRIVATE-FILENAME'], 'error': 'PRIVATE-ERROR'}},
                    runner=local_runner,
                    probe=lambda _: {'ok': True, 'peer_id': 'computer-b', 'runtime_ready': True},
                    local_call=lambda *a, **k: {'ok': True})
                self.assertEqual(result['ok'], ok, result)
                self.assertEqual(next(row['state'] for row in result['checks'] if row['category'] == 'runtime'), 'pass')
                self.assertEqual(result['native_tool_execution'], 'not_checked')
                self.assertNotIn('PRIVATE-', json.dumps(result))
                if status == 'unknown':
                    row = next(row for row in result['checks'] if row['category'] == 'runtime_bundle')
                    self.assertEqual(row['state'], 'unknown')
                    self.assertFalse(row['required'])

    def test_peer_bundle_failure_does_not_report_broken_forwarding(self):
        self.paired()
        result = onboarding.preflight(self.a, runtime_probe=compatible, runner=local_runner,
            probe=lambda _: {'ok': False, 'code': 'runtime_bundle_incomplete', 'message': 'PRIVATE-PATH'},
            local_call=lambda *a, **k: {'ok': True})
        self.assertFalse(result['ok'])
        self.assertEqual(next(row['state'] for row in result['checks'] if row['category'] == 'forwarding'), 'pass')
        self.assertIn('peer_runtime', [row['category'] for row in result['checks'] if row['state'] == 'fail'])
        self.assertNotIn('PRIVATE-PATH', json.dumps(result))

    def test_preflight_never_emits_raw_auth_or_runtime_errors(self):
        self.initialize()
        def runtime(_):
            raise OSError('PRIVATE-PATH-AND-ERROR')
        result = onboarding.preflight(self.a, runtime_probe=runtime,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, 'PRIVATE-ACCOUNT-TOKEN', ''),
            local_call=lambda *a, **k: {'ok': False, 'error': {'message': 'PRIVATE-CREDENTIAL'}})
        serialized = json.dumps(result)
        self.assertNotIn('PRIVATE-', serialized)
        codes = [item['code'] for item in result['checks']]
        self.assertIn('runtime_incompatible', codes)
        self.assertIn('sign_in_required', codes)
        self.assertIn('local_credential_rejected', codes)

    def test_transport_selection_preserves_other_settings_and_does_not_start_process(self):
        self.paired()
        identity, known = self.root / 'selected-identity', self.root / 'selected-known-hosts'
        identity.write_text('fake fixture only', encoding='utf-8')
        known.write_text('fake fixture only', encoding='utf-8')
        result = onboarding.transport_config(self.a, peer_id='computer-b', batch=True,
            settings={'ssh_host': '127.0.0.1', 'username': 'fixture', 'ssh_exe': sys.executable,
                      'identity_file': str(identity), 'known_hosts_file': str(known), 'host_key_alias': 'test-peer'})
        cfg = cli.read(self.a)
        self.assertFalse(result['started'])
        self.assertIn('computer-b', cfg['ssh_transports'])
        self.assertEqual(cfg['peers']['computer-b']['url'], 'http://127.0.0.1:47322')
        self.assertEqual(identity.read_text(), 'fake fixture only')
        self.assertNotIn(str(identity), json.dumps(result))

    def test_preflight_classifies_saved_ssh_failures_without_exposing_transport_paths(self):
        self.test_transport_selection_preserves_other_settings_and_does_not_start_process()
        cfg = cli.read(self.a)
        status_path = Path(cfg['state_dir']) / 'transports' / 'computer-b' / 'status.json'
        status_path.parent.mkdir(parents=True)
        for code, expected in (('ssh_authentication_failed', 'ssh'), ('ssh_host_key_failed', 'ssh'),
                               ('forwarding_port_in_use', 'forwarding'), ('forwarding_denied', 'forwarding')):
            cli.save(status_path, {'state': 'backoff', 'last_error': {'code': code, 'exit_code': 255}})
            result = onboarding.preflight(self.a, runtime_probe=compatible, runner=local_runner,
                probe=lambda _: {'ok': False, 'code': 'peer_unavailable'},
                local_call=lambda *a, **k: {'ok': True}, tcp_probe=lambda *a: True)
            item = next(value for value in result['checks'] if value['code'] == code)
            self.assertEqual(item['category'], expected)
            self.assertNotIn(str(self.root), json.dumps(result))

    def test_new_transport_does_not_require_unrelated_disabled_peer_files(self):
        self.paired()
        cfg = cli.read(self.a)
        cfg['peers']['old-peer'] = {'enabled': False}
        cfg['ssh_transports'] = {'old-peer': {'enabled': False,
            'identity_file': str(self.root / 'removed-old-key'), 'owner_extension': 'preserve'}}
        cli.save(self.a, cfg)
        identity, known = self.root / 'current-key', self.root / 'current-known-hosts'
        identity.write_text('fixture'); known.write_text('fixture')
        onboarding.transport_config(self.a, peer_id='computer-b', batch=True,
            settings={'ssh_host': '127.0.0.1', 'username': 'fixture', 'ssh_exe': sys.executable,
                'identity_file': str(identity), 'known_hosts_file': str(known), 'host_key_alias': 'fixture'})
        self.assertEqual(cli.read(self.a)['ssh_transports']['old-peer'], cfg['ssh_transports']['old-peer'])
        self.assertTrue(cli.read(self.a)['ssh_transports']['computer-b']['enabled'])

    def test_preflight_does_not_use_another_peers_legacy_ssh_route(self):
        self.paired()
        cfg = cli.read(self.a)
        cfg['ssh_transport'] = {'peer_id': 'computer-b', 'enabled': True}
        cfg['peers']['computer-c'] = {**cfg['peers']['computer-b'], 'url': 'http://127.0.0.1:47323'}
        cli.save(self.a, cfg)
        result = onboarding.preflight(self.a, peer_id='computer-c', runtime_probe=compatible,
            runner=local_runner, probe=lambda _: {'ok': False, 'code': 'peer_unavailable'},
            local_call=lambda *a, **k: {'ok': True})
        codes = [value['code'] for value in result['checks']]
        self.assertIn('peer_managed_route', codes)
        self.assertNotIn('ssh_configuration_invalid', codes)

    def test_show_chat_opens_only_a_released_local_codex_uri(self):
        opened = []
        caller = lambda *a, **k: {'ok': True, 'result': {'chat_url': 'codex://threads/test-thread', 'can_open': True}}
        result = onboarding.show_chat(self.a, 'sample-session', open_chat=True, caller=caller, opener=opened.append)
        self.assertTrue(result['open_requested'])
        self.assertEqual(opened, ['codex://threads/test-thread'])
        for value in ({'chat_url': 'https://unexpected.invalid', 'can_open': True},
                      {'chat_url': 'codex://threads/test-thread', 'can_open': False}):
            with self.assertRaises(BridgeError):
                onboarding.show_chat(self.a, 'sample-session', open_chat=True,
                    caller=lambda *a, **k: {'ok': True, 'result': value}, opener=opened.append)
        self.assertEqual(len(opened), 1)

    def test_cli_new_commands_dispatch_with_explicit_options(self):
        with mock.patch.object(onboarding, 'setup', return_value={'ok': True}) as setup, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--config', str(self.a), 'setup', '--peer-id', 'computer-a',
                                      '--codex', sys.executable, '--batch', '--register-mcp']), 0)
        self.assertTrue(setup.call_args.kwargs['register'])
        with mock.patch.object(onboarding, 'show_chat', return_value={'ok': True}) as show, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--config', str(self.a), 'show-chat', '--session-id', 'sample', '--open']), 0)
        self.assertTrue(show.call_args.kwargs['open_chat'])

    def test_safe_status_and_missing_configuration_errors_never_print_paths(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self.assertEqual(cli.main(['--config', str(self.a), 'status']), 1)
        self.assertNotIn(str(self.root), captured.getvalue())
        self.assertIn('local_check_failed', captured.getvalue())
        captured = io.StringIO()
        with mock.patch.object(cli, 'call', return_value={'ok': False, 'error': {'message': 'PRIVATE-PATH'}}) as call, \
                contextlib.redirect_stdout(captured):
            self.assertEqual(cli.main(['--config', str(self.a), 'status']), 1)
        self.assertEqual(call.call_args.args[1], 'bridge_status')
        self.assertNotIn('PRIVATE-PATH', captured.getvalue())

    def prepared_startup_scope(self):
        self.paired()
        onboarding.project_select(self.a, project_id='sample', workspace=self.workspace,
                                  peer_id='computer-b', batch=True)

    def test_guided_startup_defaults_to_no_without_changing_configuration(self):
        self.prepared_startup_scope()
        before = self.a.read_bytes()
        register = mock.Mock()
        result = onboarding.offer_autostart(self.a, {'ok': True}, platform='nt',
            prompt=lambda _: '', read_settings=lambda _: {'components': {}, 'transports': {}}, register=register)
        register.assert_not_called()
        self.assertEqual(result['autostart']['choice'], 'not_enabled')
        self.assertEqual(before, self.a.read_bytes())
        self.assertFalse((self.a.parent / 'state' / 'autostart.json').exists())

    def test_guided_startup_yes_registers_only_and_preserves_existing_startup(self):
        self.prepared_startup_scope()
        register = mock.Mock(return_value={'enabled': True, 'started_now': False})
        with mock.patch.object(cli, 'start_daemon') as start:
            result = onboarding.offer_autostart(self.a, {'ok': True}, platform='nt',
                prompt=lambda _: 'yes', read_settings=lambda _: {'components': {}}, register=register)
        register.assert_called_once_with(self.a, component='daemon')
        start.assert_not_called()
        self.assertFalse(result['autostart']['started_now'])
        register.reset_mock()
        prompt = mock.Mock(side_effect=AssertionError('Existing startup must not be silently replaced'))
        result = onboarding.offer_autostart(self.a, {'ok': True}, platform='nt', prompt=prompt,
            read_settings=lambda _: {'components': {'daemon': {'enabled': True}}, 'transports': {'computer-b': {'enabled': True}}}, register=register)
        self.assertTrue(result['autostart']['existing_registration_preserved'])
        register.assert_not_called()
        prompt.assert_not_called()

    def test_waiting_startup_offered_without_pairing_on_windows_and_linux(self):
        self.initialize()
        for platform in ('nt', 'posix', 'linux'):
            prompt = mock.Mock(return_value='yes')
            register = mock.Mock(return_value={'enabled': True, 'started_now': False})
            result = onboarding.offer_autostart(self.a, {'ok': True}, platform=platform, prompt=prompt,
                read_settings=lambda _: {'components': {}}, register=register)
            register.assert_called_once_with(self.a, component='daemon')
            self.assertIn('waiting Bridge daemon', prompt.call_args.args[0])
            self.assertFalse(result['autostart']['started_now'])
        prompt = mock.Mock()
        register = mock.Mock()
        result = onboarding.offer_autostart(self.a, {'ok': True}, platform='darwin', prompt=prompt, register=register)
        prompt.assert_not_called()
        register.assert_not_called()

    def test_existing_transport_startup_does_not_skip_waiting_daemon_offer(self):
        self.initialize()
        register = mock.Mock(return_value={'enabled': True, 'started_now': False})
        onboarding.offer_autostart(self.a, {'ok': True}, platform='nt', prompt=lambda _: 'yes',
            read_settings=lambda _: {'components': {}, 'transports': {'old-peer': {'enabled': True}}}, register=register)
        register.assert_called_once_with(self.a, component='daemon')

    def test_pair_later_still_offers_waiting_startup(self):
        self.initialize()
        with mock.patch.object(onboarding, 'offer_autostart', return_value={'offered': True}) as offer:
            result = onboarding.continue_setup(self.a, {}, prompt=lambda _: '')
        offer.assert_called_once()
        self.assertTrue(result['offered'])

    @unittest.skipUnless(os.name == 'nt', 'Windows entry point')
    def test_setup_launcher_parses_without_executing_installation(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'setup.ps1'
        code = '$tokens=$null; $errors=$null; [Management.Automation.Language.Parser]::ParseFile(' + cli.ps_literal(script) + ',[ref]$tokens,[ref]$errors) | Out-Null; if ($errors.Count) { throw ($errors | Out-String) }'
        cli.powershell(code)


if __name__ == '__main__':
    unittest.main()
