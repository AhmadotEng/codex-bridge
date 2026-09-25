import asyncio
import copy
from pathlib import Path
import tempfile
import time
import types
import unittest
import uuid

from codex_bridge.core import BridgeError, Store
from codex_bridge.connections import ConnectionManager, OWNER_CAPABILITY, validate_connections


def request():
    return str(uuid.uuid4())


class Pair:
    """Two real managers/stores, mock only the SSH carrier and HTTP transport."""
    def __init__(self, root):
        self.nodes = {}; self.routes = {}; self.opens = []; self.failure = None
        self.after_rpc = None; self.before_rpc = None
        lanes = {'alpha': {'alpha': 48001, 'bravo': 48002},
                 'bravo': {'alpha': 48003, 'bravo': 48004}}
        for own, peer in (('alpha', 'bravo'), ('bravo', 'alpha')):
            node = types.SimpleNamespace(node_id=own, busy=False)
            node.store = Store(root / (own + '.sqlite'))
            node.cfg = {'peer_id': own, 'listen_port': 47001, 'state_dir': str(root / own),
                'peers': {peer: {'enabled': True, 'incoming_token': peer + '-secret',
                                'outgoing_token': own + '-secret', 'url': 'http://127.0.0.1:48001'}},
                'connections': {peer: {'lanes': copy.deepcopy(lanes), 'initial_attempts': 1,
                    'initial_seconds': 10, 'recovery_attempts': 1, 'recovery_seconds': 10, 'idle_seconds': 1}}}
            node.config = lambda node=node: node.cfg
            def lookup(peer, node=node):
                item = node.cfg['peers'][peer]
                if not item['enabled']: raise BridgeError('access_revoked', 'Revoked')
                return item
            node.peer = lookup
            node.connection_activity = lambda peer, node=node: node.busy
            async def direct(peer, method, p, url, connection=None, node=node):
                port = int(url.rsplit(':', 1)[1])
                route = next((origin for origin, ports in lanes.items() if ports[node.node_id] == port), None)
                if route not in self.routes: raise BridgeError('peer_unavailable', 'Mock carrier unavailable', True)
                if self.before_rpc: await self.before_rpc(node, method, p)
                result = await self.nodes[peer].manager.peer_rpc(method, p, node.node_id)
                if self.after_rpc: result = await self.after_rpc(node, method, p, result)
                return result
            node.remote_direct = direct
            node.manager = ConnectionManager(node)
            async def opened(peer, candidate, node=node):
                self.opens.append((node.node_id, candidate['attempt_id']))
                charged = node.manager._state(peer)['episode']['attempts']
                if charged < 1: raise AssertionError('SSH was spawned before durable charge')
                if self.failure: raise self.failure
                self.routes[node.node_id] = candidate['attempt_id']
                # Retained ownership is distinct from a reachable origin lane:
                # reopening that lane must not revive the previous child.
                attempt = candidate['attempt_id']
                process = types.SimpleNamespace(poll=lambda: None if self.routes.get(node.node_id) == attempt else 255)
                def release():
                    if self.routes.get(node.node_id) == attempt: self.routes.pop(node.node_id)
                node.manager.owners[(peer, attempt)] = (types.SimpleNamespace(process=process, close=release), types.SimpleNamespace(finish=lambda: None))
                node.store.put('connection_children', attempt, {'peer_id': peer, 'attempt_id': attempt, 'closed': False})
                await node.manager._verify(peer, candidate)
            async def closed(peer, attempt, node=node):
                if self.routes.get(node.node_id) == attempt:
                    self.routes.pop(node.node_id, None)
                node.manager.owners.pop((peer, attempt), None)
                child = node.store.get('connection_children', attempt)
                if child:
                    child['closed'] = True; node.store.put('connection_children', attempt, child)
            async def losers(peer, winner, node=node):
                for owner_peer, attempt in list(node.manager.owners):
                    if owner_peer == peer and attempt != winner: await closed(peer, attempt, node=node)
            node.manager._open_candidate = opened
            node.manager._close_owner = closed
            node.manager._close_losers = losers
            self.nodes[own] = node

    @property
    def a(self): return self.nodes['alpha'].manager
    @property
    def b(self): return self.nodes['bravo'].manager

    async def close(self):
        await self.a.close(); await self.b.close()
        for node in self.nodes.values(): node.store.db.close()


class ConnectionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.p = Pair(Path(self.temp.name))

    async def asyncTearDown(self):
        # Allow acknowledged-cleanup callbacks to finish before closing stores.
        await asyncio.sleep(.55)
        await self.p.close(); self.temp.cleanup()

    async def test_waiting_startup_and_status_never_dial_even_pending(self):
        self.p.nodes['alpha'].busy = True
        for _ in range(4):
            await self.p.a.maintenance()
            self.assertEqual(self.p.a.status('bravo')['message'], 'Not connected; remote Bridge not checked.')
        self.assertEqual(self.p.opens, [])

    async def test_both_origins_select_and_allow_both_directions(self):
        result = await self.p.b.ensure('alpha', request())
        self.assertTrue(result['available'], result)
        self.assertEqual(result['decision']['candidate']['initiator'], 'bravo')
        for manager, peer, receiver in ((self.p.a, 'bravo', self.p.b), (self.p.b, 'alpha', self.p.a)):
            async with manager.route(peer) as selected:
                async with receiver.accept(manager.bridge.node_id, selected['connection']): pass
        self.assertEqual(len(self.p.opens), 1)

    async def test_reuse_committed_route_and_repeated_id_do_not_spawn(self):
        rid = request(); await self.p.a.ensure('bravo', rid)
        await self.p.a.ensure('bravo', rid)
        await self.p.b.ensure('alpha', request())
        self.assertEqual(len(self.p.opens), 1)

    async def same_lane_recovery(self, origin, tick=False):
        peer = 'bravo' if origin == 'alpha' else 'alpha'
        manager = self.p.nodes[origin].manager
        initial = await manager.ensure(peer, request())
        old_id = initial['decision']['connection_id']
        self.p.routes.clear()  # Physical exit; retain its exact ownership entry.
        if tick:
            original_select = manager._select
            async def concurrent_tick(peer, candidate):
                result = await original_select(peer, candidate)
                manager.last_probe.clear(); manager.last_activity[peer] = time.monotonic()
                await manager.maintenance()
                return result
            manager._select = concurrent_tick
        sentinel = {'saved': 'unrelated peer/session content'}
        manager.bridge.store.put('sentinel', 'unrelated', sentinel)
        rid = request(); recovered = await manager.ensure(peer, rid)
        self.assertTrue(recovered['available'], recovered)
        self.assertEqual(recovered['generation'], initial['generation'] + 1)
        new_id = recovered['decision']['connection_id']
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self.p.routes[origin], new_id)
        self.assertEqual(recovered['episode']['attempts'], 1)
        self.assertEqual(recovered['episode']['kind'], 'recovery')
        self.assertTrue(manager.bridge.store.get('connection_requests', peer + ':' + rid)['finished'])
        self.assertTrue(manager.bridge.store.get('connection_children', old_id)['closed'])
        self.assertFalse(manager.bridge.store.get('connection_children', new_id)['closed'])
        self.assertEqual(manager.bridge.store.get('sentinel', 'unrelated'), sentinel)
        self.assertTrue((await manager.ensure(peer, rid))['deduplicated'])
        self.assertEqual(len(self.p.opens), 2)

    async def test_same_origin_recovery_never_reuses_dead_prior_candidate_alpha(self):
        await self.same_lane_recovery('alpha')

    async def test_same_origin_recovery_never_reuses_dead_prior_candidate_bravo(self):
        await self.same_lane_recovery('bravo')

    async def test_maintenance_during_recovery_selection_cannot_erase_episode(self):
        await self.same_lane_recovery('alpha', tick=True)

    async def test_opposite_origin_replaces_dead_carrier_both_orders(self):
        for old_origin, new_origin in (('bravo', 'alpha'), ('alpha', 'bravo')):
            with self.subTest(old_origin=old_origin):
                old_peer = 'bravo' if old_origin == 'alpha' else 'alpha'
                before = await self.p.nodes[old_origin].manager.ensure(old_peer, request(), retry=True)
                self.p.routes.clear()
                result = await self.p.nodes[new_origin].manager.ensure(old_origin, request(), retry=True)
                self.assertTrue(result['available'], result)
                self.assertEqual(result['generation'], before['generation'] + 1)
                self.assertEqual(result['decision']['candidate']['initiator'], new_origin)
                await asyncio.sleep(.55)
                self.assertEqual(self.p.routes, {new_origin: result['decision']['connection_id']})

    async def test_origin_restart_cannot_adopt_unowned_journal_pid_or_listener(self):
        result = await self.p.a.ensure('bravo', request())
        node = self.p.nodes['alpha']; old = node.manager
        # Keep the mock physical lane present, like an orphaned process, while
        # the restarted origin has no retained child handle. No PID adoption.
        node.manager = restored = ConnectionManager(node)
        self.assertFalse(await restored._healthy('bravo', result['decision']))
        self.assertFalse(await self.p.b._healthy('alpha', self.p.b._state('alpha')['decision']))
        self.assertFalse(restored.status('bravo')['available'])
        self.assertEqual(self.p.routes['alpha'], result['decision']['connection_id'])
        self.assertIsNotNone(node.store.get('connection_children', result['decision']['connection_id']))
        await old.close()

    async def test_missing_signed_owner_capability_fails_closed(self):
        result = await self.p.a.ensure('bravo', request())
        original = self.p.a._receive_owner
        async def old_reply(peer, params):
            value = await original(peer, params); value.pop('ownership_proof'); return value
        self.p.a._receive_owner = old_reply
        with self.assertRaises(BridgeError) as error:
            await self.p.b._require_carrier('alpha', result['decision']['candidate'])
        self.assertEqual(error.exception.code, 'capability_missing')
        self.assertFalse(await self.p.b._healthy('alpha', result['decision']))

    async def test_old_receiver_probe_without_owner_capability_is_rejected(self):
        original = self.p.b._receive_probe
        async def old_probe(peer, params):
            value = await original(peer, params); value.pop('ownership_proof'); return value
        self.p.b._receive_probe = old_probe
        result = await self.p.a.ensure('bravo', request())
        self.assertFalse(result['available'])
        self.assertEqual(result['error']['code'], 'capability_missing')
        self.assertEqual(self.p.routes, {})

    async def test_stopped_signed_decision_exposes_owner_protocol_without_dial(self):
        state = self.p.a._state('bravo'); state['stop_requested'] = True; self.p.a._save(state)
        result = await self.p.a.peer_rpc('peer.connection_decision', {'nonce': 'a' * 64, 'payload': {}}, 'bravo')
        self.assertTrue(result['result']['stop_requested'])
        self.assertEqual(result['result']['ownership_proof'], OWNER_CAPABILITY)
        self.assertIn('version', result['result']); self.assertEqual(self.p.opens, [])

    async def test_restarted_nonorigin_coordinator_finishes_interrupted_final_commit(self):
        result = await self.p.b.ensure('alpha', request())
        # Durable coordinator commit survived; the final commit was not yet
        # installed by the originating follower. Its exact carrier is alive.
        follower = self.p.b._state('alpha'); follower['decision'].update(state='prepared', dispatch_ready=False)
        self.p.b._save(follower)
        node = self.p.nodes['alpha']; old = node.manager
        await old.close(); node.manager = restored = ConnectionManager(node)
        recovered = await restored.ensure('bravo', request())
        self.assertTrue(recovered['available'], recovered)
        self.assertEqual(recovered['generation'], result['generation'])
        self.assertEqual(len(self.p.opens), 1)
        self.assertTrue(self.p.b.status('alpha')['available'])

    async def restored_idle_interval(self, interrupted):
        result = await self.p.b.ensure('alpha', request())
        if interrupted:
            follower = self.p.b._state('alpha')
            follower['decision'].update(state='prepared', dispatch_ready=False)
            self.p.b._save(follower)
        node = self.p.nodes['alpha']; old = node.manager
        await old.close(); node.manager = restored = ConnectionManager(node)
        recovered = await restored.ensure('bravo', request())
        self.assertTrue(recovered['available'], recovered)
        self.assertEqual(recovered['generation'], result['generation'])
        self.assertIn('bravo', restored.last_activity)
        initial_clock = restored.last_activity['bravo']
        self.assertTrue(await restored._healthy('bravo', recovered['decision']))
        self.assertTrue((await restored.ensure('bravo', request()))['available'])
        self.assertEqual(restored.last_activity['bravo'], initial_clock)
        restored.last_activity['bravo'] = time.monotonic()-10
        restored.last_probe.clear()
        await restored.maintenance()
        self.assertFalse(restored.status('bravo')['available'])
        self.assertFalse(self.p.b.status('alpha')['available'])
        self.assertEqual(len(self.p.opens), 1)

    async def test_restored_retained_route_initializes_idle_clock_once(self):
        await self.restored_idle_interval(interrupted=False)

    async def test_reconciled_final_commit_initializes_idle_clock_once(self):
        await self.restored_idle_interval(interrupted=True)

    async def lost_reconciliation(self, action):
        await self.p.b.ensure('alpha', request())
        follower = self.p.b._state('alpha'); follower['decision'].update(state='prepared', dispatch_ready=False)
        self.p.b._save(follower)
        state = self.p.a._state('bravo'); state['decision']['dispatch_ready'] = False; self.p.a._save(state)
        async def lost_commit(node, method, params):
            if node.node_id == 'alpha' and method.endswith('_' + action):
                raise BridgeError('peer_unavailable', 'Lost reconciliation control', True)
        self.p.before_rpc = lost_commit
        rid = request(); result = await self.p.a.ensure('bravo', rid)
        self.assertFalse(result['available'], result)
        self.assertEqual(result['episode']['state'], 'exhausted')
        self.assertEqual(result['episode']['attempts'], 1)
        self.assertTrue(self.p.a.bridge.store.get('connection_requests', 'bravo:' + rid)['finished'])
        self.assertEqual(len(self.p.opens), 1)
        repeated = await self.p.a.ensure('bravo', rid)
        self.assertTrue(repeated['deduplicated'])
        self.assertEqual(repeated['episode']['attempts'], 1)

    async def test_lost_reconciliation_commit_returns_bounded_finished_request(self):
        await self.lost_reconciliation('commit')

    async def test_lost_reconciliation_probe_returns_bounded_finished_request(self):
        await self.lost_reconciliation('probe')

    async def test_dead_draining_owner_never_becomes_dispatch_ready_during_replacement(self):
        result = await self.p.b.ensure('alpha', request())
        old_id = result['decision']['connection_id']
        for manager, peer in ((self.p.a, 'bravo'), (self.p.b, 'alpha')):
            state = manager._state(peer); state['decision'].update(state='draining', dispatch_ready=False)
            manager._save(state)
        self.p.routes.clear()
        observations = []
        original = self.p.b._receive_undrain
        async def observe(peer, params):
            answer = await original(peer, params)
            observations.append(self.p.b.status(peer)['decision'].copy())
            return answer
        self.p.b._receive_undrain = observe
        new = await self.p.a.ensure('bravo', request(), retry=True)
        self.assertTrue(new['available'], new)
        self.assertEqual(new['generation'], result['generation'] + 1)
        self.assertTrue(observations)
        self.assertTrue(all(x['connection_id'] == old_id and not x['dispatch_ready'] for x in observations))

    async def test_awaited_old_health_failure_does_not_fence_replacement(self):
        before = await self.p.a.ensure('bravo', request())
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.p.a._healthy
        async def delayed(peer, decision):
            entered.set(); await release.wait(); return False
        self.p.a._healthy = delayed; self.p.a.last_probe.clear()
        tick = asyncio.create_task(self.p.a.maintenance())
        await entered.wait()
        self.p.a._healthy = original; self.p.routes.clear()
        new = await self.p.b.ensure('alpha', request(), retry=True)
        self.assertEqual(new['generation'], before['generation'] + 1)
        release.set(); await tick
        self.assertTrue(self.p.a.status('bravo')['available'])
        self.assertEqual(self.p.a.status('bravo')['decision']['connection_id'], new['decision']['connection_id'])

    async def test_failed_old_owner_cleanup_retains_new_winner_and_budget(self):
        initial = await self.p.a.ensure('bravo', request())
        old_id = initial['decision']['connection_id']; owner = self.p.a.owners[('bravo', old_id)][0]
        normal_close = owner.close
        def failed_close(): raise OSError('Fixture owned-handle cleanup is temporarily unavailable')
        owner.close = failed_close
        self.p.a._close_owner = types.MethodType(ConnectionManager._close_owner, self.p.a)
        self.p.a._close_losers = types.MethodType(ConnectionManager._close_losers, self.p.a)
        self.p.routes.clear()
        rid = request(); result = await self.p.a.ensure('bravo', rid)
        self.assertEqual(result['error']['code'], 'ssh_cleanup_unconfirmed')
        self.assertFalse(result['available'])
        self.assertEqual(result['generation'], 2)
        new_id = result['decision']['connection_id']
        self.assertEqual(self.p.routes['alpha'], new_id)
        self.assertEqual(result['cleanup_pending'], [old_id])
        self.assertIn(('bravo', old_id), self.p.a.owners)
        self.assertIn(('bravo', new_id), self.p.a.owners)
        self.assertEqual(result['episode']['attempts'], 1)
        await self.p.a.maintenance()
        with self.assertRaises(BridgeError): await self.p.a.ensure('bravo', request())
        self.assertEqual(len(self.p.opens), 2)
        owner.close = normal_close
        await self.p.a.disconnect('bravo', request())
        self.assertTrue(self.p.a.bridge.store.get('connection_children', old_id)['closed'])
        self.assertTrue(self.p.a.bridge.store.get('connection_children', new_id)['closed'])

    async def test_simultaneous_selection_one_winner(self):
        a, b = await asyncio.gather(self.p.a.ensure('bravo', request()), self.p.b.ensure('alpha', request()))
        self.assertTrue(a['available'], a); self.assertTrue(b['available'], b)
        self.assertEqual(a['decision']['connection_id'], b['decision']['connection_id'])
        self.assertEqual(a['decision']['candidate']['initiator'], 'alpha')
        self.assertEqual(set(self.p.routes), {'alpha'})

    async def test_lost_commit_ack_reconciles_without_second_generation(self):
        dropped = False
        async def after(node, method, params, result):
            nonlocal dropped
            if method.endswith('_commit') and not dropped:
                dropped = True
                raise BridgeError('peer_unavailable', 'Lost ACK', True)
            return result
        self.p.after_rpc = after
        result = await self.p.a.ensure('bravo', request())
        self.assertTrue(result['available'], result)
        self.assertEqual(result['generation'], 1)
        self.assertEqual(len(self.p.opens), 1)

    async def test_offline_budget_not_replenished_by_new_requests_or_restart(self):
        self.p.failure = BridgeError('ssh_endpoint_unreachable', 'Offline', True)
        first = await self.p.a.ensure('bravo', request())
        self.assertEqual(first['episode']['state'], 'exhausted')
        for _ in range(3):
            await self.p.a.ensure('bravo', request()); await self.p.a.maintenance()
        self.assertEqual(len(self.p.opens), 1)
        restarted = ConnectionManager(self.p.nodes['alpha'])
        await restarted.ensure('bravo', request())
        self.assertEqual(len(self.p.opens), 1)
        await self.p.a.ensure('bravo', request(), retry=True)
        self.assertEqual(len(self.p.opens), 2)

    async def test_disconnect_rejects_incoming_and_preserves_work(self):
        await self.p.a.ensure('bravo', request())
        self.p.nodes['alpha'].store.put('sentinel', 'saved-task', {'value': 42})
        await self.p.a.disconnect('bravo', request())
        result = await self.p.b.ensure('alpha', request())
        self.assertFalse(result['available'])
        self.assertEqual(result['error']['code'], 'peer_stopped')
        self.assertEqual(self.p.nodes['alpha'].store.get('sentinel', 'saved-task'), {'value': 42})
        resumed = await self.p.a.ensure('bravo', request(), retry=True)
        self.assertTrue(resumed['available'], resumed)

    async def test_passive_accept_after_local_budget_exhausted(self):
        self.p.failure = BridgeError('ssh_endpoint_unreachable', 'Offline', True)
        await self.p.a.ensure('bravo', request())
        self.p.failure = None
        result = await self.p.b.ensure('alpha', request())
        self.assertTrue(result['available'], result)
        self.assertTrue(self.p.a.status('bravo')['available'])

    async def test_uncommitted_and_stale_generation_fenced(self):
        with self.assertRaisesRegex(BridgeError, 'committed'):
            async with self.p.b.accept('alpha', {'generation': 1, 'connection_id': request()}): pass
        await self.p.a.ensure('bravo', request())
        async with self.p.a.route('bravo') as route:
            bad = {**route['connection'], 'generation': 0}
            with self.assertRaises(BridgeError):
                async with self.p.b.accept('alpha', bad): pass

    async def test_tampered_control_response_rejected(self):
        async def after(node, method, params, result):
            return {**result, 'signature': '0' * 64}
        self.p.after_rpc = after
        result = await self.p.a.ensure('bravo', request())
        self.assertFalse(result['available'])
        self.assertEqual(result['error']['code'], 'unauthorized')

    async def test_mapping_mismatch_rejected_without_project_dispatch(self):
        self.p.nodes['bravo'].cfg['connections']['alpha']['lanes']['alpha']['bravo'] += 20
        result = await self.p.a.ensure('bravo', request())
        self.assertEqual(result['error']['code'], 'protocol_error')

    async def test_idle_work_on_other_endpoint_prevents_closure(self):
        await self.p.a.ensure('bravo', request())
        self.p.a.last_activity['bravo'] = time.monotonic()-10
        self.p.nodes['bravo'].busy = True
        await self.p.a.maintenance()
        self.assertTrue(self.p.a.status('bravo')['available'])
        self.assertTrue(self.p.b.status('alpha')['available'])

    async def test_idle_closure_and_new_demand_reconnect(self):
        await self.p.a.ensure('bravo', request())
        self.p.a.last_activity['bravo'] = time.monotonic()-10
        await self.p.a.maintenance()
        self.assertEqual(self.p.a.status('bravo')['state'], 'idle')
        result = await self.p.b.ensure('alpha', request())
        self.assertTrue(result['available'], result)
        self.assertEqual(result['generation'], 2)

    async def idle_reopen_keeps_new_interval(self, origin):
        await self.p.a.ensure('bravo', request())
        for manager, peer in ((self.p.a, 'bravo'), (self.p.b, 'alpha')):
            manager.bridge.cfg['connections'][peer]['idle_seconds'] = 60
            manager.last_activity[peer] = time.monotonic()-120
        await self.p.a.maintenance()
        self.assertEqual(self.p.a.status('bravo')['state'], 'idle')
        self.assertEqual(self.p.b.status('alpha')['state'], 'idle')
        peer = 'bravo' if origin == 'alpha' else 'alpha'
        began = time.monotonic()
        result = await self.p.nodes[origin].manager.ensure(peer, request())
        self.assertTrue(result['available'], result)
        self.assertEqual(result['generation'], 2)
        for manager, other in ((self.p.a, 'bravo'), (self.p.b, 'alpha')):
            manager.last_probe.clear()
            await manager.maintenance()
            self.assertTrue(manager.status(other)['available'])
            self.assertGreaterEqual(manager.last_activity[other], began)
        self.assertEqual(len(self.p.opens), 2)

    async def test_idle_reopen_alpha_gets_new_interval_on_both_endpoints(self):
        await self.idle_reopen_keeps_new_interval('alpha')

    async def test_idle_reopen_bravo_gets_new_interval_on_both_endpoints(self):
        await self.idle_reopen_keeps_new_interval('bravo')

    async def test_healthy_reuse_and_commit_replay_do_not_extend_idle_interval(self):
        result = await self.p.a.ensure('bravo', request())
        expired = time.monotonic()-120
        for manager, peer in ((self.p.a, 'bravo'), (self.p.b, 'alpha')):
            manager.last_activity[peer] = expired
        self.assertTrue((await self.p.a.ensure('bravo', request()))['available'])
        self.assertTrue((await self.p.b.ensure('alpha', request()))['available'])
        await self.p.a._rpc('bravo', 'commit', {'decision': result['decision']}, result['decision']['candidate'])
        self.assertEqual(self.p.a.last_activity['bravo'], expired)
        self.assertEqual(self.p.b.last_activity['alpha'], expired)
        await self.p.a.maintenance()
        self.assertFalse(self.p.a.status('bravo')['available'])
        self.assertEqual(len(self.p.opens), 1)

    async def test_peer_revocation_during_handshake(self):
        async def before(node, method, params):
            if method.endswith('_prepare'):
                self.p.nodes['bravo'].cfg['peers']['alpha']['enabled'] = False
        self.p.before_rpc = before
        result = await self.p.a.ensure('bravo', request())
        self.assertEqual(result['error']['code'], 'access_revoked')

    async def test_one_direction_callback_failure_never_selects(self):
        async def before(node, method, params):
            if method.endswith('_callback'): raise BridgeError('peer_unavailable', 'Reverse port unavailable', True)
        self.p.before_rpc = before
        result = await self.p.a.ensure('bravo', request())
        self.assertFalse(result['available'])
        self.assertIsNone(result.get('decision'))

    async def test_old_stop_marker_requires_explicit_retry(self):
        stop = Path(self.p.nodes['alpha'].cfg['state_dir']) / 'transports/bravo/stop'
        stop.parent.mkdir(parents=True); stop.touch()
        result = await self.p.a.ensure('bravo', request())
        self.assertTrue(result['stop_requested']); self.assertEqual(self.p.opens, [])
        result = await self.p.a.ensure('bravo', request(), retry=True)
        self.assertTrue(result['available'], result); self.assertFalse(stop.exists())

    async def test_lane_validation_rejects_duplicate_ports(self):
        cfg = copy.deepcopy(self.p.nodes['alpha'].cfg)
        cfg['connections']['bravo']['lanes']['bravo']['alpha'] = 48001
        with self.assertRaises(BridgeError): validate_connections(cfg)

    async def test_disconnect_refuses_busy_local_and_remote_without_stop(self):
        await self.p.a.ensure('bravo', request())
        for node in ('alpha', 'bravo'):
            self.p.nodes[node].busy = True
            with self.assertRaises(BridgeError) as error:
                await self.p.a.disconnect('bravo', request())
            self.assertEqual(error.exception.code, 'connection_busy')
            self.assertFalse(self.p.a._state('bravo')['stop_requested'])
            self.assertTrue(self.p.a.status('bravo')['available'])
            self.p.nodes[node].busy = False

    async def test_opposite_reconnect_delayed_stop_cannot_close_new_generation(self):
        old = await self.p.a.ensure('bravo', request())
        self.p.routes.clear()
        new = await self.p.b.ensure('alpha', request(), retry=True)
        self.assertTrue(new['available'], new)
        self.assertEqual(new['generation'], 2)
        with self.assertRaises(BridgeError) as error:
            await self.p.b._receive_disconnect('alpha', {
                'generation': old['generation'], 'connection_id': old['decision']['connection_id']})
        self.assertEqual(error.exception.code, 'stale_generation')
        await asyncio.sleep(.6)
        self.assertEqual(set(self.p.routes), {'bravo'})
        self.assertTrue(self.p.b.status('alpha')['available'])

    async def test_inflight_route_blocks_idle_drain(self):
        await self.p.a.ensure('bravo', request())
        self.p.a.last_activity['bravo'] = time.monotonic()-10
        async with self.p.a.route('bravo', activity=False):
            await self.p.a.maintenance()
        self.assertTrue(self.p.a.status('bravo')['available'])

    async def test_new_local_work_during_idle_drain_rolls_back_both_endpoints(self):
        await self.p.a.ensure('bravo', request())
        self.p.a.last_activity['bravo'] = time.monotonic()-10
        async def after(node, method, params, result):
            if method.endswith('_drain'): self.p.nodes['alpha'].busy = True
            return result
        self.p.after_rpc = after
        await self.p.a.maintenance()
        self.assertTrue(self.p.a.status('bravo')['available'])
        self.assertTrue(self.p.b.status('alpha')['available'])

    async def test_new_remote_work_before_idle_close_rolls_back_both_endpoints(self):
        await self.p.a.ensure('bravo', request())
        self.p.a.last_activity['bravo'] = time.monotonic()-10
        async def before(node, method, params):
            if method.endswith('_close'): self.p.nodes['bravo'].busy = True
        self.p.before_rpc = before
        await self.p.a.maintenance()
        self.assertTrue(self.p.a.status('bravo')['available'])
        self.assertTrue(self.p.b.status('alpha')['available'])

    async def test_brief_recovery_does_not_reset_episode_budget(self):
        await self.p.a.ensure('bravo', request())
        self.p.nodes['alpha'].busy = True
        self.p.routes.clear(); self.p.a.last_probe.clear()
        await self.p.a.maintenance()
        await self.p.a.demands['bravo']
        self.assertTrue(self.p.a.status('bravo')['available'])
        self.assertEqual(len(self.p.opens), 2)
        self.p.routes.clear(); self.p.a.last_probe.clear()
        await self.p.a.maintenance()
        await self.p.a.demands['bravo']
        self.assertEqual(len(self.p.opens), 2)
        self.assertEqual(self.p.a.status('bravo')['episode']['state'], 'exhausted')

    async def test_process_restart_suspends_interrupted_charge_without_extra_launch(self):
        state = self.p.a._state('bravo')
        state['episode'] = {'state': 'attempting', 'attempts': 1, 'kind': 'initial', 'request_id': request()}
        self.p.a._save(state)
        restored = ConnectionManager(self.p.nodes['alpha'])
        result = await restored.ensure('bravo', request())
        self.assertEqual(result['episode']['state'], 'suspended')
        self.assertEqual(self.p.opens, [])

    async def test_replayed_retry_after_crash_does_not_refill_budget(self):
        rid = request(); state = self.p.a._state('bravo')
        state['episode'] = {'state': 'attempting', 'attempts': 1, 'kind': 'initial', 'request_id': rid}
        self.p.a._save(state)
        self.p.nodes['alpha'].store.put('connection_requests', 'bravo:' + rid,
            {'request_id': rid, 'peer_id': 'bravo', 'finished': False})
        restored = ConnectionManager(self.p.nodes['alpha'])
        result = await restored.ensure('bravo', rid, retry=True)
        self.assertTrue(result['deduplicated'])
        self.assertEqual(result['episode']['attempts'], 1)
        self.assertEqual(result['episode']['state'], 'suspended')
        self.assertEqual(self.p.opens, [])

    async def test_changed_lane_map_never_reports_old_connection_available(self):
        await self.p.a.ensure('bravo', request())
        self.p.nodes['alpha'].cfg['connections']['bravo']['lanes']['alpha']['alpha'] += 10
        self.assertFalse(self.p.a.status('bravo')['available'])
        with self.assertRaises(BridgeError):
            async with self.p.a.route('bravo'): pass
        self.assertEqual(self.p.a.inflight.get('bravo', 0), 0)

    async def test_receiver_restart_restores_only_after_mutual_verification(self):
        await self.p.a.ensure('bravo', request())
        node = self.p.nodes['bravo']; old = node.manager
        await old.close()
        node.manager = restored = ConnectionManager(node)
        restored._open_candidate = old._open_candidate
        restored._close_owner = old._close_owner
        restored._close_losers = old._close_losers
        self.assertFalse(restored.status('alpha')['available'])
        result = await self.p.a.ensure('bravo', request())
        self.assertTrue(result['available'], result)
        self.assertTrue(restored.status('alpha')['available'])
        self.assertEqual(len(self.p.opens), 1)

    async def test_revocation_closes_only_owned_route(self):
        await self.p.a.ensure('bravo', request())
        self.p.nodes['alpha'].cfg['peers']['bravo']['enabled'] = False
        await self.p.a.maintenance()
        self.assertEqual(self.p.routes, {})
        self.assertEqual(self.p.a.status('bravo')['state'], 'revoked')

    async def test_remote_codex_stage_separate_and_handles_real_capabilities(self):
        self.p.a.record_codex('bravo', {'codex': {'adapter': 'codex-app-server-stdio', 'ready': True}})
        self.assertEqual(self.p.a.status('bravo')['stages']['remote_codex']['state'], 'pass')
        self.assertFalse(self.p.a.status('bravo')['available'])
        self.p.a.record_codex('bravo', error={'code': 'login_required'})
        self.assertEqual(self.p.a.status('bravo')['stages']['remote_codex']['code'], 'login_required')

    async def test_heartbeat_only_health_does_not_refill_recovery_budget(self):
        await self.p.a.ensure('bravo', request())
        self.p.nodes['alpha'].busy = True
        state = self.p.a._state('bravo')
        state['episode'].update(kind='recovery', state='healthy', attempts=1)
        self.p.a._save(state)
        self.p.a.recovery['bravo'] = request()
        # Monotonic epochs are arbitrary: exercise a negative backdated value
        # on Windows as well as on short-lived Linux/macOS CI runners.
        self.p.a.healthy_since['bravo'] = min(-10, time.monotonic()-301)
        self.p.a.last_progress.clear()
        await self.p.a.maintenance()
        self.assertEqual(self.p.a._state('bravo')['episode']['attempts'], 1)
        self.assertIn('bravo', self.p.a.recovery)
        self.p.a.record_progress('bravo'); self.p.a.last_probe.clear()
        await self.p.a.maintenance()
        self.assertNotIn('episode', self.p.a._state('bravo'))
        self.assertNotIn('bravo', self.p.a.recovery)

    async def test_only_successful_substantive_rpc_marks_progress(self):
        await self.p.a.ensure('bravo', request())
        self.p.a.last_progress.clear()
        async with self.p.a.route('bravo', activity=False): pass
        self.assertNotIn('bravo', self.p.a.last_progress)
        with self.assertRaises(BridgeError):
            async with self.p.a.route('bravo', activity=True):
                raise BridgeError('peer_unavailable', 'Lost response')
        self.assertNotIn('bravo', self.p.a.last_progress)
        async with self.p.a.route('bravo', activity=True): pass
        self.assertIn('bravo', self.p.a.last_progress)

    async def test_failed_bridge_probe_clears_availability_separately_from_codex(self):
        await self.p.a.ensure('bravo', request())
        self.p.a.record_codex('bravo', {'ready': True})
        self.p.a.record_probe_failure('bravo', BridgeError('peer_unavailable', 'Carrier disappeared', True))
        result = self.p.a.status('bravo')
        self.assertFalse(result['available'])
        self.assertEqual(result['stages']['remote_bridge']['state'], 'fail')
        self.assertEqual(result['stages']['remote_codex']['state'], 'not_checked')
        self.assertEqual(result['stages']['forwarding']['state'], 'stale')


if __name__ == '__main__': unittest.main()
