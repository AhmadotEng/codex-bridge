"""Unconfirmed SSH shutdown retains exact ownership; no real peers are used."""
import asyncio
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from codex_bridge import autostart, cli, processes, transport
from codex_bridge.connections import ConnectionManager
from codex_bridge.core import BridgeError, Store


class Child:
    pid = 987654  # Fixture only; never passed to a real OS process operation.
    args = ['fixture-owned-child']
    def __init__(self):
        self.returncode = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.wait_fails = True
    def poll(self): return self.returncode
    def kill(self): self.kill_calls += 1
    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_fails: raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = 0
        return self.returncode


class Owner:
    def __init__(self, *, fails=True, broken=False):
        self.process = Child()
        self.fails, self.broken, self.calls = fails, broken, 0
    def close(self):
        self.calls += 1
        if self.fails: raise subprocess.TimeoutExpired(self.process.args, 5)
        if not self.broken: self.process.returncode = 0


class Errors:
    def __init__(self): self.calls = 0
    def finish(self): self.calls += 1


class ProcessCloseTests(unittest.TestCase):
    def test_windows_timeout_propagates_and_same_handle_can_retry(self):
        owned = processes.OwnedProcess.__new__(processes.OwnedProcess)
        child = owned.process = Child()
        closed = []
        owned.job = SimpleNamespace(close=lambda: closed.append('job'))
        with patch.object(processes.os, 'name', 'nt'):
            with self.assertRaises(subprocess.TimeoutExpired): owned.close()
            self.assertIs(owned.process, child)
            self.assertIsNone(child.poll())
            child.wait_fails = False
            owned.close()
        self.assertEqual(child.wait_calls, 2)
        self.assertEqual(child.kill_calls, 2)
        self.assertEqual(closed, ['job'])
        self.assertEqual(child.poll(), 0)

    def test_posix_wait_timeout_keeps_waitable_identity_through_exec_and_retry(self):
        child = Child()
        retained = processes._RetainedProcess(child)
        # exec changes an executable name, not the still-waitable owned child.
        # No persistent PID/executable lookup is used to kill this child group.
        with patch.object(processes, '_waitable_exit', return_value=None), \
                patch.object(processes.os, 'killpg', create=True) as kill, \
                patch.object(signal, 'SIGKILL', 9, create=True), \
                patch.object(processes, 'identity', side_effect=AssertionError('No PID adoption')):
            with self.assertRaises(subprocess.TimeoutExpired): retained.close()
            self.assertFalse(retained._closed)
            self.assertTrue(retained._owned)
            child.args = ['fixture-ssh-after-exec']
            child.wait_fails = False
            retained.close()
        self.assertTrue(retained._closed)
        self.assertEqual(kill.call_count, 2)
        self.assertEqual(retained.poll(), 0)

    def test_autostart_does_not_respawn_or_report_exit_after_unconfirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            owner = Owner()
            owner.process.returncode = 1
            log = io.StringIO()
            with patch.object(autostart, 'component_directory', return_value=state), \
                    patch.object(autostart, 'component_lock', return_value=state / 'lock'), \
                    patch.object(autostart, 'stop_path', return_value=state / 'stop'), \
                    patch.object(cli, 'lock_held', return_value=False), \
                    patch.object(processes, 'OwnedProcess', return_value=owner) as spawn:
                with self.assertRaises(subprocess.TimeoutExpired):
                    autostart.supervise(state / 'config.json', 'daemon', log)
            self.assertEqual(spawn.call_count, 1)
            self.assertIn('component_started', log.getvalue())
            self.assertNotIn('component_exited', log.getvalue())
            self.assertNotIn('component_restart_wait', log.getvalue())

    def test_legacy_supervisor_keeps_child_receipt_and_never_retries_after_close_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.mkdir()
            selected = state / 'transports' / 'bravo'
            owner = Owner()
            owner.process.stderr = io.BytesIO()
            config = {'state_dir': str(state), 'peers': {'bravo': {'enabled': True}}}
            def save(path, value):
                Path(path).write_text(json.dumps(value), encoding='utf-8')
                if Path(path).name == 'status.json' and value['state'] == 'forwarding_unverified':
                    (selected / 'stop').write_text('fixture stop', encoding='utf-8')
            with patch.object(cli, 'read_config', return_value=config), \
                    patch.object(cli, 'save', side_effect=save), \
                    patch.object(transport, '_selected'), \
                    patch.object(transport, 'transport_directory', return_value=selected), \
                    patch.object(transport, 'configured_transports', return_value={'bravo': {'enabled': True}}), \
                    patch.object(transport, 'transport_args', return_value=['fixture-ssh']), \
                    patch.object(transport, '_spawn_ssh', return_value=owner) as spawn, \
                    patch.object(processes, 'record', return_value={'pid': Child.pid, 'process_identity': {'creation_time': 'fixture'}}):
                with self.assertRaises(subprocess.TimeoutExpired):
                    transport.supervise_transport(root / 'config.json', 'bravo')
            self.assertEqual(spawn.call_count, 1)
            self.assertTrue((selected / 'ssh.pid.json').exists())
            status = json.loads((selected / 'status.json').read_text(encoding='utf-8'))
            self.assertNotEqual(status['state'], 'stopped')


class ManagerCloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = Store(root / 'state.sqlite')
        self.cfg = {'peer_id': 'alpha', 'listen_port': 47321, 'state_dir': str(root),
            'peers': {'bravo': {'enabled': True}},
            'connections': {'bravo': {'mode': 'on_demand', 'lanes': {
                'alpha': {'alpha': 48001, 'bravo': 48002},
                'bravo': {'alpha': 48003, 'bravo': 48004}}}}}
        self.bridge = SimpleNamespace(node_id='alpha', store=self.store,
            config=lambda: self.cfg,
            connection_activity=lambda peer: False)
        def peer_entry(peer):
            item = self.cfg['peers'][peer]
            if not item.get('enabled'): raise BridgeError('access_revoked', 'Fixture peer revoked')
            return item
        self.bridge.peer = peer_entry
        self.manager = ConnectionManager(self.bridge)

    async def asyncTearDown(self):
        for owner, _ in self.manager.owners.values():
            owner.fails = owner.broken = False
        await self.manager.close()
        self.store.db.close()
        self.temporary.cleanup()

    def add(self, owner=None):
        attempt = str(uuid.uuid4())
        owner = owner or Owner()
        errors = Errors()
        self.manager.owners[('bravo', attempt)] = (owner, errors)
        self.manager.local_candidates.setdefault('bravo', set()).add(attempt)
        self.store.put('connection_children', attempt,
            {'peer_id': 'bravo', 'attempt_id': attempt, 'pid': Child.pid,
             'process_identity': {'creation_time': 'fixture-creation'}, 'closed': False})
        return attempt, owner, errors

    async def fails_close(self, attempt):
        with self.assertRaises(BridgeError) as caught:
            await self.manager._close_owner('bravo', attempt)
        self.assertEqual(caught.exception.code, 'ssh_cleanup_unconfirmed')

    async def test_failure_retains_handle_candidate_and_open_journal_then_retry_closes(self):
        attempt, owner, errors = self.add()
        await self.fails_close(attempt)
        self.assertIs(self.manager.owners[('bravo', attempt)][0], owner)
        self.assertIn(attempt, self.manager.local_candidates['bravo'])
        self.assertFalse(self.store.get('connection_children', attempt)['closed'])
        self.assertEqual(self.manager.status('bravo')['cleanup_pending'], [attempt])
        self.assertEqual(errors.calls, 0)
        owner.fails = False
        with patch.object(processes, 'identity', side_effect=AssertionError('No stale PID lookup')):
            await self.manager._close_owner('bravo', attempt)
        self.assertNotIn(('bravo', attempt), self.manager.owners)
        self.assertNotIn(attempt, self.manager.local_candidates['bravo'])
        self.assertTrue(self.store.get('connection_children', attempt)['closed'])
        self.assertNotIn('close_error', self.store.get('connection_children', attempt))
        self.assertEqual(self.manager.status('bravo')['cleanup_pending'], [])

    async def test_returning_without_child_exit_is_not_a_successful_close(self):
        attempt, owner, _ = self.add(Owner(fails=False, broken=True))
        await self.fails_close(attempt)
        self.assertIsNone(owner.process.poll())
        self.assertIn(('bravo', attempt), self.manager.owners)
        self.assertFalse(self.store.get('connection_children', attempt)['closed'])

    async def test_same_disconnect_uuid_retries_cleanup_without_false_receipt(self):
        attempt, owner, _ = self.add()
        request = str(uuid.uuid4())
        with self.assertRaises(BridgeError): await self.manager.disconnect('bravo', request)
        self.assertTrue(self.manager.status('bravo')['stop_requested'])
        self.assertIsNone(self.store.get('connection_disconnects', 'bravo:' + request))
        self.assertIn(('bravo', attempt), self.manager.owners)
        owner.fails = False
        result = await self.manager.disconnect('bravo', request)
        self.assertTrue(result['stop_requested'])
        self.assertTrue(self.store.get('connection_children', attempt)['closed'])
        count = owner.calls
        self.assertEqual(await self.manager.disconnect('bravo', request), result)
        self.assertEqual(owner.calls, count)

    async def test_failed_loser_cleanup_preserves_winner_and_cleans_other_losers(self):
        winner, winner_owner, _ = self.add(Owner(fails=False))
        failed, failed_owner, _ = self.add()
        cleaned, cleaned_owner, _ = self.add(Owner(fails=False))
        with self.assertRaises(BridgeError): await self.manager._close_losers('bravo', winner)
        self.assertEqual(winner_owner.calls, 0)
        self.assertIsNone(winner_owner.process.poll())
        self.assertIn(('bravo', failed), self.manager.owners)
        self.assertNotIn(('bravo', cleaned), self.manager.owners)
        self.assertEqual(cleaned_owner.calls, 1)
        self.assertEqual(failed_owner.calls, 1)

    async def test_pending_cleanup_blocks_new_demand_and_maintenance_does_not_retry(self):
        attempt, owner, _ = self.add()
        await self.fails_close(attempt)
        for retry in (False, True):
            with self.assertRaises(BridgeError) as caught:
                await self.manager.ensure('bravo', str(uuid.uuid4()), retry=retry)
            self.assertEqual(caught.exception.code, 'ssh_cleanup_unconfirmed')
        for _ in range(3): await self.manager.maintenance()
        self.assertEqual(owner.calls, 1)
        self.assertEqual(self.manager.demands, {})
        self.assertFalse(self.manager.status('bravo')['available'])

    async def test_revocation_closes_other_children_without_looping_failed_cleanup(self):
        failed, failed_owner, _ = self.add()
        winner, winner_owner, _ = self.add(Owner(fails=False))
        await self.fails_close(failed)
        self.cfg['peers']['bravo']['enabled'] = False
        for _ in range(3): await self.manager.maintenance()
        self.assertEqual(failed_owner.calls, 1)
        self.assertEqual(winner_owner.calls, 1)
        self.assertIn(('bravo', failed), self.manager.owners)
        self.assertNotIn(('bravo', winner), self.manager.owners)
        self.assertEqual(self.manager.status('bravo')['state'], 'revoked')

    async def test_revoked_peer_can_retry_only_local_cleanup_without_reauthorization(self):
        attempt, owner, _ = self.add()
        request = str(uuid.uuid4())
        with self.assertRaises(BridgeError): await self.manager.disconnect('bravo', request)
        self.cfg['peers']['bravo']['enabled'] = False
        await self.manager.maintenance()
        state = self.manager._state('bravo')
        state['decision'] = {'state': 'closed', 'dispatch_ready': False,
            'connection_id': attempt, 'generation': 1, 'candidate': {'map_digest': 'fixture'}}
        self.manager._save(state)
        async def forbidden(*args, **kwargs): raise AssertionError('Revoked cleanup sent a remote RPC')
        self.manager._rpc = forbidden
        owner.fails = False
        result = await self.manager.disconnect('bravo', request)
        self.assertTrue(result['local_only'])
        self.assertFalse(result['remote_closure_confirmed'])
        self.assertFalse(self.cfg['peers']['bravo']['enabled'])
        self.assertEqual(owner.calls, 2)
        self.assertTrue(self.store.get('connection_children', attempt)['closed'])
        self.assertEqual(self.manager._state('bravo')['state'], 'revoked')
        self.assertTrue(self.manager._state('bravo')['stop_requested'])
        self.assertEqual(await self.manager.disconnect('bravo', request), result)
        self.assertEqual(owner.calls, 2)
        with self.assertRaises(BridgeError) as caught:
            await self.manager.ensure('bravo', str(uuid.uuid4()), retry=True)
        self.assertEqual(caught.exception.code, 'access_revoked')

    async def test_revoked_local_cleanup_still_refuses_active_work(self):
        attempt, owner, _ = self.add(Owner(fails=False))
        self.cfg['peers']['bravo']['enabled'] = False
        self.bridge.connection_activity = lambda peer: True
        with self.assertRaises(BridgeError) as caught:
            await self.manager.disconnect('bravo', str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, 'connection_busy')
        self.assertEqual(owner.calls, 0)
        self.assertFalse(self.store.get('connection_children', attempt)['closed'])

    async def test_selected_winner_is_preserved_when_demand_cannot_close_duplicate(self):
        failed, loser, _ = self.add()
        winner = Owner(fails=False)
        spawned = []
        async def healthy(peer, decision): return False
        async def opened(peer, candidate):
            spawned.append(candidate['attempt_id'])
            key = candidate['attempt_id']
            self.manager.owners[(peer, key)] = (winner, Errors())
            self.store.put('connection_children', key,
                {'peer_id': peer, 'attempt_id': key, 'closed': False})
        async def selected(peer, candidate):
            state = self.manager._state(peer)
            state.update(state='connected', generation=1, decision={
                'state': 'committed', 'dispatch_ready': True,
                'connection_id': candidate['attempt_id'], 'generation': 1,
                'candidate': candidate})
            self.manager._save(state)
        self.manager._healthy = healthy
        self.manager._open_candidate = opened
        self.manager._select = selected
        result = await self.manager.ensure('bravo', str(uuid.uuid4()))
        self.assertFalse(result['available'])
        self.assertEqual(result['error']['code'], 'ssh_cleanup_unconfirmed')
        self.assertEqual(len(spawned), 1)
        self.assertEqual(result['decision']['connection_id'], spawned[0])
        self.assertTrue(result['decision']['dispatch_ready'])
        self.assertEqual(winner.calls, 0)
        self.assertIn(('bravo', failed), self.manager.owners)
        async with self.manager.route('bravo') as route:
            self.assertEqual(route['connection']['connection_id'], spawned[0])

    async def test_remote_cleanup_refusal_closes_unselected_candidate_without_retry(self):
        for close_fails in (False, True):
            with self.subTest(close_fails=close_fails):
                spawned = []
                owner = Owner(fails=close_fails)
                async def healthy(peer, decision): return False
                async def opened(peer, candidate):
                    key = candidate['attempt_id']; spawned.append(key)
                    self.manager.owners[(peer, key)] = (owner, Errors())
                    self.store.put('connection_children', key,
                        {'peer_id': peer, 'attempt_id': key, 'closed': False})
                async def refused(peer, candidate):
                    raise BridgeError('ssh_cleanup_unconfirmed', 'Remote owned child is closing', True)
                self.manager._healthy, self.manager._open_candidate, self.manager._select = healthy, opened, refused
                result = await self.manager.ensure('bravo', str(uuid.uuid4()), retry=True)
                self.assertEqual(len(spawned), 1)
                self.assertEqual(owner.calls, 1)
                self.assertFalse(result['available'])
                self.assertEqual(result['error']['code'], 'ssh_cleanup_unconfirmed')
                self.assertEqual(result['episode']['state'], 'failed')
                key = spawned[0]
                self.assertEqual(self.store.get('connection_children', key)['closed'], not close_fails)
                self.assertEqual(('bravo', key) in self.manager.owners, close_fails)
                if close_fails:
                    self.assertIn(key, result['cleanup_pending'])
                    owner.fails = False
                    await self.manager._close_owner('bravo', key)

    async def test_local_overlapping_close_does_not_orphan_new_candidate(self):
        entered, released = threading.Event(), threading.Event()
        class DelayedOwner(Owner):
            def close(self):
                self.calls += 1; entered.set()
                if not released.wait(5): raise TimeoutError('Fixture did not release prior close')
                self.process.returncode = 0
        prior, prior_owner, _ = self.add(DelayedOwner())
        current = Owner(fails=False)
        spawned, background = [], []
        async def healthy(peer, decision): return False
        async def opened(peer, candidate):
            key = candidate['attempt_id']; spawned.append(key)
            self.manager.owners[(peer, key)] = (current, Errors())
            self.store.put('connection_children', key,
                {'peer_id': peer, 'attempt_id': key, 'closed': False})
            background.append(asyncio.create_task(self.manager._close_owner(peer, prior)))
            if not await asyncio.to_thread(entered.wait, 2): raise AssertionError('Prior close did not begin')
        self.manager._healthy, self.manager._open_candidate = healthy, opened
        try:
            # Actual _select/_receive_propose rejects the overlapping closure.
            result = await self.manager.ensure('bravo', str(uuid.uuid4()))
            self.assertEqual(result['error']['code'], 'ssh_cleanup_unconfirmed')
            self.assertEqual(current.calls, 1)
            released.set(); await asyncio.gather(*background)
            self.assertEqual(self.manager.status('bravo')['cleanup_pending'], [])
            self.assertFalse(self.manager.owners)
            self.assertTrue(self.store.get('connection_children', spawned[0])['closed'])
            self.assertIsNone(self.manager._state('bravo').get('decision'))
        finally:
            released.set()
            if background: await asyncio.gather(*background, return_exceptions=True)

    async def test_cleanup_refusal_preserves_current_durable_prepared_candidate(self):
        owner = Owner(fails=False)
        spawned = []
        async def healthy(peer, decision): return False
        async def opened(peer, candidate):
            key = candidate['attempt_id']; spawned.append(key)
            self.manager.owners[(peer, key)] = (owner, Errors())
            self.store.put('connection_children', key,
                {'peer_id': peer, 'attempt_id': key, 'closed': False})
        async def prepared(peer, candidate):
            state = self.manager._state(peer)
            state.update(state='prepared', generation=1, decision={'state': 'prepared',
                'dispatch_ready': False, 'connection_id': candidate['attempt_id'],
                'generation': 1, 'candidate': candidate})
            self.manager._save(state)
            raise BridgeError('ssh_cleanup_unconfirmed', 'Remote cleanup must finish', True)
        self.manager._healthy, self.manager._open_candidate, self.manager._select = healthy, opened, prepared
        result = await self.manager.ensure('bravo', str(uuid.uuid4()))
        self.assertEqual(result['decision']['state'], 'prepared')
        self.assertEqual(owner.calls, 0)
        self.assertIn(('bravo', spawned[0]), self.manager.owners)

    async def test_restarted_manager_does_not_claim_unknown_cleanup_succeeded(self):
        attempt, owner, _ = self.add()
        await self.fails_close(attempt)
        restarted = ConnectionManager(self.bridge)
        with self.assertRaises(BridgeError) as caught:
            await restarted.disconnect('bravo', str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, 'ssh_cleanup_unconfirmed')
        self.assertFalse(self.store.get('connection_children', attempt)['closed'])
        self.assertEqual(owner.calls, 1)
        await restarted.close()

    async def test_cancelled_caller_does_not_release_ownership_or_double_close(self):
        entered, released = threading.Event(), threading.Event()
        class DelayedOwner(Owner):
            def close(self):
                self.calls += 1
                entered.set()
                if not released.wait(5): raise TimeoutError('Fixture did not release close')
                self.process.returncode = 0
        attempt, owner, _ = self.add(DelayedOwner())
        first = asyncio.create_task(self.manager._close_owner('bravo', attempt))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError): await first
            self.assertIn(('bravo', attempt), self.manager.owners)
            with self.assertRaises(BridgeError) as caught:
                await self.manager.ensure('bravo', str(uuid.uuid4()), retry=True)
            self.assertEqual(caught.exception.code, 'ssh_cleanup_unconfirmed')
            second = asyncio.create_task(self.manager._close_owner('bravo', attempt))
            await asyncio.sleep(.02)
            self.assertEqual(owner.calls, 1)
            released.set()
            await second
            self.assertEqual(owner.calls, 1)
            self.assertTrue(self.store.get('connection_children', attempt)['closed'])
        finally:
            released.set()

    async def test_failed_journal_write_retains_exact_owner_until_retried(self):
        attempt, owner, _ = self.add(Owner(fails=False))
        original = self.store.put
        failed = False
        def put(kind, key, value):
            nonlocal failed
            if kind == 'connection_children' and value.get('closed') and not failed:
                failed = True
                raise OSError('Fixture journal sharing conflict')
            return original(kind, key, value)
        with patch.object(self.store, 'put', side_effect=put):
            await self.fails_close(attempt)
            self.assertIn(('bravo', attempt), self.manager.owners)
            self.assertFalse(self.store.get('connection_children', attempt)['closed'])
            await self.manager._close_owner('bravo', attempt)
        self.assertTrue(self.store.get('connection_children', attempt)['closed'])

    async def test_manager_shutdown_attempts_other_peer_children_before_reporting_failure(self):
        failed, failed_owner, _ = self.add()
        other = str(uuid.uuid4()); other_owner = Owner(fails=False)
        self.manager.owners[('charlie', other)] = (other_owner, Errors())
        self.manager.local_candidates['charlie'] = {other}
        self.store.put('connection_children', other,
            {'peer_id': 'charlie', 'attempt_id': other, 'closed': False})
        with self.assertRaises(BridgeError) as caught: await self.manager.close()
        self.assertEqual(caught.exception.code, 'ssh_cleanup_unconfirmed')
        self.assertEqual(failed_owner.calls, 1)
        self.assertEqual(other_owner.calls, 1)
        self.assertFalse(self.store.get('connection_children', failed)['closed'])
        self.assertTrue(self.store.get('connection_children', other)['closed'])
        self.assertIn(('bravo', failed), self.manager.owners)
        self.assertNotIn(('charlie', other), self.manager.owners)

    async def test_manager_shutdown_joins_already_running_exact_owner_close(self):
        entered, released = threading.Event(), threading.Event()
        class DelayedOwner(Owner):
            def close(self):
                self.calls += 1; entered.set()
                if not released.wait(5): raise TimeoutError('Fixture close did not release')
                self.process.returncode = 0
        attempt, owner, _ = self.add(DelayedOwner())
        active = asyncio.create_task(self.manager._close_owner('bravo', attempt))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            closing = asyncio.create_task(self.manager.close())
            await asyncio.sleep(.02)
            self.assertEqual(owner.calls, 1)
            released.set()
            await asyncio.gather(active, closing)
            self.assertEqual(owner.calls, 1)
            self.assertFalse(self.manager.owners)
        finally:
            released.set()


if __name__ == '__main__': unittest.main()
