"""Two real HTTP Bridge daemons and journals; TCP proxies stand in for SSH.

These tests exercise protocol/session integration, not SSH OS receiving access.
"""
import asyncio
import hashlib
import socket
import time
import unittest
import uuid

from codex_bridge.core import BridgeError
import test_core


def request_id():
    return str(uuid.uuid4())


class ConnectionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    save_config = test_core.CoreIntegrationTests.save_config

    async def asyncSetUp(self):
        await test_core.CoreIntegrationTests.asyncSetUp(self)
        self.carriers = {}
        self.channels = set()
        self.carrier_channels = {}
        self.carrier_writers = {}
        self.opens = []
        reservations = [socket.socket() for _ in range(4)]
        for item in reservations: item.bind(('127.0.0.1', 0))
        ports = [item.getsockname()[1] for item in reservations]
        for item in reservations: item.close()
        self.lanes = {'alpha': {'alpha': ports[0], 'beta': ports[1]},
                      'beta': {'alpha': ports[2], 'beta': ports[3]}}
        for node, peer in (('alpha', 'beta'), ('beta', 'alpha')):
            self.configs[node]['connections'] = {peer: {'lanes': self.lanes,
                'initial_attempts': 1, 'initial_seconds': 20,
                'recovery_attempts': 1, 'recovery_seconds': 20, 'idle_seconds': 1}}
            self.save_config(node)
            manager = self.bridges[node].connections

            async def open_candidate(peer, candidate, node=node, manager=manager):
                self.opens.append((node, candidate['attempt_id']))
                self.assertGreater(manager._state(peer)['episode']['attempts'], 0)
                carrier_key = (node, candidate['attempt_id'])
                self.carrier_channels[carrier_key] = set()
                self.carrier_writers[carrier_key] = set()
                listeners = []
                for source, target in ((node, peer), (peer, node)):
                    target_port = self.configs[target]['listen_port']
                    async def forward(reader, writer, target_port=target_port, carrier_key=carrier_key):
                        task = asyncio.current_task(); self.channels.add(task)
                        self.carrier_channels.setdefault(carrier_key, set()).add(task)
                        self.carrier_writers.setdefault(carrier_key, set()).add(writer)
                        remote = None
                        try:
                            remote_reader, remote = await asyncio.open_connection('127.0.0.1', target_port)
                            self.carrier_writers.setdefault(carrier_key, set()).add(remote)
                            async def copy(incoming, outgoing):
                                while data := await incoming.read(65536):
                                    outgoing.write(data); await outgoing.drain()
                                outgoing.close()
                            await asyncio.gather(copy(reader, remote), copy(remote_reader, writer))
                        except (OSError, asyncio.CancelledError):
                            pass
                        finally:
                            writer.close()
                            if remote: remote.close()
                            self.channels.discard(task)
                            self.carrier_channels.get(carrier_key, set()).discard(task)
                            self.carrier_writers.get(carrier_key, set()).discard(writer)
                            if remote: self.carrier_writers.get(carrier_key, set()).discard(remote)
                    listeners.append(await asyncio.start_server(forward, '127.0.0.1', self.lanes[node][source]))
                self.carriers[(node, candidate['attempt_id'])] = listeners
                await manager._verify(peer, candidate)

            async def close_owner(peer, attempt, node=node):
                key = (node, attempt)
                listeners = self.carriers.pop(key, [])
                # SSH death removes both listeners and all their channels.
                # Python 3.12+ wait_closed also waits for active clients, so
                # never wait for one listener while the other still accepts.
                for server in listeners: server.close()
                channels = self.carrier_channels.pop(key, set())
                for writer in self.carrier_writers.pop(key, set()): writer.close()
                for task in channels: task.cancel()
                await asyncio.gather(*channels, return_exceptions=True)
                await asyncio.gather(*(server.wait_closed() for server in listeners))

            async def close_losers(peer, winner, node=node, close_owner=close_owner):
                for origin, attempt in list(self.carriers):
                    if origin == node and attempt != winner: await close_owner(peer, attempt)

            manager._open_candidate = open_candidate
            manager._close_owner = close_owner
            manager._close_losers = close_losers

    async def asyncTearDown(self):
        for bridge in self.bridges.values(): await bridge.connections.close()
        listeners = [server for servers in self.carriers.values() for server in servers]
        for server in listeners: server.close()
        for task in list(self.channels): task.cancel()
        await asyncio.gather(*list(self.channels), return_exceptions=True)
        await asyncio.gather(*(server.wait_closed() for server in listeners))
        await test_core.CoreIntegrationTests.asyncTearDown(self)

    async def connect(self, origin='alpha', retry=False):
        peer = 'beta' if origin == 'alpha' else 'alpha'
        result = await self.bridges[origin].op_connection_retry({'peer_id': peer, 'request_id': request_id()}) if retry else await self.bridges[origin].op_connection_ensure({'peer_id': peer, 'request_id': request_id()})
        self.assertTrue(result['available'], result)
        return result

    async def session(self, project='first'):
        return await self.bridges['alpha'].op_session_create({'peer_id': 'beta', 'project_id': project,
            'name': project, 'goal': 'Harmless protocol test', 'session_id': request_id()})

    async def completed(self, bridge, session, rid):
        for _ in range(100):
            result = await bridge.op_task_status({'session_id': session, 'request_id': rid})
            if result['status'] == 'completed': return result
            await asyncio.sleep(.01)
        self.fail('Fake task did not finish: ' + repr(result))

    async def test_both_directions_context_project_isolation_and_file(self):
        await self.connect('beta')
        a, b = self.bridges['alpha'], self.bridges['beta']
        first, second = await self.session(), await self.session('second')
        for bridge in (a, b):
            for session in (first, second):
                args = {'session_id': session['session_id'], 'request_id': request_id(), 'prompt': 'Test ' + session['name']}
                await bridge.op_task_send(args)
                await self.completed(bridge, args['session_id'], args['request_id'])
                await bridge.op_task_send(args)
        self.assertEqual([len(adapter.runs) for adapter in self.adapters.values()], [2, 2])
        follow = {'session_id': first['session_id'], 'request_id': request_id(), 'prompt': 'Follow up'}
        await a.op_task_send(follow); await self.completed(a, follow['session_id'], follow['request_id'])
        self.assertEqual(self.adapters['beta'].runs[0]['thread_id'], self.adapters['beta'].runs[2]['thread_id'])
        self.assertNotEqual(self.adapters['beta'].runs[0]['thread_id'], self.adapters['beta'].runs[1]['thread_id'])
        root = self.root / 'alpha' / 'first' / 'export'
        data = b'file selected by owner\n'; (root / 'sample.txt').write_bytes(data)
        result = await a.op_artifact_send({'session_id': first['session_id'], 'path': 'sample.txt', 'destination': 'sample.txt', 'request_id': request_id()})
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual((self.root / 'beta' / 'first' / 'import' / 'sample.txt').read_bytes(), data)
        self.assertFalse(a.connection_activity('beta'))
        self.assertFalse(b.connection_activity('alpha'))

    async def test_simultaneous_ensure_http_and_generation_fence(self):
        left, right = await asyncio.gather(self.connect('alpha'), self.connect('beta'))
        self.assertEqual(left['decision']['connection_id'], right['decision']['connection_id'])
        await asyncio.sleep(.6)
        self.assertEqual(len(self.carriers), 1)
        with self.assertRaises(BridgeError) as error:
            await self.bridges['alpha'].remote_direct('beta', 'peer.status', {}, self.bridges['alpha'].connections._url('beta', left['decision']['candidate']))
        self.assertEqual(error.exception.code, 'stale_generation')

    async def test_lost_ack_result_preservation_reconnect_other_origin_no_reexecution(self):
        chosen = await self.connect('alpha')
        a, b = self.bridges['alpha'], self.bridges['beta']
        session = await self.session(); sid = session['session_id']; rid = request_id()
        args = {'session_id': sid, 'request_id': rid, 'prompt': 'Execute once'}
        await a.op_task_send(args)
        for _ in range(50):
            if b.store.get('incoming', rid)['status'] == 'completed': break
            await asyncio.sleep(.01)
        self.assertTrue(b.connection_activity('alpha'), 'Unread result must prevent idle closure')
        await a.connections._close_owner('beta', chosen['decision']['connection_id'])
        await self.connect('beta', retry=True)
        await a.op_task_send(args)
        result = await self.completed(a, sid, rid)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.adapters['beta'].runs), 1)
        self.assertFalse(b.connection_activity('alpha'))
        self.assertIsNotNone(b.store.get('task_delivery_acks', rid))

    async def test_old_commit_cleanup_never_kills_new_candidate_before_prepare(self):
        chosen = await self.connect('alpha')
        a, b = self.bridges['alpha'], self.bridges['beta']
        await a.connections._close_owner('beta', chosen['decision']['connection_id'])
        # Linux reports the known-dead listener immediately. Simulate that
        # evidence on every OS to expose the old +0.5s cleanup timer reliably.
        for manager in (a.connections, b.connections):
            original_health = manager._healthy
            async def health(peer, decision, original_health=original_health):
                if decision and decision['generation'] == chosen['generation']:
                    return False
                return await original_health(peer, decision)
            manager._healthy = health
        entered, release = asyncio.Event(), asyncio.Event()
        original_select = b.connections._select
        async def hold_before_prepare(peer, candidate):
            entered.set(); await release.wait()
            return await original_select(peer, candidate)
        b.connections._select = hold_before_prepare
        # Authorize cleanup before the new candidate exists, then hold the
        # new proposal at the old generation until that timer has fired.
        b.connections._defer_cleanup('alpha', chosen['generation'], chosen['decision']['connection_id'])
        pending = asyncio.create_task(self.connect('beta', retry=True))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            # Replaying the old commit across this candidate must also not
            # authorize a fresh sweep that includes this new SSH carrier.
            await b.connections._receive_commit('alpha', {'decision': chosen['decision']})
            await asyncio.sleep(.65)
            self.assertTrue(any(origin == 'beta' for origin, _ in self.carriers),
                            'An old commit timer killed a later candidate before preparation')
        finally:
            release.set()
        new = await pending
        self.assertEqual(new['generation'], chosen['generation'] + 1)
        # Successful commit must leave a usable carrier for subsequent HTTP,
        # not merely deliver the response over a channel that is already open.
        status = await a.remote('beta', 'peer.status', {})
        self.assertEqual(status['peer_id'], 'beta')

    async def test_waiting_status_and_pending_delivery_never_launch(self):
        for bridge in self.bridges.values():
            for _ in range(3):
                await bridge.op_connection_status({}); await bridge.op_peer_status({})
                await bridge.connections.maintenance()
        self.assertEqual(self.opens, [])

    async def test_deliberate_stop_retains_task_until_explicit_resume(self):
        await self.connect()
        a, b = self.bridges['alpha'], self.bridges['beta']
        session = await self.session(); sid = session['session_id']
        stopped = await a.op_connection_disconnect({'peer_id': 'beta', 'request_id': request_id()})
        self.assertTrue(stopped['stop_requested'])
        args = {'session_id': sid, 'request_id': request_id(), 'prompt': 'Held until owner resumes'}
        task = await a.op_task_send(args)
        self.assertEqual(task['status'], 'pending_delivery')
        before = len(self.opens)
        for _ in range(2):
            await a.connections.maintenance(); await a.op_task_status(args)
        self.assertEqual(len(self.opens), before)
        self.assertEqual(len(self.adapters['beta'].runs), 0)
        await self.connect(retry=True)
        await a.op_task_send(args)
        await self.completed(a, sid, args['request_id'])
        self.assertEqual(len(self.adapters['beta'].runs), 1)

    async def test_cancellation_result_ack_and_peer_revocation(self):
        await self.connect()
        a, b = self.bridges['alpha'], self.bridges['beta']
        session = await self.session(); sid = session['session_id']; rid = request_id()
        self.adapters['beta'].block = True
        await a.op_task_send({'session_id': sid, 'request_id': rid, 'prompt': 'Wait for cancellation'})
        for _ in range(100):
            if self.adapters['beta'].runs: break
            await asyncio.sleep(.01)
        with self.assertRaises(BridgeError) as error:
            await a.op_connection_disconnect({'peer_id': 'beta', 'request_id': request_id()})
        self.assertEqual(error.exception.code, 'connection_busy')
        await a.op_task_cancel({'session_id': sid, 'request_id': rid})
        for _ in range(100):
            status = await a.op_task_status({'session_id': sid, 'request_id': rid})
            if status['status'] == 'cancelled': break
            await asyncio.sleep(.01)
        self.assertEqual(status['status'], 'cancelled')
        self.assertFalse(b.connection_activity('alpha'))
        await a.op_peer_revoke({'peer_id': 'beta'})
        with self.assertRaises(BridgeError):
            await b.remote('alpha', 'peer.status', {})
        # Revocation cannot kill the maintenance loop or start another carrier.
        before = len(self.opens)
        await a.connections.maintenance()
        self.assertEqual(len(self.opens), before)

    async def test_codex_unready_is_separate_from_authenticated_bridge(self):
        async def unavailable():
            raise BridgeError('codex_signed_out', 'Owner-local sign-in required')
        self.adapters['beta'].capabilities = unavailable
        result = await self.connect()
        self.assertTrue(result['available'])
        self.assertEqual(result['stages']['remote_bridge']['state'], 'pass')
        self.assertEqual(result['stages']['remote_codex']['state'], 'fail')
        local = await self.bridges['beta'].op_local_status({})
        self.assertTrue(local['ready'])
        self.assertFalse(local['codex']['ready'])

    async def test_carrier_lost_after_selection_is_not_reported_as_codex_failure(self):
        a = self.bridges['alpha']; original = a.remote
        async def lost_probe(peer, method, params):
            if peer == 'beta' and method == 'peer.status':
                raise BridgeError('peer_unavailable', 'Carrier disappeared after selection', True)
            return await original(peer, method, params)
        a.remote = lost_probe
        result = await a.op_connection_ensure({'peer_id': 'beta', 'request_id': request_id()})
        self.assertFalse(result['available'])
        self.assertEqual(result['stages']['remote_bridge']['state'], 'fail')
        self.assertEqual(result['stages']['remote_codex']['state'], 'not_checked')
        self.assertEqual(result['error']['code'], 'peer_unavailable')
