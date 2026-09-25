"""Portable setup checks use temporary installations and never local credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        "bridge_test_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_script("setup")
installer = load_script("install")


class PortableSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bridge-setup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.destination = self.root / "installed with spaces" / "codex-bridge"
        self.config = self.root / "private" / "config.json"
        self.base = ["--directory", str(self.destination),
                     "--config", str(self.config)]

    def test_default_guided_registration_uses_current_python_and_inherits_stdio(self):
        calls = []

        def runner(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0)

        with mock.patch.dict(os.environ, {"PYTHONPATH": "unrelated-owner-path"}):
            result = setup.main(self.base + ["--codex", "/local codex/bin/codex",
                                             "--peer-id", "computer-a"],
                                source=ROOT, runner=runner)
            self.assertEqual(os.environ["PYTHONPATH"], "unrelated-owner-path")
        self.assertEqual(result, 0)
        arguments, options = calls[0]
        self.assertEqual(arguments[:5], [sys.executable, "-I", str(self.destination/'scripts'/'run_bridge.py'),
                                        "--config", str(self.config)])
        self.assertEqual(arguments[5:], ["setup", "--codex", "/local codex/bin/codex",
                                         "--peer-id", "computer-a", "--guided",
                                         "--register-mcp"])
        self.assertNotIn("cwd", options)  # retain owner-relative argument semantics
        self.assertEqual(options["env"]["PYTHONPATH"], str(self.destination))
        self.assertFalse(options["check"])
        for captured in ("stdin", "stdout", "stderr", "input", "capture_output"):
            self.assertNotIn(captured, options)
        launcher = json.loads((self.destination / ".mcp.json").read_text())
        self.assertEqual(launcher["mcpServers"]["codex_bridge"]["command"], sys.executable)
        self.assertFalse(self.config.exists())

    def test_batch_skip_registration_preserves_private_files_and_returns_cli_failure(self):
        self.config.parent.mkdir(parents=True)
        original = b'{"owner":"private sentinel","model":"saved-model"}\n'
        self.config.write_bytes(original)
        installer.install(ROOT, self.destination, self.config)
        private = self.destination / "state" / "owner.txt"
        private.parent.mkdir(parents=True)
        private.write_text("retained state", encoding="utf-8")
        calls = []

        def runner(arguments, **options):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 7)

        for _ in range(2):
            result = setup.main(self.base + ["--batch", "--skip-registration"],
                                source=ROOT, runner=runner)
            self.assertEqual(result, 7)
            self.assertEqual(self.config.read_bytes(), original)
            self.assertEqual(private.read_text(), "retained state")
        self.assertEqual(calls[0][-2:], ["setup", "--batch"])
        self.assertNotIn("--register-mcp", calls[0])
        self.assertNotIn("--guided", calls[0])

    def test_old_python_fails_before_loading_or_installing(self):
        output = io.StringIO()
        with mock.patch.object(setup.sys, "version_info", (3, 10, 14)), \
                mock.patch.object(setup, "load_installer") as load, \
                contextlib.redirect_stderr(output):
            self.assertEqual(setup.main(self.base, source=ROOT), 1)
        load.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "python_unsupported")
        self.assertFalse(self.destination.exists())

    def test_installer_failure_never_starts_cli_or_exposes_private_path(self):
        output = io.StringIO()
        install = mock.Mock(side_effect=ValueError("/private/owner/secret"))
        run = mock.Mock()
        with contextlib.redirect_stderr(output):
            self.assertEqual(setup.main(self.base, source=ROOT, installer=install,
                                        runner=run), 1)
        run.assert_not_called()
        self.assertNotIn("/private/owner/secret", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "install_failed")

    def test_launch_failure_is_actionable_without_raw_exception(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(setup.main(self.base, source=ROOT,
                runner=mock.Mock(side_effect=OSError("/private/owner/secret"))), 1)
        self.assertNotIn("/private/owner/secret", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "setup_launch_failed")

    def make_fixture_bundle(self):
        source = self.root / "temporary source"
        for name in installer.REQUIRED:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        # The installed CLI is a fixture which consumes stdin and records only
        # argument/environment behavior. It cannot invoke Codex or credentials.
        (source / "codex_bridge" / "__init__.py").write_text("", encoding="utf-8")
        (source / "codex_bridge" / "cli.py").write_text(
            "import json, os, pathlib, sys\n"
            "answer = input('Fixture choice: ') if '--guided' in sys.argv else 'batch'\n"
            "print(json.dumps({'answer': answer, 'args': sys.argv[1:], "
            "'source': str(pathlib.Path(__file__).resolve()), 'cwd': os.getcwd(), 'python': sys.executable}))\n",
            encoding="utf-8")
        (source / "private-token.txt").write_text("owner credential sentinel", encoding="utf-8")
        return source

    def test_real_python_entry_installs_allowlist_and_keeps_interactive_stdin(self):
        source = self.make_fixture_bundle()
        result = subprocess.run(
            [sys.executable, str(source / "scripts" / "setup.py"), *self.base,
             "--skip-registration"], cwd=self.root, input="continue with context\n",
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reply = json.loads(result.stdout.split("Fixture choice: ", 1)[1])
        self.assertEqual(reply["answer"], "continue with context")
        self.assertEqual(Path(reply["source"]), self.destination / "codex_bridge" / "cli.py")
        self.assertEqual(reply["python"], sys.executable)
        self.assertIn("--guided", reply["args"])
        self.assertNotIn("--register-mcp", reply["args"])
        self.assertFalse((self.destination / "private-token.txt").exists())
        self.assertEqual(reply['cwd'], str(self.root))

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows wrappers')
    def test_windows_setup_and_bridge_ignore_cwd_shadow_preserving_relative_args(self):
        source = self.make_fixture_bundle()
        shadow = self.root/'codex_bridge'; shadow.mkdir()
        (shadow/'__init__.py').write_text('raise RuntimeError("UNTRUSTED CWD SHADOW")')
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File']
        result = subprocess.run([*command, str(source/'scripts'/'setup.ps1'), '-PythonExe', sys.executable,
            '-InstallDirectory', str(self.destination), '-ConfigPath', str(self.config),
            '-Batch', '-SkipRegistration', '-CodexExe', 'relative/codex.exe'], cwd=self.root,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        answer = json.loads(result.stdout)
        self.assertEqual(Path(answer['source']), self.destination/'codex_bridge'/'cli.py')
        self.assertEqual(answer['cwd'], str(self.root))
        self.assertIn('relative/codex.exe', answer['args'])
        result = subprocess.run([*command, str(self.destination/'scripts'/'bridge.ps1'),
            '--batch', '--import-file', 'relative/invitation.json'], cwd=self.root,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        answer = json.loads(result.stdout)
        self.assertEqual(Path(answer['source']), self.destination/'codex_bridge'/'cli.py')
        self.assertEqual(answer['cwd'], str(self.root))
        self.assertIn('relative/invitation.json', answer['args'])

    def test_unrelated_destination_and_launcher_conflict_leave_files_untouched(self):
        self.destination.mkdir(parents=True)
        sentinel = self.destination/'owner.txt'; sentinel.write_text('unrelated')
        with self.assertRaisesRegex(ValueError, 'Nonempty destination'):
            installer.install(ROOT, self.destination, self.config)
        self.assertEqual(list(self.destination.iterdir()), [sentinel])
        sentinel.unlink()
        installer.install(ROOT, self.destination, self.config)
        launcher = self.destination/'.mcp.json'
        before = launcher.read_bytes()
        with self.assertRaisesRegex(ValueError, 'different settings'):
            installer.install(ROOT, self.destination, self.root/'other.json')
        self.assertEqual(launcher.read_bytes(), before)

    def test_changed_source_requires_explicit_upgrade_and_mcp_stays_byte_identical(self):
        source = self.make_fixture_bundle()
        installer.install(source, self.destination, self.config)
        launcher = self.destination/'.mcp.json'; before = launcher.read_bytes()
        installed = self.destination/'codex_bridge'/'cli.py'; old = installed.read_bytes()
        (source/'codex_bridge'/'cli.py').write_text('# new release\n')
        with self.assertRaisesRegex(ValueError, '--upgrade'):
            installer.install(source, self.destination, self.config)
        self.assertEqual(installed.read_bytes(), old)
        installer.install(source, self.destination, self.config, upgrade=True)
        self.assertEqual(installed.read_text(), '# new release\n')
        self.assertEqual(launcher.read_bytes(), before)

    def test_invalid_late_source_file_never_partially_installs(self):
        source = self.make_fixture_bundle()
        (source/'scripts'/'run_bridge.py').write_text('def broken(')
        with self.assertRaises(SyntaxError):
            installer.install(source, self.destination, self.config)
        self.assertFalse(self.destination.exists())

    def test_failed_promotion_restores_changed_files_without_touching_private_state(self):
        source = self.make_fixture_bundle()
        installer.install(source, self.destination, self.config)
        paths = ['codex_bridge/__init__.py', 'codex_bridge/cli.py']
        before = {name: (self.destination/name).read_bytes() for name in paths}
        for name in paths: (source/name).write_text('# revised source\n')
        private = self.destination/'private.log'; private.write_text('retained')
        original = installer.os.replace
        count = 0
        def replace(*args):
            nonlocal count
            count += 1
            if count == 2: raise OSError('simulated disk replacement failure')
            return original(*args)
        with mock.patch.object(installer.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError):
                installer.install(source, self.destination, self.config, upgrade=True)
        self.assertEqual(before, {name: (self.destination/name).read_bytes() for name in paths})
        self.assertEqual(private.read_text(), 'retained')

    @unittest.skipUnless(os.name == "posix", "POSIX setup.sh entry")
    def test_shell_entry_respects_python_override_and_arguments(self):
        source = self.make_fixture_bundle()
        environment = dict(os.environ, CODEX_BRIDGE_PYTHON=sys.executable)
        result = subprocess.run(
            ["sh", str(source / "scripts" / "setup.sh"), *self.base,
             "--batch", "--skip-registration", "--peer-id", "computer-b"],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual(reply["python"], sys.executable)
        self.assertEqual(reply["answer"], "batch")
        self.assertIn("computer-b", reply["args"])

    @unittest.skipUnless(os.name == "posix", "POSIX missing Python diagnostic")
    def test_shell_entry_explains_missing_python(self):
        environment = dict(os.environ, CODEX_BRIDGE_PYTHON=str(self.root / "no-python"))
        result = subprocess.run(["sh", str(ROOT / "scripts" / "setup.sh")],
            env=environment, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11+", result.stderr)
        self.assertIn("CODEX_BRIDGE_PYTHON", result.stderr)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
