import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_bridge.core import BridgeError
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
        def run(peer):
            try: supervise_transport(self.path, peer)
            except Exception as exc: failures.append(exc)
        with patch("codex_bridge.transport._spawn_ssh", side_effect=FakeSSH):
            threads = {peer: threading.Thread(target=run, args=(peer,)) for peer in self.cfg["peers"]}
            for thread in threads.values(): thread.start()
            try:
                deadline = time.monotonic()+8
                while time.monotonic()<deadline and not all((transport_directory(self.cfg, peer)/"ssh.pid.json").exists() for peer in threads):
                    time.sleep(.05)
                self.assertFalse(failures)
                self.assertTrue(all(thread.is_alive() for thread in threads.values()))
                self.cfg["peers"]["peer-b"]["enabled"] = False; self.write(self.cfg)
                threads["peer-b"].join(5)
                self.assertFalse(threads["peer-b"].is_alive())
                self.assertTrue(threads["peer-a"].is_alive())
                result = stop_transports(self.path, "peer-a", timeout=5)
                threads["peer-a"].join(2)
                self.assertTrue(result["transports"][0]["stopped"])
                self.assertFalse(threads["peer-a"].is_alive())
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
        (self.root/"state"/"transport-run.pid.json").write_text(json.dumps({"pid": os.getpid()}))
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
