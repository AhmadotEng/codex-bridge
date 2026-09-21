"""Owner-login startup and exact process ownership; no real tasks are registered."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from codex_bridge import autostart, cli, processes
from codex_bridge.core import BridgeError


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='codex-bridge-startup-')
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.path = self.root / 'config.json'
        self.cfg = {'version': 1, 'peer_id': 'startup-test', 'listen_port': 47321, 'state_dir': str(self.state),
                    'codex_path': sys.executable, 'local_token': 'test-only-' + 'x' * 32, 'peers': {}, 'projects': {}}
        self.path.write_text(json.dumps(self.cfg), encoding='utf-8')

    def tearDown(self):
        self.temporary.cleanup()

    def configure(self, component='daemon'):
        data = {'version': 1, 'components': {component: {'enabled': True, 'task_name': autostart.task_name(self.path, component)}}}
        (self.state / 'autostart.json').write_text(json.dumps(data), encoding='utf-8')

    def parser_check(self, script):
        if os.name != 'nt':
            return
        selected = self.state / 'parse-only.ps1'
        selected.write_text(script, encoding='utf-8-sig')
        cli.powershell("$tokens=$null; $errors=$null\n[Management.Automation.Language.Parser]::ParseFile(" + cli.ps_literal(selected) + ",[ref]$tokens,[ref]$errors) | Out-Null\nif($errors.Count){throw ($errors | Out-String)}")

    def test_registration_is_hidden_limited_owner_login_and_not_started_now(self):
        with mock.patch.object(autostart, '_windows'), mock.patch.object(autostart, '_pythonw', return_value=Path(sys.executable)), mock.patch.object(cli, 'powershell') as ps:
            result = autostart.enable(self.path)
        script = ps.call_args.args[0]
        self.assertEqual(result['enabled_components'], [{'component': 'daemon', 'peer_id': None}])
        self.assertFalse(result['started_now'])
        for fragment in ['-AtLogOn -User $identity.Name', '-LogonType Interactive -RunLevel Limited', '-Hidden', '-MultipleInstances IgnoreNew', '-ExecutionTimeLimit ([TimeSpan]::Zero)', '-RestartCount 3', '-AllowStartIfOnBatteries', '-DontStopIfGoingOnBatteries']:
            self.assertIn(fragment, script)
        for forbidden in ['-Password', '-RunLevel Highest', 'Start-ScheduledTask', 'Unregister-ScheduledTask']:
            self.assertNotIn(forbidden, script)
        for policy in ['$owner -ne $identity.User.Value', "$task.Principal.LogonType -ne 'Interactive'", "$task.Principal.RunLevel -ne 'Limited'", '$ownerTriggers.Count -ne 1']:
            self.assertIn(policy, script)
        self.assertTrue(autostart.enabled(self.path, 'daemon'))
        self.parser_check(script)

    def test_auto_selection_uses_config_not_computer_or_project_names(self):
        self.cfg['peers'] = {'peer-one': {'enabled': True}}
        self.cfg['ssh_transports'] = {'peer-one': {'enabled': True}}
        self.path.write_text(json.dumps(self.cfg), encoding='utf-8')
        with mock.patch.object(autostart, '_windows'), mock.patch.object(autostart, '_pythonw', return_value=Path(sys.executable)), mock.patch('codex_bridge.transport.transport_args') as validate, mock.patch.object(cli, 'powershell'):
            result = autostart.enable(self.path)
        self.assertEqual(result['enabled_components'], [{'component': 'daemon', 'peer_id': None}, {'component': 'transport', 'peer_id': 'peer-one'}])
        validate.assert_called_once()

    def test_transport_startup_rejects_disabled_transport(self):
        with mock.patch.object(autostart, '_windows'), mock.patch.object(cli, 'powershell') as ps:
            with self.assertRaises(BridgeError):
                autostart.enable(self.path, 'transport')
        ps.assert_not_called()

    def test_disable_preserves_running_work_and_remove_requires_stopped_wrapper(self):
        self.configure()
        with cli.process_lock(self.state / 'autostart-daemon.lock'):
            with mock.patch.object(autostart, '_windows'), mock.patch.object(cli, 'powershell') as ps:
                result = autostart.disable(self.path, 'daemon')
                self.assertFalse(result['running_work_stopped'])
                self.assertIn('Disable-ScheduledTask', ps.call_args.args[0])
                self.assertNotIn('Stop-ScheduledTask', ps.call_args.args[0])
                self.assertNotIn('Unregister-ScheduledTask', ps.call_args.args[0])
                with self.assertRaises(BridgeError) as caught:
                    autostart.disable(self.path, 'daemon', remove=True)
                self.assertEqual(caught.exception.code, 'component_running')
        with mock.patch.object(autostart, '_windows'), mock.patch.object(cli, 'powershell') as ps:
            result = autostart.disable(self.path, 'daemon', remove=True)
        self.assertTrue(result['registrations_removed'])
        self.assertIn('Unregister-ScheduledTask', ps.call_args.args[0])
        self.parser_check(ps.call_args.args[0])

    def test_persistent_task_takes_precedence_over_background_launch(self):
        self.configure()
        reply = {'ok': True, 'result': {'sessions': []}}
        with mock.patch.object(cli, 'call', side_effect=[ConnectionRefusedError(), reply]), mock.patch.object(autostart, 'start', return_value={'launch_mode': 'owner-login-task'}) as start, mock.patch.object(cli, 'launch') as ordinary:
            result = cli.start_daemon(self.path, interactive=False)
        self.assertTrue(result['ready'])
        self.assertEqual(result['launch_mode'], 'owner-login-task')
        start.assert_called_once_with(self.path, 'daemon')
        ordinary.assert_not_called()

    def test_remote_stop_uses_running_desktop_login_identity(self):
        cli.save(autostart.runner_record(self.path, 'daemon'), processes.record(os.getpid(), login_identity='desktop-login'))
        with mock.patch.object(processes, 'login_identity', return_value='ssh-login'):
            autostart.request_stop(self.path, 'daemon')
        marker = cli.read(autostart.stop_path(self.path, 'daemon'))
        self.assertEqual(marker['login_identity'], 'desktop-login')

    def test_repeated_remote_stop_after_runner_exit_preserves_desktop_marker(self):
        cli.save(autostart.stop_path(self.path, 'daemon'), {'login_identity': 'desktop-login'})
        with mock.patch.object(processes, 'interactive_session', return_value=False), mock.patch.object(processes, 'login_identity', return_value='ssh-login'):
            autostart.request_stop(self.path, 'daemon')
        self.assertEqual(cli.read(autostart.stop_path(self.path, 'daemon'))['login_identity'], 'desktop-login')
        with mock.patch.object(processes, 'interactive_session', return_value=True), mock.patch.object(processes, 'login_identity', return_value='new-desktop-login'):
            autostart.request_stop(self.path, 'daemon')
        self.assertEqual(cli.read(autostart.stop_path(self.path, 'daemon'))['login_identity'], 'new-desktop-login')

    def test_manual_stop_survives_scheduler_retry_but_new_login_clears_it(self):
        self.configure()
        cli.save(autostart.stop_path(self.path, 'daemon'), {'login_identity': 'first-login'})
        with mock.patch.object(autostart, '_windows'), mock.patch.object(processes, 'login_identity', return_value='first-login'), mock.patch.object(autostart, 'supervise') as supervisor:
            self.assertEqual(autostart.run(self.path, 'daemon'), 0)
            supervisor.assert_not_called()
        self.assertFalse(autostart.runner_record(self.path, 'daemon').exists())
        with mock.patch.object(autostart, '_windows'), mock.patch.object(processes, 'login_identity', return_value='next-login'), mock.patch.object(autostart, 'supervise', return_value=0) as supervisor:
            self.assertEqual(autostart.run(self.path, 'daemon'), 0)
            supervisor.assert_called_once()
        self.assertFalse(autostart.stop_path(self.path, 'daemon').exists())

    def test_auto_enable_snapshots_peer_scope_without_enabling_future_peers(self):
        self.cfg.update(peers={'first-peer': {'enabled': True}}, ssh_transports={'first-peer': {'enabled': True}})
        self.path.write_text(json.dumps(self.cfg))
        with mock.patch.object(autostart, '_windows'), mock.patch.object(autostart, '_pythonw', return_value=Path(sys.executable)), mock.patch('codex_bridge.transport.transport_args'), mock.patch.object(cli, 'powershell'):
            autostart.enable(self.path)
        self.cfg['peers']['later-peer'] = {'enabled': True}
        self.cfg['ssh_transports']['later-peer'] = {'enabled': True}
        self.path.write_text(json.dumps(self.cfg))
        self.assertTrue(autostart.enabled(self.path, 'transport', peer_id='first-peer'))
        self.assertFalse(autostart.enabled(self.path, 'transport', peer_id='later-peer'))
        self.assertNotEqual(autostart.task_name(self.path, 'transport', 'first-peer'), autostart.task_name(self.path, 'transport', 'later-peer'))
        args = autostart.settings(self.path)['transports']['first-peer']['arguments']
        self.assertIn('--peer first-peer', args)
        self.assertNotIn('later-peer', args)

    def test_peer_disable_preserves_other_registrations_and_running_component(self):
        cli.save(self.state/'autostart.json', {'version': 2, 'components': {}, 'transports': {'one': {'enabled': True}, 'two': {'enabled': True}}})
        lock = autostart.runner_lock(self.path, 'transport', 'one')
        with cli.process_lock(lock), mock.patch.object(autostart, '_windows'), mock.patch.object(cli, 'powershell') as ps:
            result = autostart.disable(self.path, 'transport', peer_id='one')
        self.assertFalse(result['running_work_stopped'])
        self.assertFalse(autostart.enabled(self.path, 'transport', 'one'))
        self.assertTrue(autostart.enabled(self.path, 'transport', 'two'))
        self.assertNotIn(autostart.task_name(self.path, 'transport', 'two'), ps.call_args.args[0])
        self.assertNotIn('Stop-ScheduledTask', ps.call_args.args[0])

    def test_legacy_transport_requires_explicit_scope_and_migration_preserves_work(self):
        self.configure('transport')
        with mock.patch.object(autostart, '_windows'), mock.patch.object(autostart, 'supervise') as supervisor:
            self.assertEqual(autostart.run(self.path, 'transport'), 1)
        supervisor.assert_not_called()
        self.assertFalse(autostart.runner_record(self.path, 'transport').exists())
        self.cfg.update(peers={'selected-peer': {'enabled': True}}, ssh_transports={'selected-peer': {'enabled': True}})
        self.path.write_text(json.dumps(self.cfg))
        with mock.patch.object(autostart, '_windows'), mock.patch.object(autostart, '_pythonw', return_value=Path(sys.executable)), mock.patch('codex_bridge.transport.transport_args'), mock.patch.object(cli, 'powershell') as ps:
            result = autostart.enable(self.path, 'transport', 'selected-peer')
        self.assertTrue(result['legacy_transport_disabled'])
        self.assertFalse(autostart.enabled(self.path, 'transport'))
        self.assertTrue(autostart.enabled(self.path, 'transport', 'selected-peer'))
        self.assertIn('Disable-ScheduledTask', ps.call_args.args[0])
        self.assertNotIn('Stop-ScheduledTask', ps.call_args.args[0])

    def test_runner_marker_clear_cannot_erase_concurrent_manual_stop(self):
        self.configure()
        marker = autostart.stop_path(self.path, 'daemon')
        cli.save(marker, {'login_identity': 'previous-login'})
        clearing, writer_started, stop_written = threading.Event(), threading.Event(), threading.Event()
        old_clear = autostart.clear_stop
        failures = []
        def clear(path, component, peer_id=None):
            clearing.set()
            self.assertTrue(writer_started.wait(3))
            self.assertFalse(stop_written.wait(.1), 'Stop must wait for the marker check/clear lock')
            old_clear(path, component, peer_id)
        def stop():
            try:
                if not clearing.wait(3): raise AssertionError('Runner never reached marker clear')
                writer_started.set()
                with cli.daemon_control(self.path): autostart.request_stop(self.path, 'daemon')
                stop_written.set()
            except Exception as exc: failures.append(repr(exc))
        def supervise(*args):
            self.assertTrue(stop_written.wait(3))
            return 0
        with mock.patch.object(autostart, '_windows'), mock.patch.object(processes, 'login_identity', return_value='current-login'), mock.patch.object(autostart, 'clear_stop', side_effect=clear), mock.patch.object(autostart, 'supervise', side_effect=supervise):
            thread = threading.Thread(target=stop); thread.start()
            self.assertEqual(autostart.run(self.path, 'daemon'), 0)
            thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertFalse(failures, failures)
        self.assertEqual(cli.read(marker)['login_identity'], 'current-login')
        self.assertFalse(autostart.runner_record(self.path, 'daemon').exists())

    def test_ambiguous_legacy_stop_marker_is_preserved_until_explicit_start(self):
        self.configure(); marker = autostart.stop_path(self.path, 'daemon'); marker.touch()
        with mock.patch.object(autostart, '_windows'), mock.patch.object(autostart, 'supervise') as supervise:
            self.assertEqual(autostart.run(self.path, 'daemon'), 0)
        supervise.assert_not_called(); self.assertTrue(marker.exists())

    def test_successful_stop_is_not_restarted(self):
        child = mock.Mock(pid=3456)
        child.poll.return_value = 0
        owner = mock.Mock(process=child)
        with mock.patch.object(processes, 'OwnedProcess', return_value=owner) as spawn:
            result = autostart.supervise(self.path, 'daemon', io.StringIO())
        self.assertEqual(result, 0)
        spawn.assert_called_once()
        owner.close.assert_called_once()

    def test_hidden_runner_retains_launch_failure_in_local_log(self):
        self.configure()
        with mock.patch.object(autostart, '_windows'), mock.patch.object(autostart, 'supervise', side_effect=OSError('fixture runtime missing')):
            self.assertEqual(autostart.run(self.path, 'daemon'), 1)
        entry = json.loads((self.state / 'autostart-daemon.log').read_text().splitlines()[-1])
        self.assertEqual(entry['event'], 'startup_failed')
        self.assertEqual(entry['error']['code'], 'startup_failed')
        self.assertFalse(autostart.runner_record(self.path, 'daemon').exists())

    def test_failure_restarts_are_bounded_and_stop_during_backoff_wins(self):
        child = mock.Mock(pid=3456)
        child.poll.return_value = 2
        owner = mock.Mock(process=child)
        with mock.patch.object(processes, 'OwnedProcess', return_value=owner) as spawn, mock.patch.object(autostart, 'RESTART_DELAYS', (0, 0)):
            self.assertEqual(autostart.supervise(self.path, 'daemon', io.StringIO()), 1)
        self.assertEqual(spawn.call_count, 3)
        def stopping_sleep(seconds):
            autostart.stop_path(self.path, 'daemon').touch()
        with mock.patch.object(processes, 'OwnedProcess', return_value=owner) as spawn, mock.patch.object(autostart.time, 'sleep', side_effect=stopping_sleep):
            self.assertEqual(autostart.supervise(self.path, 'daemon', io.StringIO()), 0)
        spawn.assert_called_once()

    def test_existing_component_is_not_adopted_or_duplicated(self):
        with cli.process_lock(self.state / 'serve.lock'), mock.patch.object(processes, 'OwnedProcess') as spawn:
            self.assertEqual(autostart.supervise(self.path, 'daemon', io.StringIO()), 0)
        spawn.assert_not_called()

    def test_status_distinguishes_registration_process_and_end_to_end_readiness(self):
        self.configure()
        rows = [{'component': name, 'registered': name == 'daemon', 'enabled': name == 'daemon', 'state': 'Ready'} for name in autostart.COMPONENTS]
        with mock.patch.object(autostart, '_windows'), mock.patch.object(cli, 'powershell', return_value=json.dumps(rows)) as ps:
            result = autostart.status(self.path)
        self.assertTrue(result['components'][0]['configured_enabled'])
        self.assertFalse(result['components'][0]['runner']['running'])
        self.assertIn('does not verify SSH', result['readiness'])
        self.parser_check(ps.call_args.args[0])

    def test_process_creation_identity_rejects_reused_or_unverifiable_pid(self):
        current = processes.record(os.getpid())
        self.assertTrue(processes.matches(current))
        altered = {**current, 'process_identity': {**current['process_identity'], 'creation_time': 'not-this-process'}}
        self.assertFalse(processes.matches(altered))
        self.assertFalse(processes.matches({'pid': os.getpid()}))
        self.assertIsNone(processes.identity(-1))
        self.assertEqual(processes.login_identity(), processes.login_identity())

    @unittest.skipUnless(os.name == 'posix', 'POSIX waitable-child process group cleanup')
    def test_exited_owned_parent_retains_pid_until_its_live_descendant_is_cleaned(self):
        selected = self.root/'descendant.json'
        script = "import json,subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); Path(sys.argv[1]).write_text(json.dumps({'pid':p.pid}))"
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        owner = processes.OwnedProcess([sys.executable, '-c', script, str(selected)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(owner.process.wait(timeout=10), 0)
            descendant = json.loads(selected.read_text())['pid']
            retained = processes.record(descendant)
            self.assertTrue(processes.matches(retained))
            owner.close()
            deadline = time.monotonic()+5
            while processes.matches(retained) and time.monotonic()<deadline: time.sleep(.05)
            self.assertFalse(processes.matches(retained))
            self.assertIsNone(unrelated.poll())
            owner.close()  # Idempotent; never signal a reused PID after reaping.
        finally:
            owner.close(); unrelated.terminate(); unrelated.wait(timeout=5)

    @unittest.skipUnless(os.name == 'nt', 'Windows job ownership')
    def test_owned_process_closes_child_and_grandchild_without_touching_other_process(self):
        selected = self.root / 'descendant.json'
        script = "import json,subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); Path(sys.argv[1]).write_text(json.dumps({'pid':p.pid})); time.sleep(60)"
        owner = processes.OwnedProcess([sys.executable, '-c', script, str(selected)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not selected.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(selected.exists())
            descendant = json.loads(selected.read_text())['pid']
            child_identity = processes.record(owner.process.pid)
            descendant_identity = processes.record(descendant)
            self.assertTrue(processes.matches(child_identity))
            self.assertTrue(processes.matches(descendant_identity))
        finally:
            owner.close()
        deadline = time.monotonic() + 5
        while processes.matches(descendant_identity) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse(processes.matches(child_identity))
        self.assertFalse(processes.matches(descendant_identity))
        self.assertIsNotNone(processes.identity(os.getpid()))


if __name__ == '__main__':
    unittest.main()
