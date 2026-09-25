"""OpenSSH argument parsing only; never connect or read real credentials."""
import copy
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from codex_bridge import cli
from codex_bridge.core import BridgeError
from codex_bridge.transport import quote_ssh_option, transport_args


SSH = shutil.which("ssh")


class SSHOptionTests(unittest.TestCase):
    def test_quotes_spaces_and_preserves_windows_backslashes(self):
        self.assertEqual(quote_ssh_option("/fixture none"), '"/fixture none"')
        self.assertEqual(quote_ssh_option(r"C:\Example Owner\known_hosts"),
                         r'"C:\\Example Owner\\known_hosts"')
        self.assertEqual(quote_ssh_option('/fixture a"b/known_hosts'),
                         r'"/fixture a\"b/known_hosts"')

    def test_controls_and_invalid_values_fail_without_echoing_path(self):
        for value in (None, [], "", "x" * 32769, "PRIVATE\nPATH", "PRIVATE\rPATH",
                      "PRIVATE\tPATH", "PRIVATE\x00PATH", "PRIVATE\x1bPATH", "PRIVATE\x7fPATH"):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(BridgeError) as caught:
                quote_ssh_option(value)
            self.assertEqual(caught.exception.code, "configuration")
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_named_and_legacy_builders_quote_selected_file_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            key = root / "identity with spaces"
            known = root / "known hosts none"
            key.write_text("fixture, not a private key")
            known.write_text("fixture, not a host key")
            route = {"ssh_exe": sys.executable, "ssh_host": "example.invalid",
                     "username": "fixture", "host_key_alias": "fixture-alias",
                     "identity_file": str(key), "known_hosts_file": str(known),
                     "ssh_port": 22, "local_peer_port": 47501,
                     "remote_bridge_port": 47321, "remote_peer_port": 47511}
            config = {"listen_port": 47321, "peers": {"other": {"enabled": True}},
                      "ssh_transports": {"other": copy.deepcopy(route)}}
            for args in (transport_args(config, "other"),
                         cli.transport_args({"listen_port": 47321, "ssh_transport": route})):
                self.assertIn("UserKnownHostsFile=" + quote_ssh_option(str(known)), args)
                self.assertEqual(args[args.index("-i") + 1], str(key))
                self.assertIn("StrictHostKeyChecking=yes", args)
                self.assertIn("BatchMode=yes", args)
                self.assertEqual(args[args.index("-L") + 1], "127.0.0.1:47501:127.0.0.1:47321")
                self.assertEqual(args[args.index("-R") + 1], "127.0.0.1:47511:127.0.0.1:47321")

    @unittest.skipUnless(SSH, "OpenSSH parsing check requires the local ssh executable")
    def test_real_openssh_parser_keeps_one_literal_spaced_or_quoted_filename(self):
        # "none" as a separate list element is rejected by OpenSSH. As part of
        # this quoted filename it is literal, so this detects accidental split.
        samples = ["/fixture none", "/fixture owner/known_hosts",
                   r"C:\Users\Example Owner\known_hosts",
                   r"\\fixture-server\share name\known_hosts",
                   '/fixture a"b/known_hosts', "/fixture a'b/known_hosts",
                   r"/fixture a\b/known_hosts", "/fixture # comment/known_hosts"]
        for value in samples:
            with self.subTest(filename=value):
                result = subprocess.run([SSH, "-T", "-G", "-F", "none", "-o",
                    "UserKnownHostsFile=" + quote_ssh_option(value), "example.invalid"],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = [line for line in result.stdout.splitlines()
                        if line.startswith("userknownhostsfile ")]
                self.assertEqual(rows, ["userknownhostsfile " + value])


if __name__ == "__main__":
    unittest.main()
