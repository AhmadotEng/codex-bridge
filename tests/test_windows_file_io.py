"""Windows sharing violations must not stop unrelated peers or reuse old scope."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_bridge import cli
from codex_bridge.core import Bridge, windows_file_retry
from codex_bridge.mcp import LocalBridgeClient


class WindowsFileIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve()/"config.json"

    def test_one_sharing_conflict_reopens_file_for_current_value(self):
        operation = Mock(side_effect=[PermissionError("sharing conflict"), "new config"])
        with patch("codex_bridge.core._WINDOWS_IO", True):
            self.assertEqual(windows_file_retry(operation), "new config")
        self.assertEqual(operation.call_count, 2)

    def test_permanent_denial_stops_at_bounded_deadline(self):
        operation = Mock(side_effect=PermissionError("permanent denial"))
        with patch("codex_bridge.core._WINDOWS_IO", True), patch("codex_bridge.core.time.monotonic", side_effect=[0, .6, .6, 1.2]), patch("codex_bridge.core.time.sleep") as sleep:
            with self.assertRaises(PermissionError): windows_file_retry(operation)
        self.assertEqual(operation.call_count, 2)
        sleep.assert_called_once()

    def test_non_windows_denial_and_bad_json_are_not_retried(self):
        operation = Mock(side_effect=PermissionError("denied"))
        with patch("codex_bridge.core._WINDOWS_IO", False):
            with self.assertRaises(PermissionError): windows_file_retry(operation)
        self.assertEqual(operation.call_count, 1)
        with patch.object(Path, "read_text", return_value="{malformed") as read, patch("codex_bridge.core._WINDOWS_IO", True):
            with self.assertRaises(json.JSONDecodeError): cli.read(self.path)
        self.assertEqual(read.call_count, 1)

    def test_core_cli_and_mcp_config_reads_use_current_file_after_conflict(self):
        config = {"version": 1, "listen_port": False, "local_token": "fixture"}
        value = json.dumps(config)
        bridge = Bridge.__new__(Bridge); bridge.config_path = self.path
        for reader in (lambda: cli.read(self.path), bridge.config):
            with patch.object(Path, "read_text", side_effect=[PermissionError("sharing"), value]) as read, patch("codex_bridge.core._WINDOWS_IO", True):
                self.assertEqual(reader(), config)
            self.assertEqual(read.call_count, 2)
        with patch.object(Path, "read_text", side_effect=[PermissionError("sharing"), value]) as read, patch("codex_bridge.core._WINDOWS_IO", True):
            result = LocalBridgeClient(self.path).call("peer_status", {})
        self.assertIn("listen_port", result["error"]["message"])
        self.assertEqual(read.call_count, 2)

    def test_atomic_save_retries_replace_without_leaving_temporary_data(self):
        self.path.write_text('{"old":true}')
        original_replace = os.replace
        attempts = []
        def replace(source, destination):
            attempts.append((source, destination))
            if len(attempts) == 1: raise PermissionError("sharing conflict")
            return original_replace(source, destination)
        with patch("codex_bridge.cli.os.replace", side_effect=replace), patch("codex_bridge.core._WINDOWS_IO", True):
            cli.save(self.path, {"revoked": True})
        self.assertEqual(json.loads(self.path.read_text()), {"revoked": True})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
