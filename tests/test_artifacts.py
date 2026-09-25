"""Chunked transfers across real loopback HTTP, with no account or live files."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import time
import threading
import unittest
import uuid
from unittest.mock import patch

import test_core
from codex_bridge.artifacts import CHUNK_BYTES, MAX_FILE_BYTES, TRANSFER_TTL_SECONDS
from codex_bridge.core import Bridge, BridgeError


class ChunkedArtifactTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_core.CoreIntegrationTests.asyncSetUp
    save_config = test_core.CoreIntegrationTests.save_config
    rpc = test_core.CoreIntegrationTests.rpc
    create = test_core.CoreIntegrationTests.create

    async def asyncTearDown(self):
        for bridge in self.bridges.values():
            await bridge.artifact_transfers.close()
        await test_core.CoreIntegrationTests.asyncTearDown(self)

    async def finished(self, node, request, session='session-first'):
        for _ in range(500):
            result = await self.rpc(node, 'artifact_transfer_status', {'session_id': session, 'request_id': request})
            if result['status'] in ('completed', 'paused', 'failed', 'aborted'):
                return result
            await asyncio.sleep(.01)
        self.fail('Transfer did not reach a terminal or paused state')

    def export(self, node, name='large.bin', project='first'):
        data = (b'bridge-artifact-test\x00' * (CHUNK_BYTES // 20 + 1))[:CHUNK_BYTES] * 9 + b'end'
        path = Path(self.configs[node]['projects'][project]['export_root']) / name
        path.write_bytes(data)
        return data

    def send_args(self, request='large-send', **changes):
        return {'session_id': 'session-first', 'request_id': request,
                'path': 'large.bin', 'destination': 'received.bin', **changes}

    async def test_large_send_both_directions_fetch_and_completed_retry(self):
        await self.create()
        for node, peer in [('alpha', 'beta'), ('beta', 'alpha')]:
            data = self.export(node)
            args = self.send_args(node + '-send')
            initial = await self.rpc(node, 'artifact_send', args)
            self.assertEqual(initial['status'], 'queued')
            final = await self.finished(node, args['request_id'])
            self.assertEqual(final['status'], 'completed', final)
            result = final['result']
            target = Path(self.configs[peer]['projects']['first']['import_root']) / 'received.bin'
            self.assertEqual(target.read_bytes(), data)
            again = await self.rpc(node, 'artifact_send', args)
            self.assertEqual(again, result)
            fetch = {'session_id': 'session-first', 'request_id': node + '-fetch',
                     'artifact_id': result['artifact_id'], 'destination': 'fetched.bin'}
            await self.rpc(node, 'artifact_fetch', fetch)
            fetched = await self.finished(node, fetch['request_id'])
            self.assertEqual(fetched['status'], 'completed', fetched)
            self.assertEqual(Path(fetched['result']['path']).read_bytes(), data)

    async def test_dropped_chunk_reply_and_receiver_restart_resume_once(self):
        await self.create()
        data = self.export('alpha')
        sender = self.bridges['alpha']
        original = sender.remote
        sent_indices = []
        async def unreliable(peer_id, method, params):
            result = await original(peer_id, method, params)
            if method == 'peer.artifact_chunk':
                sent_indices.append(params['index'])
                if params['index'] == 2 and sent_indices.count(2) == 1:
                    raise BridgeError('peer_unavailable', 'Injected dropped response', True)
            return result
        sender.remote = unreliable
        args = self.send_args()
        await self.rpc('alpha', 'artifact_send', args)
        paused = await self.finished('alpha', args['request_id'])
        self.assertEqual(paused['status'], 'paused')
        old = self.bridges['beta']
        self.servers['beta'].close()
        await self.servers['beta'].wait_closed()
        old.store.db.close()
        self.bridges['beta'] = replacement = Bridge(old.config_path, adapter=self.adapters['beta'])
        self.servers['beta'] = await asyncio.start_server(replacement.handle_http, '127.0.0.1', self.configs['beta']['listen_port'])
        await self.rpc('alpha', 'artifact_send', args)
        result = await self.finished('alpha', args['request_id'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(len(sent_indices), len(set(sent_indices)))
        self.assertEqual(Path(result['result']['path']).read_bytes(), data)
        self.assertEqual(len(replacement.store.all('artifacts')), 1)

    async def test_conflicting_request_scope_and_old_peer_rejected(self):
        await self.create()
        await self.create('second', 'session-second')
        self.export('alpha')
        self.export('alpha', project='second')
        sender = self.bridges['alpha']
        original = sender.remote
        async def old_peer(peer_id, method, params):
            result = await original(peer_id, method, params)
            if method == 'peer.status':
                result['capabilities'].remove('artifacts_chunked_v1')
            return result
        sender.remote = old_peer
        args = self.send_args()
        await self.rpc('alpha', 'artifact_send', args)
        failed = await self.finished('alpha', args['request_id'])
        self.assertEqual(failed['error']['code'], 'unsupported_transfer')
        with self.assertRaises(BridgeError) as caught:
            await self.rpc('alpha', 'artifact_send', {**args, 'session_id': 'session-second'})
        self.assertEqual(caught.exception.code, 'duplicate_conflict')
        self.assertEqual(self.bridges['beta'].store.all('artifact_transfers'), [])

    async def test_lost_commit_reply_sender_restart_retains_immutable_snapshot(self):
        await self.create()
        data = self.export('alpha')
        sender = self.bridges['alpha']
        original = sender.remote
        async def unreliable(peer_id, method, params):
            result = await original(peer_id, method, params)
            if method == 'peer.artifact_commit':
                raise BridgeError('peer_unavailable', 'Injected lost final response', True)
            return result
        sender.remote = unreliable
        args = self.send_args()
        await self.rpc('alpha', 'artifact_send', args)
        self.assertEqual((await self.finished('alpha', args['request_id']))['status'], 'paused')
        target = Path(self.configs['beta']['projects']['first']['import_root']) / 'received.bin'
        original_time = target.stat().st_mtime_ns
        self.servers['alpha'].close()
        await self.servers['alpha'].wait_closed()
        await sender.artifact_transfers.close()
        sender.store.db.close()
        self.bridges['alpha'] = replacement = Bridge(sender.config_path, adapter=self.adapters['alpha'])
        self.servers['alpha'] = await asyncio.start_server(replacement.handle_http, '127.0.0.1', self.configs['alpha']['listen_port'])
        source = Path(self.configs['alpha']['projects']['first']['export_root']) / 'large.bin'
        source.write_bytes(b'changed after the durable snapshot')
        await self.rpc('alpha', 'artifact_send', args)
        final = await self.finished('alpha', args['request_id'])
        self.assertEqual(final['status'], 'completed', final)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(target.stat().st_mtime_ns, original_time)
        self.assertEqual(len(self.bridges['beta'].store.all('artifacts')), 1)

    async def test_commit_retry_after_import_publish_does_not_overwrite(self):
        await self.create()
        self.export('alpha')
        manager = self.bridges['beta'].artifact_transfers
        original = manager.archive
        attempts = 0
        async def interrupted(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BridgeError('peer_unavailable', 'Injected interruption after atomic import publication', True)
            return await original(*args)
        manager.archive = interrupted
        args = self.send_args()
        await self.rpc('alpha', 'artifact_send', args)
        self.assertEqual((await self.finished('alpha', args['request_id']))['status'], 'paused')
        target = Path(self.configs['beta']['projects']['first']['import_root']) / 'received.bin'
        first_time = target.stat().st_mtime_ns
        await self.rpc('alpha', 'artifact_send', args)
        final = await self.finished('alpha', args['request_id'])
        self.assertEqual(final['status'], 'completed', final)
        self.assertEqual(target.stat().st_mtime_ns, first_time)

    async def test_disk_hashing_does_not_block_other_bridge_requests(self):
        await self.create()
        self.export('alpha')
        import codex_bridge.artifacts as artifacts
        original = artifacts.sha_file
        entered = threading.Event()
        release = threading.Event()
        escaped = threading.Event()
        completed = threading.Event()
        def slow_hash(path):
            entered.set()
            if not release.wait(10):
                escaped.set()
            result = original(path)
            completed.set()
            return result
        with patch('codex_bridge.artifacts.sha_file', slow_hash):
            try:
                await self.rpc('alpha', 'artifact_send', self.send_args())
                self.assertTrue(await asyncio.to_thread(entered.wait, 10), 'Hashing did not start')
                response = await self.rpc('alpha', 'peer_status', {})
                self.assertTrue(response['peers'][0]['available'])
                self.assertFalse(escaped.is_set(), 'Hashing blocked progress until its failsafe expired')
                self.assertFalse(completed.is_set(), 'Status must complete while hashing is still held')
                self.assertFalse(release.is_set())
            finally:
                release.set()
            self.assertEqual((await self.finished('alpha', 'large-send'))['status'], 'completed')

    async def offer(self, **changes):
        payload = {'session_id': 'session-first', 'request_id': 'manual-offer', 'artifact_id': 'manual-artifact',
                   'size': CHUNK_BYTES + 1, 'sha256': hashlib.sha256(b'x' * (CHUNK_BYTES + 1)).hexdigest(),
                   'destination': 'manual.bin', **changes}
        reply = await self.bridges['alpha'].remote('beta', 'peer.artifact_begin', payload)
        return payload, reply

    async def test_bad_chunk_hash_full_hash_traversal_and_overwrite_refused(self):
        await self.create()
        sender = self.bridges['alpha']
        for destination in ('../outside.bin', 'C:/outside.bin'):
            with self.assertRaises(BridgeError) as caught:
                await self.offer(destination=destination)
            self.assertEqual(caught.exception.code, 'path_denied')
        root = Path(self.configs['beta']['projects']['first']['import_root'])
        (root / 'existing.bin').write_bytes(b'existing')
        with self.assertRaises(BridgeError) as caught:
            await self.offer(destination='existing.bin')
        self.assertEqual(caught.exception.code, 'file_exists')
        _, reply = await self.offer(sha256='0' * 64)
        p = {'session_id': 'session-first', 'transfer_id': reply['transfer_id'], 'index': 0,
             'sha256': '0' * 64, 'data': base64.b64encode(b'x' * CHUNK_BYTES).decode()}
        with self.assertRaises(BridgeError) as caught:
            await sender.remote('beta', 'peer.artifact_chunk', p)
        self.assertEqual(caught.exception.code, 'invalid_artifact')
        for index, data in [(0, b'x' * CHUNK_BYTES), (1, b'x')]:
            await sender.remote('beta', 'peer.artifact_chunk', {**p, 'index': index,
                'sha256': hashlib.sha256(data).hexdigest(), 'data': base64.b64encode(data).decode()})
        with self.assertRaises(BridgeError) as caught:
            await sender.remote('beta', 'peer.artifact_commit', {'session_id': 'session-first', 'transfer_id': reply['transfer_id']})
        self.assertEqual(caught.exception.code, 'invalid_artifact')
        self.assertFalse((root / 'manual.bin').exists())
        self.assertEqual((root / 'existing.bin').read_bytes(), b'existing')

    async def test_duplicate_chunks_scope_expiry_abort_and_limits(self):
        await self.create()
        await self.create('second', 'session-second')
        with self.assertRaises(BridgeError) as caught:
            await self.offer(size=MAX_FILE_BYTES + 1)
        self.assertEqual(caught.exception.code, 'file_denied')
        with patch('codex_bridge.artifacts.MAX_STORAGE_BYTES', 1):
            with self.assertRaises(BridgeError) as caught:
                await self.offer()
            self.assertEqual(caught.exception.code, 'storage_limit')
        _, reply = await self.offer()
        sender = self.bridges['alpha']
        params = {'session_id': 'session-first', 'transfer_id': reply['transfer_id'], 'index': 0,
                  'sha256': hashlib.sha256(b'x' * CHUNK_BYTES).hexdigest(),
                  'data': base64.b64encode(b'x' * CHUNK_BYTES).decode()}
        await sender.remote('beta', 'peer.artifact_chunk', params)
        repeated = await sender.remote('beta', 'peer.artifact_chunk', params)
        self.assertEqual(repeated['received_bytes'], CHUNK_BYTES)
        with self.assertRaises(BridgeError) as caught:
            await sender.remote('beta', 'peer.artifact_chunk', {**params, 'session_id': 'session-second'})
        self.assertEqual(caught.exception.code, 'transfer_not_found')
        receiver = self.bridges['beta']
        record = receiver.store.get('artifact_transfers', reply['transfer_id'])
        record['updated_epoch'] = time.time() - TRANSFER_TTL_SECONDS - 1
        receiver.store.put('artifact_transfers', reply['transfer_id'], record)
        receiver.artifact_transfers.cleanup()
        self.assertFalse((receiver.artifact_transfers.root / reply['transfer_id']).exists())
        with self.assertRaises(BridgeError) as caught:
            await self.offer()
        self.assertEqual(caught.exception.code, 'transfer_closed')
        _, second = await self.offer(request_id='another')
        aborted = await sender.remote('beta', 'peer.artifact_abort', {'session_id': 'session-first', 'transfer_id': second['transfer_id']})
        self.assertEqual(aborted['status'], 'aborted')

    async def test_cancel_and_revoke_stop_background_transfers(self):
        await self.create()
        self.export('alpha')
        sender = self.bridges['alpha']
        original = sender.remote
        held = asyncio.Event()
        release = asyncio.Event()
        async def delayed(peer_id, method, params):
            if method == 'peer.artifact_chunk':
                held.set()
                await release.wait()
            return await original(peer_id, method, params)
        sender.remote = delayed
        args = self.send_args()
        await self.rpc('alpha', 'artifact_send', args)
        await asyncio.wait_for(held.wait(), 10)
        cancelled = await self.rpc('alpha', 'artifact_transfer_cancel', {'session_id': 'session-first', 'request_id': args['request_id']})
        self.assertEqual(cancelled['status'], 'aborted')
        release.set()
        await asyncio.gather(*list(sender.artifact_transfers.running.values()))
        self.assertFalse((Path(self.configs['beta']['projects']['first']['import_root']) / 'received.bin').exists())
        with self.assertRaises(BridgeError) as caught:
            await self.rpc('alpha', 'artifact_send', args)
        self.assertEqual(caught.exception.code, 'transfer_closed')
        _, reply = await self.offer(request_id='revoke')
        await self.rpc('beta', 'peer_revoke', {'peer_id': 'alpha'})
        self.assertEqual(self.bridges['beta'].store.get('artifact_transfers', reply['transfer_id'])['status'], 'aborted')

    async def test_failed_abort_retry_acknowledges_same_transfer_without_resuming(self):
        await self.create()
        self.export('alpha')
        sender, receiver = self.bridges['alpha'], self.bridges['beta']
        original = sender.remote
        aborts = []
        async def unreliable(peer_id, method, params):
            if method == 'peer.artifact_chunk':
                raise BridgeError('peer_unavailable', 'Injected transfer interruption', True)
            if method == 'peer.artifact_abort':
                aborts.append(dict(params))
                if len(aborts) <= 2:
                    raise BridgeError('peer_unavailable', 'Injected abort delivery failure', True)
            return await original(peer_id, method, params)
        sender.remote = unreliable
        args = self.send_args('abort-retry')
        await self.rpc('alpha', 'artifact_send', args)
        paused = await self.finished('alpha', args['request_id'])
        self.assertEqual(paused['status'], 'paused')
        for count in (1, 2):
            failed = await self.rpc('alpha', 'artifact_transfer_cancel', args)
            self.assertEqual(failed['status'], 'aborted')
            self.assertTrue(failed['remote_abort_pending'])
            self.assertEqual(len(aborts), count, 'Only explicit cancellation may retry')
            self.assertEqual(receiver.store.get('artifact_transfers', paused['transfer_id'])['status'], 'receiving')
        final = await self.rpc('alpha', 'artifact_transfer_cancel', args)
        self.assertEqual(final['status'], 'aborted')
        self.assertFalse(final.get('remote_abort_pending', False))
        self.assertNotIn('error', final)
        self.assertEqual(aborts, [{'session_id': args['session_id'], 'transfer_id': paused['transfer_id']}] * 3)
        self.assertEqual(len(receiver.store.all('artifact_transfers')), 1)
        self.assertEqual(receiver.store.get('artifact_transfers', paused['transfer_id'])['status'], 'aborted')
        self.assertFalse(sender.connection_activity('beta'))
        with self.assertRaises(BridgeError) as caught:
            await self.rpc('alpha', 'artifact_send', args)
        self.assertEqual(caught.exception.code, 'transfer_closed')
        self.assertIsNone(sender.store.get('mutations', args['request_id']))
        self.assertFalse((Path(self.configs['beta']['projects']['first']['import_root']) / 'received.bin').exists())

    async def test_abort_retry_preserves_completed_commit_and_request_deduplication(self):
        await self.create()
        data = self.export('alpha')
        sender, receiver = self.bridges['alpha'], self.bridges['beta']
        original = sender.remote
        commits, aborts = [], []
        async def unreliable(peer_id, method, params):
            if method == 'peer.artifact_abort':
                aborts.append(dict(params))
                if len(aborts) == 1:
                    raise BridgeError('peer_unavailable', 'Injected abort delivery failure', True)
            result = await original(peer_id, method, params)
            if method == 'peer.artifact_commit':
                commits.append(dict(params))
                raise BridgeError('peer_unavailable', 'Injected lost commit receipt', True)
            return result
        sender.remote = unreliable
        args = self.send_args('committed-abort-retry')
        await self.rpc('alpha', 'artifact_send', args)
        paused = await self.finished('alpha', args['request_id'])
        self.assertEqual(paused['status'], 'paused')
        committed = receiver.store.get('artifact_transfers', paused['transfer_id'])['result']
        target = Path(self.configs['beta']['projects']['first']['import_root']) / 'received.bin'
        first_mtime = target.stat().st_mtime_ns
        failed = await self.rpc('alpha', 'artifact_transfer_cancel', args)
        self.assertTrue(failed['remote_abort_pending'])
        final = await self.rpc('alpha', 'artifact_transfer_cancel', args)
        self.assertEqual(final['status'], 'completed')
        self.assertFalse(final.get('remote_abort_pending', False))
        self.assertNotIn('error', final)
        self.assertEqual(final['result'], committed)
        self.assertEqual(await self.rpc('alpha', 'artifact_send', args), committed)
        self.assertEqual((await self.rpc('alpha', 'artifact_transfer_cancel', args))['result'], committed)
        self.assertEqual(len(commits), 1)
        self.assertEqual(aborts, [{'session_id': args['session_id'], 'transfer_id': paused['transfer_id']}] * 2)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(target.stat().st_mtime_ns, first_mtime)
        self.assertEqual(len(receiver.store.all('artifacts')), 1)
        self.assertEqual(sender.store.get('mutations', args['request_id'])['result'], committed)
        self.assertFalse(sender.connection_activity('beta'))

    async def test_explicit_cancel_reconciles_historical_completed_pending_flag(self):
        await self.create()
        self.export('alpha')
        args = self.send_args('historical-pending-abort')
        await self.rpc('alpha', 'artifact_send', args)
        completed = await self.finished('alpha', args['request_id'])
        sender = self.bridges['alpha']
        value = sender.store.get('artifact_requests', args['request_id'])
        value.update(remote_abort_pending=True, error={'code': 'peer_unavailable'})
        sender.store.put('artifact_requests', args['request_id'], value)
        original = sender.remote
        calls = []
        async def observe(peer_id, method, params):
            calls.append((method, dict(params)))
            return await original(peer_id, method, params)
        sender.remote = observe
        final = await self.rpc('alpha', 'artifact_transfer_cancel', args)
        self.assertEqual(final['status'], 'completed')
        self.assertFalse(final.get('remote_abort_pending', False))
        self.assertNotIn('error', final)
        self.assertEqual(final['result'], completed['result'])
        self.assertEqual(calls, [('peer.artifact_abort', {'session_id': args['session_id'], 'transfer_id': completed['transfer_id']})])
        self.assertEqual(await self.rpc('alpha', 'artifact_send', args), completed['result'])
        self.assertEqual(len(calls), 1)

    async def test_terminal_pending_abort_keeps_only_its_peer_active(self):
        bridge = self.bridges['alpha']
        for kind in ('artifact_requests', 'artifact_transfers'):
            for status in ('aborted', 'failed', 'cancelled', 'expired', 'completed'):
                with self.subTest(kind=kind, status=status):
                    value = {'status': status, 'remote_abort_pending': True,
                             'peer_id': 'beta'} if kind == 'artifact_requests' else {
                                 'status': status, 'remote_abort_pending': True, 'actor': 'beta'}
                    bridge.store.put(kind, 'pending-abort', value)
                    self.assertTrue(bridge.connection_activity('beta'))
                    self.assertFalse(bridge.connection_activity('another-peer'))
                    value['remote_abort_pending'] = False
                    bridge.store.put(kind, 'pending-abort', value)
                    self.assertFalse(bridge.connection_activity('beta'))

    async def test_unacknowledged_abort_reply_never_clears_pending_intent(self):
        await self.create()
        sender = self.bridges['alpha']
        args = {'session_id': 'session-first', 'request_id': 'invalid-abort-reply'}
        sender.store.put('artifact_requests', args['request_id'], {**args, 'direction': 'send',
            'peer_id': 'beta', 'transfer_id': 'unacknowledged-transfer', 'status': 'paused', 'digest': 'unchanged'})
        replies = [None, {'status': 'receiving'}, {'status': 'completed'}]
        for reply in replies:
            calls = []
            async def invalid(peer_id, method, params):
                calls.append((peer_id, method, params))
                return reply
            sender.remote = invalid
            result = await sender.artifact_transfers.cancel(args)
            self.assertTrue(result['remote_abort_pending'])
            self.assertEqual(result['status'], 'aborted')
            self.assertEqual(result['error']['code'], 'invalid_response')
            self.assertTrue(sender.connection_activity('beta'))
            self.assertEqual(len(calls), 1)
            self.assertIsNone(sender.store.get('mutations', args['request_id']))

    async def test_concurrent_cancel_cannot_regress_completed_acknowledgment(self):
        await self.create()
        self.export('alpha')
        sender = self.bridges['alpha']
        original = sender.remote
        entered, release = asyncio.Event(), asyncio.Event()
        aborts = []
        async def unreliable(peer_id, method, params):
            if method == 'peer.artifact_abort':
                aborts.append(dict(params))
                if len(aborts) == 1:
                    entered.set()
                    await release.wait()
                    raise BridgeError('peer_unavailable', 'Injected delayed abort failure', True)
            result = await original(peer_id, method, params)
            if method == 'peer.artifact_commit':
                raise BridgeError('peer_unavailable', 'Injected lost commit receipt', True)
            return result
        sender.remote = unreliable
        args = self.send_args('concurrent-abort')
        await self.rpc('alpha', 'artifact_send', args)
        self.assertEqual((await self.finished('alpha', args['request_id']))['status'], 'paused')
        first = asyncio.create_task(sender.artifact_transfers.cancel(args))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(sender.artifact_transfers.cancel(args))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(len(aborts), 1, 'Same-request abort attempts must be serialized')
            self.assertTrue(sender.connection_activity('beta'))
        finally:
            release.set()
            outcomes = await asyncio.gather(first, second)
        self.assertTrue(outcomes[0]['remote_abort_pending'])
        self.assertEqual(outcomes[1]['status'], 'completed')
        final = await sender.artifact_transfers.status(args)
        self.assertEqual(final['status'], 'completed')
        self.assertFalse(final.get('remote_abort_pending', False))
        self.assertNotIn('error', final)
        self.assertEqual(await self.rpc('alpha', 'artifact_send', args), final['result'])
        self.assertEqual(len(aborts), 2)
        self.assertEqual(aborts[0], aborts[1])

    async def test_cancel_during_send_begin_preserves_failed_remote_abort_for_retry(self):
        await self.create()
        self.export('alpha')
        sender = self.bridges['alpha']
        original = sender.remote
        entered, release = asyncio.Event(), asyncio.Event()
        aborts = []
        async def unreliable(peer_id, method, params):
            if method == 'peer.artifact_abort':
                aborts.append(dict(params))
                if len(aborts) == 1:
                    raise BridgeError('peer_unavailable', 'Injected begin-race abort failure', True)
            result = await original(peer_id, method, params)
            if method == 'peer.artifact_begin':
                entered.set()
                await release.wait()
            return result
        sender.remote = unreliable
        args = self.send_args('begin-race-abort')
        await self.rpc('alpha', 'artifact_send', args)
        await asyncio.wait_for(entered.wait(), 5)
        try:
            cancelled = await self.rpc('alpha', 'artifact_transfer_cancel', args)
            self.assertEqual(cancelled['status'], 'aborted')
            self.assertNotIn('transfer_id', cancelled)
        finally:
            release.set()
            await asyncio.gather(*list(sender.artifact_transfers.running.values()))
        pending = await sender.artifact_transfers.status(args)
        self.assertTrue(pending['remote_abort_pending'])
        self.assertEqual(len(aborts), 1)
        self.assertTrue(sender.connection_activity('beta'))
        final = await self.rpc('alpha', 'artifact_transfer_cancel', args)
        self.assertEqual(final['status'], 'aborted')
        self.assertFalse(final.get('remote_abort_pending', False))
        self.assertEqual(aborts, [{'session_id': args['session_id'], 'transfer_id': pending['transfer_id']}] * 2)
        self.assertEqual(len(self.bridges['beta'].store.all('artifact_transfers')), 1)
        self.assertFalse(sender.connection_activity('beta'))
        self.assertFalse((Path(self.configs['beta']['projects']['first']['import_root']) / 'received.bin').exists())

    async def test_symlink_import_root_is_denied(self):
        await self.create()
        parent = self.root / 'outside'
        parent.mkdir()
        root = Path(self.configs['beta']['projects']['first']['import_root'])
        link = root / 'link'
        try:
            link.symlink_to(parent, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks is unavailable on this test account')
        with self.assertRaises(BridgeError) as caught:
            await self.offer(destination='link/denied.bin')
        self.assertEqual(caught.exception.code, 'path_denied')

    async def test_revocation_during_final_copy_prevents_publication(self):
        await self.create()
        self.export('alpha')
        manager = self.bridges['beta'].artifact_transfers
        original = manager.atomic_copy
        held, release = threading.Event(), threading.Event()
        import_root = Path(self.configs['beta']['projects']['first']['import_root'])
        def delayed(*args):
            if Path(args[1]) == import_root:
                held.set()
                if not release.wait(10):
                    raise RuntimeError('Test did not release final copy')
            return original(*args)
        manager.atomic_copy = delayed
        try:
            await self.rpc('alpha', 'artifact_send', self.send_args())
            self.assertTrue(await asyncio.to_thread(held.wait, 10))
            revoked = asyncio.create_task(self.rpc('beta', 'peer_revoke', {'peer_id': 'alpha'}))
            for _ in range(100):
                if not self.bridges['beta'].config()['peers']['alpha']['enabled']:
                    break
                await asyncio.sleep(.01)
            self.assertFalse(self.bridges['beta'].config()['peers']['alpha']['enabled'])
            release.set()
            self.assertTrue((await revoked)['revoked'])
            final = await self.finished('alpha', 'large-send')
            self.assertEqual(final['status'], 'failed', final)
            self.assertEqual(final['error']['code'], 'access_revoked')
            self.assertFalse((import_root / 'received.bin').exists())
        finally:
            release.set()

    async def test_inline_send_offer_and_fetch_share_the_archive_quota(self):
        await self.create()
        export = Path(self.configs['alpha']['projects']['first']['export_root']) / 'small.bin'
        export.write_bytes(b'12345')
        sender = self.bridges['alpha']
        args = self.send_args(path='small.bin')
        with patch('codex_bridge.artifacts.MAX_STORAGE_BYTES', 4):
            with self.assertRaises(BridgeError) as caught:
                await self.rpc('alpha', 'artifact_send', args)
            self.assertEqual(caught.exception.code, 'storage_limit')
            with self.assertRaises(BridgeError) as caught:
                await sender.remote('beta', 'peer.artifact_offer', {
                    'session_id': 'session-first', 'request_id': 'inline-offer', 'artifact_id': 'inline-id',
                    'destination': 'inline.bin', 'sha256': hashlib.sha256(b'12345').hexdigest(),
                    'data': base64.b64encode(b'12345').decode()})
            self.assertEqual(caught.exception.code, 'storage_limit')
        session = self.bridges['beta'].session('session-first')
        self.bridges['beta'].archive_artifact(session, 'fetch-inline', b'12345', 'local-fixture')
        with patch('codex_bridge.artifacts.MAX_STORAGE_BYTES', 4):
            with self.assertRaises(BridgeError) as caught:
                await self.rpc('alpha', 'artifact_fetch', {'session_id': 'session-first',
                    'request_id': 'fetch-small', 'artifact_id': 'fetch-inline', 'destination': 'fetched-small.bin'})
            self.assertEqual(caught.exception.code, 'storage_limit')
        for node in ('alpha', 'beta'):
            self.assertEqual(list(Path(self.configs[node]['projects']['first']['import_root']).iterdir()), [])

    async def test_cancel_during_fetch_begin_releases_reservation(self):
        await self.create()
        manager = self.bridges['alpha'].artifact_transfers
        data = self.export('beta')
        source = Path(self.configs['beta']['projects']['first']['export_root']) / 'large.bin'
        await self.bridges['beta'].artifact_transfers.archive(self.bridges['beta'].session('session-first'),
            'remote-large', source, len(data), hashlib.sha256(data).hexdigest(), 'large.bin')
        original = manager.begin
        held, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            result = await original(*args)
            held.set()
            await release.wait()
            return result
        manager.begin = delayed
        args = {'session_id': 'session-first', 'request_id': 'fetch-race',
                'artifact_id': 'remote-large', 'destination': 'cancelled.bin'}
        await self.rpc('alpha', 'artifact_fetch', args)
        await asyncio.wait_for(held.wait(), 10)
        try:
            cancelled = await self.rpc('alpha', 'artifact_transfer_cancel', args)
            self.assertEqual(cancelled['status'], 'aborted')
        finally:
            release.set()
        await asyncio.gather(*list(manager.running.values()))
        transfers = self.bridges['alpha'].store.all('artifact_transfers')
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0]['status'], 'aborted')
        self.assertFalse((manager.root / transfers[0]['transfer_id']).exists())

    async def test_atomic_scratch_collision_preserves_owner_file_and_expired_cleanup_is_owned(self):
        await self.create()
        manager = self.bridges['alpha'].artifact_transfers
        root = Path(self.configs['alpha']['projects']['first']['import_root'])
        source = self.root / 'source.txt'
        source.write_bytes(b'new bytes')
        scratch_id = uuid.UUID('12345678-1234-1234-1234-123456789abc')
        collision = root / ('.codex-bridge-' + scratch_id.hex + '.partial')
        collision.write_bytes(b'owner file')
        with patch('codex_bridge.artifacts.uuid.uuid4', return_value=scratch_id):
            with self.assertRaises(FileExistsError):
                manager.atomic_copy(source, root, 'final.bin', len(b'new bytes'), hashlib.sha256(b'new bytes').hexdigest(), 'legacy-tag')
        self.assertEqual(collision.read_bytes(), b'owner file')
        self.assertFalse((root / 'final.bin').exists())
        identity = collision.stat()
        journal = manager.staging / (scratch_id.hex + '.json')
        journal.write_text(json.dumps({'root': str(root), 'relative': collision.name,
            'created_epoch': time.time() - TRANSFER_TTL_SECONDS - 1,
            'device': identity.st_dev, 'inode': identity.st_ino + 1}), encoding='utf-8')
        manager.cleanup_staging()
        self.assertTrue(collision.exists(), 'Mismatched identity must not be deleted')
        record = json.loads(journal.read_text())
        record['inode'] = identity.st_ino
        journal.write_text(json.dumps(record), encoding='utf-8')
        manager.cleanup_staging()
        self.assertFalse(collision.exists())
        self.assertFalse(journal.exists())

    async def test_concurrent_archive_id_collision_cannot_reassign_session(self):
        await self.create()
        await self.create('second', 'session-second')
        bridge = self.bridges['alpha']
        source = self.root / 'same-bytes.txt'
        source.write_bytes(b'same bytes')
        params = ('shared-id', source, len(b'same bytes'), hashlib.sha256(b'same bytes').hexdigest(), 'same-bytes.txt')
        results = await asyncio.gather(
            bridge.artifact_transfers.archive(bridge.session('session-first'), *params),
            bridge.artifact_transfers.archive(bridge.session('session-second'), *params),
            return_exceptions=True)
        self.assertIsInstance(results[0], dict)
        self.assertIsInstance(results[1], BridgeError)
        self.assertEqual(results[1].code, 'duplicate_conflict')
        self.assertEqual(bridge.store.get('artifacts', 'shared-id')['session_id'], 'session-first')

    async def test_inline_cannot_reassign_chunked_claim_during_disk_copy(self):
        await self.create()
        await self.create('second', 'session-second')
        bridge = self.bridges['alpha']
        manager = bridge.artifact_transfers
        source = self.root / 'claim.txt'
        source.write_bytes(b'claim bytes')
        held, release = threading.Event(), threading.Event()
        original = manager.atomic_copy
        def delayed(*args):
            held.set()
            release.wait(10)
            return original(*args)
        manager.atomic_copy = delayed
        pending = asyncio.create_task(manager.archive(bridge.session('session-first'), 'claimed-id', source,
            len(b'claim bytes'), hashlib.sha256(b'claim bytes').hexdigest(), 'claim.txt'))
        try:
            self.assertTrue(await asyncio.to_thread(held.wait, 10))
            with self.assertRaises(BridgeError) as caught:
                bridge.archive_artifact(bridge.session('session-second'), 'claimed-id', b'claim bytes', 'inline.txt')
            self.assertEqual(caught.exception.code, 'duplicate_conflict')
        finally:
            release.set()
        result = await pending
        self.assertEqual(result['session_id'], 'session-first')
        self.assertEqual(bridge.store.get('artifacts', 'claimed-id')['session_id'], 'session-first')

    async def test_inline_orphan_blob_must_match_bytes_before_metadata_commit(self):
        await self.create()
        bridge = self.bridges['alpha']
        blob = bridge.state_dir / 'artifacts' / 'orphan-id'
        blob.write_bytes(b'old bytes')
        with self.assertRaises(BridgeError) as caught:
            bridge.archive_artifact(bridge.session('session-first'), 'orphan-id', b'new bytes', 'new.txt')
        self.assertEqual(caught.exception.code, 'invalid_artifact')
        self.assertIsNone(bridge.store.get('artifacts', 'orphan-id'))
        self.assertEqual(blob.read_bytes(), b'old bytes')


if __name__ == '__main__':
    unittest.main()
