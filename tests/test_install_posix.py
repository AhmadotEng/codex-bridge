import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge_portable_installer", ROOT/"scripts"/"install.py")
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)


class PortableInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_allowlist_and_existing_configuration_preserved(self):
        source = self.root/"source"; target = self.root/"installed"/"codex-bridge"
        for name in installer.REQUIRED:
            file = source/name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text("fixture")
        for name in (".git/config", "config.json", "codex_bridge/extra_private_module.py", "private_notes.txt", "state/data.json", "Codex-Bridge-Friend-Runtime-Repair.md"):
            file = source/name; file.parent.mkdir(parents=True, exist_ok=True); file.write_text("private")
        guide = source/'docs'/'RUNTIME.md'; guide.parent.mkdir(parents=True); guide.write_text('Generic runtime repair')
        target.mkdir(parents=True)
        local = target/"config.json"; local.write_text("owner config")
        installer.install(source, target, local)
        self.assertEqual(local.read_text(), "owner config")
        self.assertFalse((target/".git").exists())
        self.assertFalse((target/"codex_bridge"/"extra_private_module.py").exists())
        self.assertFalse((target/"private_notes.txt").exists())
        self.assertFalse((target/"Codex-Bridge-Friend-Runtime-Repair.md").exists())
        self.assertEqual((target/'docs'/'RUNTIME.md').read_text(), 'Generic runtime repair')
        self.assertEqual(json.loads((target/".mcp.json").read_text())["mcpServers"]["codex_bridge"]["command"], sys.executable)

    def test_missing_required_and_nested_targets_fail_before_copy(self):
        source = self.root/"source"; source.mkdir()
        with self.assertRaises(ValueError): installer.install(source, source/"codex-bridge", self.root/"config.json")
        target = self.root/"codex-bridge"
        with self.assertRaises(ValueError): installer.install(source, target, self.root/"config.json")
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX shell and local background lifecycle")
    def test_posix_launcher_install_init_start_status_stop(self):
        target = self.root/"installed"/"codex-bridge"
        config = self.root/"private"/"config.json"
        env = dict(os.environ, CODEX_BRIDGE_PYTHON=sys.executable)
        installed = subprocess.run(["sh", str(ROOT/"scripts"/"install.sh"), "--directory", str(target), "--config", str(config)],
                                   capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(installed.returncode, 0, installed.stderr+installed.stdout)
        # A protocol fixture answers initialization only: no account, token or
        # actual Codex inference is involved in this process-lifecycle test.
        from tests.test_compatibility import write_bundle
        schema = self.root/"schema"; write_bundle(schema)
        fake_codex = self.root/"fake-codex"
        fake_codex.write_text("#!" + sys.executable + "\n" + "\n".join([
            "import json, shutil, sys",
            "from pathlib import Path",
            "if '--version' in sys.argv:",
            "    print('codex-cli 0.153.4'); raise SystemExit(0)",
            "if 'generate-json-schema' in sys.argv:",
            "    shutil.copytree(" + repr(str(schema)) + ", sys.argv[sys.argv.index('--out')+1], dirs_exist_ok=True)",
            "    raise SystemExit(0)",
            "for line in sys.stdin:",
            "    item=json.loads(line)",
            "    if 'id' in item:",
            "        print(json.dumps({'id':item['id'],'result':{'platformOs':sys.platform}}), flush=True)",
        ]) + "\n")
        fake_codex.chmod(0o700)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        def run(*args):
            result = subprocess.run(["sh", str(target/"scripts"/"bridge.sh"), *args],
                capture_output=True, text=True, env=env, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
            return json.loads(result.stdout)
        run("init", "--peer-id", "computer-a", "--codex", str(fake_codex), "--port", str(port))
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        try:
            run("start", "--background")
            status = run("status")
            self.assertNotIn("error", status)
        finally:
            if config.exists(): run("stop")

    @unittest.skipUnless(os.name == "posix", "POSIX symlink behavior")
    def test_linked_destination_is_rejected(self):
        target = self.root/"codex-bridge"; outside = self.root/"outside"; outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): installer.install(ROOT, target, self.root/"private"/"config.json")

    def test_launcher_link_is_rejected_before_any_runtime_file_is_copied(self):
        target = self.root/"codex-bridge"; target.mkdir()
        outside = self.root/"owner-settings.json"; outside.write_text("preserve owner settings")
        link = target/".mcp.json"
        try:
            link.symlink_to(outside)
        except OSError:
            if os.name == "nt": self.skipTest("File symlink privileges unavailable on this Windows runner")
            raise
        try:
            with self.assertRaises(ValueError): installer.install(ROOT, target, self.root/"config.json")
            self.assertEqual(outside.read_text(), "preserve owner settings")
            self.assertEqual(list(target.iterdir()), [link])
        finally:
            link.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows junction handling on Python 3.11+")
    def test_windows_destination_junction_is_rejected_without_following_it(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not powershell: self.skipTest("PowerShell unavailable")
        target = self.root/"codex-bridge"; outside = self.root/"owner-directory"; outside.mkdir()
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        script = 'New-Item -ItemType Junction -Path ' + quote(target) + ' -Value ' + quote(outside) + ' -ErrorAction Stop | Out-Null'
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=20)
        if result.returncode: self.skipTest("Directory junction creation is unavailable on this runner")
        try:
            with self.assertRaises(ValueError): installer.install(ROOT, target, self.root/"config.json")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            target.rmdir()
