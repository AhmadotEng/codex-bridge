"""Owner-local entrypoints never mistake failed authentication for absent daemon.

Uses disposable loopback listeners and mocked startup; no SSH, user sessions,
scheduled tasks, or installed configuration are modified.
"""
import asyncio
import contextlib
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
import urllib.error
import uuid

from codex_bridge import autostart, cli, mcp
from codex_bridge.core import Bridge, BridgeError, Store


def refused():
    return urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'fixture refused'))


class ConnectionEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bridge-entrypoint-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.path = self.root / 'config.json'
        self.request_id = str(uuid.uuid4())
        self.params = {'peer_id': 'peer', 'request_id': self.request_id}
        self.cfg = {'version': 1, 'peer_id': 'local', 'listen_port': 47321,
                    'state_dir': str(self.state), 'codex_path': sys.executable,
                    'local_token': 'fixture-local-token-' + 'x' * 32,
                    'peers': {'peer': {'url': 'http://127.0.0.1:49999', 'enabled': True,
                                      'outgoing_token': 'fixture-remote-token-' + 'y' * 32}}, 'projects': {}}
        self.write_config()
        self.client = mcp.LocalBridgeClient(self.path)
        self.envelope = {'ok': True, 'result': {'available': True}, 'request_id': self.request_id}

    def write_config(self):
        self.path.write_text(json.dumps(self.cfg), encoding='utf-8')

    def response(self):
        return io.BytesIO(json.dumps(self.envelope).encode())

    @contextlib.contextmanager
    def http(self, status=200, payload=None):
        requests = []
        content = self.envelope if payload is None else payload
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('Authorization'), body))
                raw = json.dumps(content).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.cfg['listen_port'] = server.server_port
        self.write_config()
        try:
            yield requests
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_refused_ensure_bootstraps_only_local_daemon_and_preserves_uuid(self):
        with mock.patch.object(self.client.opener, 'open', side_effect=[refused(), self.response()]) as open_request, mock.patch.object(cli, 'start_daemon') as start:
            result = self.client.call('connection_ensure', self.params)
        start.assert_called_once_with(self.path, resume=False)
        self.assertEqual(result, self.envelope)
        self.assertEqual(open_request.call_count, 2)
        for request in (item.args[0] for item in open_request.call_args_list):
            self.assertEqual(request.full_url, 'http://127.0.0.1:47321/rpc')
            self.assertEqual(json.loads(request.data), {'method': 'connection_ensure', 'params': self.params})
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.cfg['local_token'])
            self.assertNotIn(self.cfg['peers']['peer']['outgoing_token'], str(request.headers))

    def test_only_explicit_retry_authorizes_local_resume(self):
        with mock.patch.object(self.client.opener, 'open', side_effect=[refused(), self.response()]), mock.patch.object(cli, 'start_daemon') as start:
            result = self.client.call('connection_retry', self.params)
        start.assert_called_once_with(self.path, resume=True)
        self.assertEqual(result['request_id'], self.request_id)

    def test_repeated_refusal_bootstraps_at_most_once_per_call(self):
        with mock.patch.object(self.client.opener, 'open', side_effect=refused()) as open_request, mock.patch.object(cli, 'start_daemon') as start:
            result = self.client.call('connection_ensure', self.params)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(open_request.call_count, 2)
        self.assertEqual(result['error']['code'], 'bridge_unavailable')
        self.assertEqual(result['request_id'], self.request_id)

    def test_readonly_calls_and_disconnect_never_bootstrap_on_refusal(self):
        for method in ('local_status', 'connection_status', 'bridge_status', 'peer_status',
                       'session_list', 'task_status', 'task_wait', 'connection_disconnect'):
            with self.subTest(method=method), mock.patch.object(self.client.opener, 'open', side_effect=refused()), mock.patch.object(cli, 'start_daemon') as start:
                result = self.client.call(method, self.params)
            self.assertEqual(result['error']['code'], 'bridge_unavailable')
            start.assert_not_called()

    def test_socket_timeout_reset_or_unreachable_never_bootstraps(self):
        errors = [TimeoutError('fixture timeout'), urllib.error.URLError(TimeoutError()),
                  ConnectionResetError(errno.ECONNRESET, 'fixture reset'),
                  urllib.error.URLError(OSError(errno.ENETUNREACH, 'fixture unreachable'))]
        for error in errors:
            for method in ('connection_ensure', 'connection_retry'):
                with self.subTest(error=type(error).__name__, method=method), mock.patch.object(self.client.opener, 'open', side_effect=error), mock.patch.object(cli, 'start_daemon') as start:
                    result = self.client.call(method, self.params)
                self.assertEqual(result['error']['code'], 'bridge_unavailable')
                start.assert_not_called()

    def test_real_local_auth_rejection_never_bootstraps_or_exposes_token(self):
        for status in (401, 403):
            with self.subTest(status=status), self.http(status), mock.patch.object(cli, 'start_daemon') as start:
                result = self.client.call('connection_ensure', self.params)
            self.assertEqual(result['error']['code'], 'bridge_access_denied')
            self.assertEqual(result['request_id'], self.request_id)
            self.assertNotIn(self.cfg['local_token'], json.dumps(result))
            start.assert_not_called()

    def test_real_local_success_preserves_exact_mutation_and_no_bootstrap(self):
        with self.http() as requests, mock.patch.object(cli, 'start_daemon') as start:
            result = self.client.call('connection_ensure', self.params)
        self.assertTrue(result['ok'])
        self.assertEqual(requests[0][2], {'method': 'connection_ensure', 'params': self.params})
        start.assert_not_called()

    def test_malformed_response_does_not_trigger_daemon_start(self):
        with self.http(payload={'unexpected': True}), mock.patch.object(cli, 'start_daemon') as start:
            result = self.client.call('connection_retry', self.params)
        self.assertEqual(result['error']['code'], 'invalid_bridge_response')
        start.assert_not_called()

    def test_missing_configuration_or_invalid_listen_port_never_bootstraps(self):
        self.path.unlink()
        with mock.patch.object(cli, 'start_daemon') as start:
            self.assertEqual(self.client.call('connection_ensure', self.params)['error']['code'], 'configuration_error')
        start.assert_not_called()
        self.cfg['listen_port'] = True
        self.write_config()
        with mock.patch.object(cli, 'start_daemon') as start:
            self.assertEqual(self.client.call('connection_retry', self.params)['error']['code'], 'configuration_error')
        start.assert_not_called()

    def test_connection_uuid_validated_before_network_or_bootstrap(self):
        for request_id in ('not-a-uuid', self.request_id.upper(), self.request_id.replace('-', ''), None, 7):
            for method in ('connection_ensure', 'connection_retry', 'connection_disconnect'):
                with self.subTest(request_id=request_id, method=method), mock.patch.object(self.client.opener, 'open') as request, mock.patch.object(cli, 'start_daemon') as start:
                    result = self.client.call(method, {'peer_id': 'peer', 'request_id': request_id})
                self.assertEqual(result['error']['code'], 'invalid_argument')
                request.assert_not_called(); start.assert_not_called()

    def test_deliberate_daemon_stop_survives_connection_ensure(self):
        marker = autostart.stop_path(self.path, 'daemon')
        marker.write_text(json.dumps({'login_identity': 'fixture-owner'}))
        with mock.patch.object(self.client.opener, 'open', side_effect=refused()), mock.patch.object(cli, 'wait_for_exit', return_value=True), mock.patch.object(cli, 'launch') as launch:
            result = self.client.call('connection_ensure', self.params)
        self.assertEqual(result['error']['code'], 'daemon_stopped')
        self.assertEqual(result['request_id'], self.request_id)
        self.assertTrue(marker.exists())
        launch.assert_not_called()

    def test_explicit_retry_can_clear_daemon_stop_and_preserves_request(self):
        marker = autostart.stop_path(self.path, 'daemon')
        marker.write_text(json.dumps({'login_identity': 'fixture-owner'}))
        with mock.patch.object(self.client.opener, 'open', side_effect=[refused(), self.response()]), mock.patch.object(cli, 'wait_for_exit', return_value=True), mock.patch.object(cli, 'call', return_value={'ok': True, 'result': {'sessions': []}}), mock.patch.object(cli, 'launch') as launch:
            result = self.client.call('connection_retry', self.params)
        self.assertEqual(result['request_id'], self.request_id)
        self.assertTrue(result['ok'])
        self.assertFalse(marker.exists())
        launch.assert_not_called()

    def test_cli_maps_commands_and_preserves_caller_uuid(self):
        for command, flags, method in [('connect', [], 'connection_ensure'),
                                        ('connect', ['--retry'], 'connection_retry'),
                                        ('disconnect', [], 'connection_disconnect')]:
            with self.subTest(command=command, flags=flags), mock.patch.object(mcp.LocalBridgeClient, 'call', return_value=self.envelope) as call, contextlib.redirect_stdout(io.StringIO()) as out:
                result = cli.main(['--config', str(self.path), command, '--peer', 'peer', '--request-id', self.request_id, *flags])
            self.assertEqual(result, 0)
            call.assert_called_once_with(method, self.params)
            self.assertEqual(json.loads(out.getvalue())['request_id'], self.request_id)

    def test_cli_connection_status_stays_readonly(self):
        with mock.patch.object(mcp.LocalBridgeClient, 'call', return_value=self.envelope) as call, mock.patch.object(cli, 'start_daemon') as start, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--config', str(self.path), 'connection-status', '--peer', 'peer']), 0)
        call.assert_called_once_with('connection_status', {'peer_id': 'peer'})
        start.assert_not_called()

    def test_mcp_notifications_cannot_invoke_bootstrap_or_retry(self):
        client = mock.Mock()
        server = mcp.MCPServer(client)
        for name in ('connection_ensure', 'connection_retry'):
            result = server.handle({'jsonrpc': '2.0', 'method': 'tools/call',
                'params': {'name': name, 'arguments': self.params}})
            self.assertIsNone(result)
        client.call.assert_not_called()

    def test_remote_credential_cannot_invoke_local_connection_admin(self):
        node = types.SimpleNamespace(peer=mock.Mock(), connections=mock.Mock())
        for method in ('local_status', 'connection_status', 'connection_ensure',
                       'connection_retry', 'connection_disconnect', 'shutdown'):
            with self.subTest(method=method), self.assertRaises(BridgeError) as caught:
                asyncio.run(Bridge.dispatch(node, method, self.params, actor='peer'))
            self.assertEqual(caught.exception.code, 'scope_denied')
        node.connections.assert_not_called()

    def test_daemon_second_probe_timeout_or_auth_failure_never_launches_duplicate(self):
        for error in (TimeoutError('fixture timeout'),
                      urllib.error.HTTPError('http://127.0.0.1', 401, 'fixture denied', {}, None)):
            with self.subTest(error=type(error).__name__), mock.patch.object(cli, 'call', side_effect=error), mock.patch.object(autostart, 'enabled', return_value=False), mock.patch.object(cli, 'launch') as launch:
                with self.assertRaises((BridgeError, OSError)):
                    cli._start_daemon(self.path, resume=False)
                launch.assert_not_called()


class ConnectionIntegrationReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse the real two-manager, two-store fixture; only the SSH carrier is
        # simulated. The entrypoint suite's HTTP checks above use real sockets.
        from test_connections import Pair
        self.temporary = tempfile.TemporaryDirectory(prefix='bridge-adversarial-')
        self.root = Path(self.temporary.name)
        self.pair = Pair(self.root)

    async def asyncTearDown(self):
        await asyncio.sleep(.55)
        await self.pair.close()
        self.temporary.cleanup()

    async def test_stale_remote_disconnect_does_not_pause_new_generation(self):
        await self.pair.a.ensure('bravo', str(uuid.uuid4()))
        before = self.pair.b._state('alpha')
        stale = {'generation': before['generation'] - 1, 'connection_id': str(uuid.uuid4())}
        with self.assertRaises(BridgeError) as caught:
            await self.pair.b._receive_disconnect('alpha', stale)
        self.assertEqual(caught.exception.code, 'stale_generation')
        self.assertEqual(self.pair.b._state('alpha'), before)
        self.assertTrue(self.pair.b.status('alpha')['available'])

    async def test_idle_drain_does_not_close_newly_queued_local_work(self):
        await self.pair.a.ensure('bravo', str(uuid.uuid4()))
        async def new_work_arrives(node, method, params, result):
            if node.node_id == 'alpha' and method == 'peer.connection_drain':
                node.busy = True
            return result
        self.pair.after_rpc = new_work_arrives
        await self.pair.a._idle_close('bravo', self.pair.a._state('bravo')['decision'])
        self.assertTrue(self.pair.nodes['alpha'].busy)
        self.assertNotEqual(self.pair.a._state('bravo')['decision']['state'], 'closed')
        self.assertNotEqual(self.pair.b._state('alpha')['decision']['state'], 'closed')
        self.assertIn('alpha', self.pair.routes)

    async def test_remote_close_cannot_bypass_busy_guard_without_drain(self):
        await self.pair.a.ensure('bravo', str(uuid.uuid4()))
        self.pair.nodes['bravo'].busy = True
        decision = self.pair.b._state('alpha')['decision']
        with self.assertRaises(BridgeError):
            await self.pair.b._receive_close('alpha', {'generation': decision['generation'],
                                                       'connection_id': decision['connection_id']})
        self.assertTrue(self.pair.b.status('alpha')['available'])

    async def test_task_delivery_retains_uuid_while_explicit_connection_stop_is_active(self):
        store = Store(self.root / 'delivery.sqlite')
        try:
            request_id = str(uuid.uuid4())
            task = {'request_id': request_id, 'session_id': 'session', 'peer_id': 'bravo',
                    'prompt': 'fixture work', 'status': 'pending_delivery'}
            store.put('outgoing', request_id, task)
            node = types.SimpleNamespace(store=store, delivery_lock=asyncio.Lock(), node_id='alpha',
                session=lambda *args, **kwargs: {'coordinator_id': 'bravo'},
                remote=mock.AsyncMock(side_effect=BridgeError('connection_stopped', 'Owner deliberately stopped this connection')))
            await Bridge.deliver(node, task)
            self.assertEqual(store.get('outgoing', request_id)['status'], 'pending_delivery')
            node.remote = mock.AsyncMock(return_value={'request_id': request_id, 'status': 'queued'})
            await Bridge.deliver(node, store.get('outgoing', request_id))
            saved = store.get('outgoing', request_id)
            self.assertEqual(saved['request_id'], request_id)
            self.assertEqual(saved['status'], 'queued')
            node.remote.assert_awaited_once()
        finally:
            store.db.close()


if __name__ == '__main__':
    unittest.main()
