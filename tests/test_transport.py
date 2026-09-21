import copy
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_bridge.core import BridgeError
from codex_bridge import processes
from codex_bridge.transport import (classify_ssh_error, configure_transport, configured_transports,
    start_transports, stop_transports, supervise_transport, transport_args, transport_directory,
    transport_status, validate_transports)


class FakeSSH:
    serial = 800000
    def __init__(self, args, **kwargs):
        type(self).serial += 1
        self.pid = type(self).serial
        self.returncode = None
        self.stderr = io.BytesIO(b"")
        self.args = args
    def poll(self): return self.returncode
    def terminate(self): self.returncode = 0
    def kill(self): self.returncode = -1
    def wait(self, timeout=None): return self.returncode


class FakeOwner:
    def __init__(self, args, **kwargs):
        self.process = FakeSSH(args, **kwargs)
        self.closed = False
    def close(self):
        self.closed = True
        self.process.kill()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/"state").mkdir()
        for name in ("key", "known_hosts"): (self.root/name).write_text("test fixture")
        self.settings = {"enabled": True, "ssh_exe": sys.executable, "ssh_host": "example.invalid",
            "ssh_port": 22, "username": "example", "host_key_alias": "example",
            "identity_file": str(self.root/"key"), "known_hosts_file": str(self.root/"known_hosts"),
            "local_peer_port": 47501, "remote_bridge_port": 47321, "remote_peer_port": 47511}
        self.cfg = {"version": 1, "peer_id": "here", "listen_port": 47321,
            "state_dir": str(self.root/"state"), "codex_path": sys.executable, "local_token": "x"*40,
            "peers": {"peer-a": {"enabled": True}, "peer-b": {"enabled": True}}, "projects": {},
            "ssh_transports": {"peer-a": copy.deepcopy(self.settings), "peer-b": {**self.settings, "local_peer_port": 47502, "ssh_host": "second.invalid"}}}
        self.path = self.root/"config.json"
        self.write(self.cfg)

    def write(self, cfg):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cfg)); os.replace(temporary, self.path)

    def test_each_peer_has_distinct_ports_and_loopback_only_arguments(self):
        validate_transports(self.cfg)
        one = transport_args(self.cfg, "peer-a"); two = transport_args(self.cfg, "peer-b")
        self.assertIn("127.0.0.1:47501:127.0.0.1:47321", one)
        self.assertIn("127.0.0.1:47502:127.0.0.1:47321", two)
        self.assertIn("StrictHostKeyChecking=yes", one)
        self.assertIn("BatchMode=yes", one)
        self.assertNotEqual(transport_directory(self.cfg, "peer-a"), transport_directory(self.cfg, "peer-b"))
        self.cfg["ssh_transports"]["peer-b"]["local_peer_port"] = 47501
        with self.assertRaises(BridgeError): validate_transports(self.cfg)

    def test_legacy_migration_preserves_scope_and_rejects_ambiguity(self):
        old = copy.deepcopy(self.cfg); old.pop("ssh_transports")
        old["ssh_transport"] = {**self.settings, "peer_id": "peer-a"}
        updated = configure_transport(old, "peer-b", {**self.settings, "local_peer_port": 47502})
        self.assertNotIn("ssh_transport", updated)
        self.assertEqual(set(updated["ssh_transports"]), {"peer-a", "peer-b"})
        self.assertEqual(updated["peers"], old["peers"])
        self.assertIn("ssh_transport", old)
        old["ssh_transport"].pop("peer_id")
        with self.assertRaises(BridgeError): configured_transports(old)

    def test_selected_start_ignores_another_peers_missing_identity(self):
        self.cfg["ssh_transports"]["peer-b"]["identity_file"] = str(self.root/"missing")
        self.write(self.cfg)
        with patch("codex_bridge.transport._launch", return_value=FakeSSH([])) as launch:
            result = start_transports(self.path, "peer-a")
        self.assertTrue(result["transports"][0]["started"])
        self.assertEqual(launch.call_args.args[1], "peer-a")

    def test_all_start_reports_bad_peer_and_starts_other_peer(self):
        for key in ("identity_file", "ssh_exe"):
            self.cfg["ssh_transports"]["peer-a"] = {**self.settings, key: str(self.root/"missing")}
            self.write(self.cfg)
            with patch("codex_bridge.transport._launch", return_value=FakeSSH([])) as launch:
                result = start_transports(self.path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["transports"][0]["state"], "failed")
            self.assertTrue(result["transports"][1]["started"])
            self.assertEqual(launch.call_args.args[1], "peer-b")

    def test_updating_route_does_not_validate_unrelated_identity_file(self):
        self.cfg["ssh_transports"]["peer-b"]["identity_file"] = str(self.root/"missing")
        updated = configure_transport(self.cfg, "peer-a", {**self.settings, "ssh_port": 2222})
        self.assertEqual(updated["ssh_transports"]["peer-a"]["ssh_port"], 2222)
        self.assertEqual(updated["ssh_transports"]["peer-b"], self.cfg["ssh_transports"]["peer-b"])

    def test_supervisors_stop_and_revoke_independently(self):
        failures = []
        owners = {}
        revoked = threading.Event()
        conflict_injected = threading.Event()
        original_read = Path.read_text
        def sharing_conflict(path, *args, **kwargs):
            if path == self.path and threading.current_thread().name == "transport-peer-a" and revoked.is_set() and not conflict_injected.is_set():
                conflict_injected.set()
                raise PermissionError("Simulated Windows atomic replacement sharing conflict")
            return original_read(path, *args, **kwargs)
        def run(peer):
            try: supervise_transport(self.path, peer)
            except Exception as exc: failures.append((peer, type(exc).__name__, str(exc)))
        def spawn(args):
            owner = owners[args[-1]] = FakeOwner(args)
            return owner
        with patch("codex_bridge.transport._spawn_ssh", side_effect=spawn), patch("codex_bridge.core._WINDOWS_IO", True), patch.object(Path, "read_text", sharing_conflict):
            threads = {peer: threading.Thread(target=run, args=(peer,), name="transport-"+peer) for peer in self.cfg["peers"]}
            for thread in threads.values(): thread.start()
            try:
                deadline = time.monotonic()+8
                while time.monotonic()<deadline and not all((transport_directory(self.cfg, peer)/"ssh.pid.json").exists() for peer in threads):
                    time.sleep(.05)
                self.assertFalse(failures)
                self.assertTrue(all(thread.is_alive() for thread in threads.values()), failures)
                self.cfg["peers"]["peer-b"]["enabled"] = False; self.write(self.cfg)
                revoked.set()
                threads["peer-b"].join(5)
                self.assertFalse(threads["peer-b"].is_alive())
                self.assertTrue(owners['second.invalid'].closed)
                self.assertFalse(owners['example.invalid'].closed)
                self.assertTrue(conflict_injected.wait(2), "Expected the healthy supervisor to reopen the updated config")
                self.assertTrue(threads["peer-a"].is_alive(), failures)
                result = stop_transports(self.path, "peer-a", timeout=5)
                threads["peer-a"].join(2)
                self.assertTrue(result["transports"][0]["stopped"])
                self.assertFalse(threads["peer-a"].is_alive())
                self.assertTrue(owners['example.invalid'].closed)
            finally:
                for peer in threads: (transport_directory(self.cfg, peer)/"stop").touch()
                for thread in threads.values(): thread.join(7)
            self.assertFalse(failures)

    def test_status_does_not_expose_private_paths_or_hosts(self):
        result = transport_status(self.path)
        encoded = json.dumps(result)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("example.invalid", encoded)
        self.assertEqual(len(result["transports"]), 2)

    def test_stopping_legacy_waits_and_reports_if_still_alive(self):
        (self.root/"state"/"transport-run.pid.json").write_text(json.dumps(processes.record(os.getpid())))
        result = stop_transports(self.path, timeout=0)
        self.assertFalse(result["legacy_stopped"])
        self.assertTrue((self.root/"state"/"transport.stop").exists())
        with self.assertRaises(BridgeError) as raised: start_transports(self.path)
        self.assertEqual(raised.exception.code, "legacy_transport_running")

    def test_safe_ssh_error_categories(self):
        cases = {"Host key verification failed": "ssh_host_key_failed",
            "Permission denied (publickey)": "ssh_authentication_failed",
            "bind: Address already in use": "forwarding_port_in_use",
            "remote port forwarding failed": "forwarding_denied",
            "connect to host private.example port 22: Connection refused": "ssh_endpoint_unreachable",
            "closed by peer": "ssh_disconnected"}
        for raw, expected in cases.items(): self.assertEqual(classify_ssh_error(raw), expected)

    def test_unpaired_peer_and_unsafe_ssh_names_rejected(self):
        bad = copy.deepcopy(self.cfg); bad["ssh_transports"]["unknown"] = self.settings
        with self.assertRaises(BridgeError): configured_transports(bad)
        self.cfg["ssh_transports"]["peer-a"]["ssh_host"] = "-oProxyCommand=anything"
        with self.assertRaises(BridgeError): transport_args(self.cfg, "peer-a")

    def test_stale_pid_cannot_impersonate_a_transport_or_block_start(self):
        directory = transport_directory(self.cfg, 'peer-a')
        directory.mkdir(parents=True)
        stale = {'pid': os.getpid(), 'process_identity': {'creation_time': 'wrong', 'executable': 'wrong'}}
        for name in ('supervisor.pid.json', 'ssh.pid.json', 'launch.pid.json'):
            (directory / name).write_text(json.dumps(stale))
        (self.root / 'state' / 'transport-run.pid.json').write_text(json.dumps(stale))
        status = transport_status(self.path, 'peer-a')
        self.assertFalse(status['legacy_supervisor_running'])
        self.assertFalse(status['transports'][0]['supervisor_running'])
        self.assertFalse(status['transports'][0]['ssh_running'])
        self.assertTrue(status['transports'][0]['stale_process_record'])
        with patch('codex_bridge.transport._launch', return_value=FakeSSH([])) as launch:
            result = start_transports(self.path, 'peer-a')
        self.assertTrue(result['transports'][0]['started'])
        launch.assert_called_once()

    def test_verified_startup_record_prevents_duplicate_launch(self):
        directory = transport_directory(self.cfg, 'peer-a')
        directory.mkdir(parents=True)
        (directory / 'launch.pid.json').write_text(json.dumps(processes.record(os.getpid())))
        with patch('codex_bridge.transport._launch') as launch:
            result = start_transports(self.path, 'peer-a')
        self.assertTrue(result['transports'][0]['already_running'])
        launch.assert_not_called()

    def test_concurrent_start_launches_one_supervisor(self):
        entered, release = threading.Event(), threading.Event()
        results, failures = [], []
        child = FakeSSH([])
        child.pid = os.getpid()  # Exact live identity in this disposable test record.
        def launch(*args):
            entered.set()
            if not release.wait(10): raise RuntimeError('Test launch was not released')
            return child
        def first():
            try: results.append(start_transports(self.path, 'peer-a'))
            except BaseException as exc: failures.append(exc)
        with patch('codex_bridge.transport._launch', side_effect=launch) as launcher:
            thread = threading.Thread(target=first)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(BridgeError) as failure:
                    start_transports(self.path, 'peer-a')
                self.assertEqual(failure.exception.code, 'already_running')
            finally:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures)
            self.assertTrue(results[0]['transports'][0]['started'])
            repeated = start_transports(self.path, 'peer-a')
            self.assertTrue(repeated['transports'][0]['already_running'])
            launcher.assert_called_once()

    def test_stop_survives_supervisor_retry_and_targeted_resume_preserves_other_peer(self):
        stop_transports(self.path, timeout=0)
        a = transport_directory(self.cfg, 'peer-a')
        b = transport_directory(self.cfg, 'peer-b')
        other_marker = (b / 'stop').read_bytes()
        with patch('codex_bridge.transport._spawn_ssh') as ssh:
            result = supervise_transport(self.path, 'peer-a')
            ssh.assert_not_called()
        self.assertTrue(result['stopped'])
        self.assertTrue((a / 'stop').exists())
        with patch('codex_bridge.transport._launch', return_value=FakeSSH([])):
            start_transports(self.path, 'peer-a')
        self.assertFalse((a / 'stop').exists())
        self.assertEqual((b / 'stop').read_bytes(), other_marker)

    def test_resume_refuses_to_clear_marker_while_its_runner_still_stops(self):
        directory = transport_directory(self.cfg, 'peer-a')
        directory.mkdir(parents=True)
        (directory / 'stop').write_text('preserve-stop')
        with patch('codex_bridge.transport._wait_peer_stopped', return_value=False), \
                patch('codex_bridge.transport._launch') as launch:
            with self.assertRaises(BridgeError) as raised:
                start_transports(self.path, 'peer-a')
        self.assertEqual(raised.exception.code, 'still_stopping')
        self.assertEqual((directory / 'stop').read_text(), 'preserve-stop')
        launch.assert_not_called()

    def test_stop_does_not_race_an_in_progress_start_marker_clear(self):
        from codex_bridge.cli import process_lock
        directory = transport_directory(self.cfg, 'peer-a')
        directory.mkdir(parents=True)
        with process_lock(directory / 'start.lock'):
            with self.assertRaises(BridgeError) as raised:
                stop_transports(self.path, 'peer-a', timeout=0)
        self.assertEqual(raised.exception.code, 'transport_start_in_progress')
        self.assertTrue(raised.exception.retryable)
        self.assertFalse((directory / 'stop').exists())
        repeated = stop_transports(self.path, 'peer-a', timeout=0)
        self.assertTrue(repeated['transports'][0]['stop_requested'])
        self.assertTrue((directory / 'stop').exists())

    def test_owned_ssh_is_closed_when_supervisor_fails_after_spawn(self):
        from codex_bridge import cli
        original = cli.save
        owner = FakeOwner([])
        def failing_save(path, data):
            if path.name == 'ssh.pid.json':
                raise OSError('Injected journal failure')
            return original(path, data)
        with patch('codex_bridge.transport._spawn_ssh', return_value=owner), \
                patch('codex_bridge.cli.save', side_effect=failing_save):
            with self.assertRaises(OSError):
                supervise_transport(self.path, 'peer-a')
        self.assertTrue(owner.closed)
        self.assertIsNotNone(owner.process.poll())

    def test_ssh_child_uses_exact_owned_process_without_shell(self):
        from codex_bridge.transport import _spawn_ssh
        args = transport_args(self.cfg, 'peer-a')
        with patch('codex_bridge.transport.processes.OwnedProcess') as owned:
            result = _spawn_ssh(args)
        self.assertIs(result, owned.return_value)
        self.assertEqual(owned.call_args.args, (args,))
        self.assertNotIn('shell', owned.call_args.kwargs)

    def test_selected_start_uses_only_its_registered_login_task(self):
        with patch('codex_bridge.autostart.enabled', return_value=True) as enabled, \
                patch('codex_bridge.autostart.start', return_value={'start_requested': True}) as start, \
                patch('codex_bridge.transport._launch') as launch:
            result = start_transports(self.path, 'peer-a')
        enabled.assert_called_once_with(self.path, 'transport', peer_id='peer-a')
        start.assert_called_once_with(self.path, 'transport', peer_id='peer-a')
        launch.assert_not_called()
        self.assertTrue(result['transports'][0]['start_requested'])
        self.assertFalse(result['transports'][0]['ssh_connection_verified'])

    @unittest.skipUnless(os.name == 'nt', 'Windows kill-on-close job regression')
    def test_windows_supervisor_crash_releases_its_exact_ssh_child(self):
        script = (
            'import json,sys,time\n'
            'from codex_bridge import processes\n'
            'from codex_bridge.transport import _spawn_ssh\n'
            'owner=_spawn_ssh([sys.executable,"-c","import time; time.sleep(60)"])\n'
            'print(json.dumps(processes.record(owner.process.pid)),flush=True)\n'
            'time.sleep(60)\n')
        owned = processes.OwnedProcess([sys.executable, '-u', '-c', script],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child = owned.process
        ready = threading.Event()
        output = []
        def read_line():
            output.append(child.stdout.readline())
            ready.set()
        reader = threading.Thread(target=read_line, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(15), 'Disposable supervisor did not report its child')
            self.assertTrue(output[0], 'Disposable supervisor failed before reporting its child')
            record = json.loads(output[0])
            self.assertTrue(processes.matches(record))
            child.kill()  # Exact Popen handle, deliberately bypassing Python cleanup.
            child.wait(timeout=10)
            deadline = time.monotonic() + 10
            while processes.matches(record) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(processes.matches(record), 'SSH child survived supervisor job-handle closure')
        finally:
            owned.close()  # Outer test job also contains every disposable descendant.
            reader.join(5)
            child.stdout.close()
            child.stderr.close()
