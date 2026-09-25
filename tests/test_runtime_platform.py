"""Native runtime launch diagnostics; no account data or model commands."""
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_bridge import compatibility
from tests.test_compatibility import write_bundle


class RuntimePlatformTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bridge-platform-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.executable = self.directory / "private-owner-codex"

    def assert_safe_failure(self, result, code):
        self.assertFalse(result["ready"])
        self.assertFalse(result["schema_ready"])
        self.assertEqual(result["errors"][0]["code"], code)
        self.assertEqual(result["native_tool_execution"], "not_checked")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        self.assertNotIn(str(self.directory), json.dumps(result))
        self.assertNotIn(self.executable.name, json.dumps(result))

    def test_posix_execute_permission_errors_have_specific_sanitized_diagnostic(self):
        for value in (errno.EACCES, errno.EPERM):
            with self.subTest(errno=value), \
                    mock.patch.object(compatibility.sys, "platform", "linux"), \
                    mock.patch.object(compatibility.subprocess, "run", side_effect=
                        OSError(value, "PRIVATE_SENTINEL", str(self.executable))) as run:
                result = compatibility.inspect_runtime(self.executable)
            self.assert_safe_failure(result, "runtime_not_executable")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["runtime_bundle"]["status"], "not_applicable")

    def test_posix_wrong_format_diagnostic_does_not_claim_an_exact_cpu(self):
        with mock.patch.object(compatibility.sys, "platform", "linux"), \
                mock.patch.object(compatibility.subprocess, "run", side_effect=
                    OSError(errno.ENOEXEC, "PRIVATE_SENTINEL", str(self.executable))):
            result = compatibility.inspect_runtime(self.executable)
        self.assert_safe_failure(result, "runtime_exec_format")
        self.assertIn("CPU architecture", result["errors"][0]["message"])
        self.assertIn("valid executable launcher", result["errors"][0]["message"])

    def test_missing_file_retains_existing_generic_failure(self):
        with mock.patch.object(compatibility.sys, "platform", "linux"), \
                mock.patch.object(compatibility.subprocess, "run", side_effect=
                    FileNotFoundError(errno.ENOENT, "PRIVATE_SENTINEL", str(self.executable))):
            result = compatibility.inspect_runtime(self.executable)
        self.assert_safe_failure(result, "runtime_probe_failed")

    def test_windows_does_not_inherit_posix_errno_interpretation(self):
        with mock.patch.object(compatibility.sys, "platform", "win32"), \
                mock.patch.object(compatibility.subprocess, "run", side_effect=
                    OSError(errno.ENOEXEC, "PRIVATE_SENTINEL", str(self.executable))):
            result = compatibility.inspect_runtime(self.executable)
        self.assert_safe_failure(result, "runtime_probe_failed")

    def test_schema_read_permission_failure_is_not_labeled_execute_permission(self):
        replies = [subprocess.CompletedProcess([], 0, b"codex-cli 9.9.9\n", b""),
                   subprocess.CompletedProcess([], 0, b"", b"")]
        with mock.patch.object(compatibility.sys, "platform", "linux"), \
                mock.patch.object(compatibility.subprocess, "run", side_effect=replies), \
                mock.patch.object(compatibility, "validate_schema_bundle", side_effect=
                    PermissionError(errno.EACCES, "PRIVATE_SENTINEL", str(self.executable))):
            result = compatibility.inspect_runtime(self.executable)
        self.assert_safe_failure(result, "runtime_probe_failed")

    def test_unknown_linux_version_keeps_schema_fallback_without_helper_assumptions(self):
        calls = []

        def run(arguments, **options):
            calls.append(arguments)
            if "--version" in arguments:
                return subprocess.CompletedProcess(arguments, 0, b"codex-cli 9.9.9\n", b"")
            write_bundle(Path(arguments[arguments.index("--out") + 1]))
            return subprocess.CompletedProcess(arguments, 0, b"", b"")

        with mock.patch.object(compatibility.sys, "platform", "linux"), \
                mock.patch.object(compatibility.subprocess, "run", side_effect=run):
            result = compatibility.inspect_runtime(self.executable)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["runtime_bundle"]["status"], "not_applicable")
        self.assertEqual(result["runtime_bundle"]["required_files"], [])
        self.assertEqual(result["native_tool_execution"], "not_checked")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], [str(self.executable), "--version"])
        self.assertEqual(calls[1][1:3], ["app-server", "generate-json-schema"])

    @unittest.skipUnless(os.name == "posix", "Native POSIX execute-permission failure")
    def test_actual_file_without_execute_permission_is_diagnosed(self):
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o600)
        self.assert_safe_failure(compatibility.inspect_runtime(self.executable),
                                 "runtime_not_executable")

    @unittest.skipUnless(os.name == "posix", "Native POSIX executable-format failure")
    def test_actual_non_native_format_is_diagnosed_without_running_it(self):
        self.executable.write_bytes(b"MZ\x00\x00invalid native executable fixture\n")
        self.executable.chmod(0o700)
        self.assert_safe_failure(compatibility.inspect_runtime(self.executable),
                                 "runtime_exec_format")


if __name__ == "__main__":
    unittest.main()
