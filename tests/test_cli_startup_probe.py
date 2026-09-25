"""Authenticated startup probes on isolated loopback sockets; no daemon launches."""
import contextlib
import http.server
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from codex_bridge import autostart, cli
from codex_bridge.core import BridgeError


class StartupProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='bridge-startup-probe-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'config.json'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config = {'version': 1, 'peer_id': 'fixture-owner', 'listen_port': 1,
                       'state_dir': str(self.state), 'codex_path': sys.executable,
                       'local_token': 'isolated-test-token-' + 'x' * 40, 'peers': {}, 'projects': {}}
        self.set_port(1)
        self.launch = self.enterContext(mock.patch.object(cli, 'launch', return_value=SimpleNamespace(pid=-1)))
        self.interactive = self.enterContext(mock.patch.object(cli, 'launch_interactive'))
        self.service = self.enterContext(mock.patch.object(autostart, 'start'))
        self.enterContext(mock.patch.object(autostart, 'enabled', return_value=False))

    def set_port(self, port):
        self.config['listen_port'] = port
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

    def assert_no_launch(self):
        self.launch.assert_not_called()
        self.interactive.assert_not_called()
        self.service.assert_not_called()

    @contextlib.contextmanager
    def endpoint(self, mode, delay=0):
        released = threading.Event()
        requests = []
        token = self.config['local_token']
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((request, self.headers.get('Authorization')))
                if mode == 'wedged':
                    released.wait(10)
                    return
                if delay:
                    released.wait(delay)
                accepted = mode != 'wrong_auth' and self.headers.get('Authorization') == 'Bearer ' + token
                status = 200 if accepted else 401
                if mode == 'malformed':
                    body = b'not a bridge response'
                elif mode == 'unauthorized_envelope':
                    body = json.dumps({'ok': False, 'error': {'code': 'unauthorized', 'message': 'Wrong owner'}}).encode()
                else:
                    body = json.dumps({'ok': True, 'result': {'sessions': []}} if accepted else
                                      {'ok': False, 'error': {'code': 'unauthorized', 'message': 'Wrong owner'}}).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.set_port(server.server_address[1])
        try:
            yield requests
        finally:
            released.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_platform_budgets_allow_windows_refusal_without_changing_other_platforms(self):
        for name, expected in (('nt', 5), ('posix', 2)):
            with self.subTest(platform=name), mock.patch.object(cli.os, 'name', name):
                self.assertEqual(cli._startup_probe_timeout(), expected)

    def test_real_closed_loopback_allows_one_launch_after_explicit_refusal(self):
        # Reserve without listening: no other process can race to claim this
        # isolated port, and Windows may take about 2.05s to return WSAECONNREFUSED.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                reserved.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reserved.bind(('127.0.0.1', 0))
            self.set_port(reserved.getsockname()[1])
            result = cli._start_daemon(self.path, resume=False)
        self.assertIsInstance(result, tuple)
        self.launch.assert_called_once_with(self.path, 'serve')
        self.interactive.assert_not_called()
        self.service.assert_not_called()

    def test_healthy_authenticated_listener_is_reused_without_launch(self):
        with self.endpoint('healthy') as requests:
            result = cli._start_daemon(self.path, resume=False)
        self.assertTrue(result['already_running'])
        self.assertEqual(requests, [({'method': 'session_list', 'params': {}},
                                     'Bearer ' + self.config['local_token'])])
        self.assert_no_launch()

    @unittest.skipUnless(os.name == 'nt', 'Windows startup probe has a five-second budget')
    def test_windows_slow_authenticated_listener_is_reused(self):
        with self.endpoint('healthy', delay=2.3):
            result = cli._start_daemon(self.path, resume=False)
        self.assertTrue(result['already_running'])
        self.assert_no_launch()

    def test_real_wedged_listener_fails_closed_without_launch(self):
        with self.endpoint('wedged'):
            with self.assertRaises(BridgeError) as caught:
                cli._start_daemon(self.path, resume=False)
        self.assertEqual(caught.exception.code, 'local_endpoint_unverified')
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        self.assert_no_launch()

    def test_real_wrong_auth_or_malformed_listener_never_launches(self):
        for mode, error in (('wrong_auth', BridgeError), ('unauthorized_envelope', BridgeError),
                            ('malformed', json.JSONDecodeError)):
            with self.subTest(mode=mode), self.endpoint(mode):
                with self.assertRaises(error):
                    cli._start_daemon(self.path, resume=False)
            self.assert_no_launch()

    def test_timeout_never_becomes_absence_even_with_windows_budget(self):
        for error in (TimeoutError('fixture'), urllib.error.URLError(TimeoutError('fixture'))):
            with self.subTest(type=type(error).__name__), \
                 mock.patch.object(cli, '_startup_probe_timeout', return_value=5), \
                 mock.patch.object(cli, 'call', side_effect=error) as probe:
                with self.assertRaises(BridgeError) as caught:
                    cli._start_daemon(self.path, resume=False)
                self.assertEqual(caught.exception.code, 'local_endpoint_unverified')
                self.assertFalse(cli.connection_refused(error))
                self.assertEqual(probe.call_args.kwargs['timeout'], 5)
                self.assertEqual(probe.call_count, 1)
            self.assert_no_launch()

    def test_deliberate_stop_is_preserved_before_any_probe(self):
        marker = autostart.stop_path(self.path, 'daemon')
        marker.write_bytes(b'owner-stop-fixture')
        with mock.patch.object(cli, 'call') as probe:
            with self.assertRaises(BridgeError) as caught:
                cli._start_daemon(self.path, resume=False)
        self.assertEqual(caught.exception.code, 'daemon_stopped')
        self.assertEqual(marker.read_bytes(), b'owner-stop-fixture')
        probe.assert_not_called()
        self.assert_no_launch()

    def test_readiness_caps_probe_and_sleep_to_total_thirty_second_budget(self):
        clock = SimpleNamespace(now=0.0)
        timeouts, sleeps = [], []
        def probe(path, method, params, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30 - clock.now)
            timeouts.append(timeout)
            clock.now += timeout
            raise TimeoutError('fixture not ready')
        def sleep(seconds):
            self.assertLessEqual(seconds, 30 - clock.now)
            sleeps.append(seconds)
            clock.now += seconds
        with mock.patch.object(cli, '_startup_probe_timeout', return_value=5), \
             mock.patch.object(cli.time, 'monotonic', side_effect=lambda: clock.now), \
             mock.patch.object(cli.time, 'sleep', side_effect=sleep), \
             mock.patch.object(cli, 'call', side_effect=probe):
            with self.assertRaises(BridgeError) as caught:
                cli._wait_daemon_ready(self.path, None, {}, 'background')
        self.assertEqual(caught.exception.code, 'startup_timeout')
        self.assertEqual(clock.now, 30)
        self.assertLess(timeouts[-1], 5)
        self.assertEqual(sum(timeouts) + sum(sleeps), 30)
        self.assert_no_launch()

    def test_exited_child_probe_uses_remaining_budget_and_authenticates_winner(self):
        clock = SimpleNamespace(now=0.0)
        def poll():
            clock.now = 29
            return 0
        process = SimpleNamespace(poll=poll)
        with mock.patch.object(cli, '_startup_probe_timeout', return_value=5), \
             mock.patch.object(cli.time, 'monotonic', side_effect=lambda: clock.now), \
             mock.patch.object(cli, 'call', return_value={'ok': True, 'result': {'sessions': []}}) as probe:
            result = cli._wait_daemon_ready(self.path, process, {}, 'background')
        self.assertTrue(result['already_running'])
        self.assertEqual(probe.call_args.kwargs['timeout'], 1)
        self.assert_no_launch()


if __name__ == '__main__':
    unittest.main()
