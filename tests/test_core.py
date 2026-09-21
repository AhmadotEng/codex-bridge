"""Core integration tests use real loopback HTTP and a harmless fake Codex adapter."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.request

from codex_bridge.core import Bridge, BridgeError, TERMINAL, canonical, safe_path


class FakeAdapter:
    def __init__(self, label):
        self.label = label
        self.runs = []
        self.cancellations = []
        self.history = {}
        self.block = False
        self.events = {}

    async def start(self):
        pass

    async def close(self):
        for event in self.events.values():
            event.set()

    async def capabilities(self):
        return {'adapter': 'fake', 'version': 'test'}

    async def run(self, *, workspace, prompt, thread_id, on_event, on_started,
                  policy, writable_roots):
        number = len(self.runs) + 1
        thread_id = thread_id or f'{self.label}-thread-{number}'
        turn_id = f'{self.label}-turn-{number}'
        call = {'workspace': workspace, 'prompt': prompt, 'thread_id': thread_id,
                'turn_id': turn_id, 'policy': policy, 'writable_roots': writable_roots}
        self.runs.append(call)
        self.history.setdefault(thread_id, []).append(prompt)
        event = self.events[(thread_id, turn_id)] = asyncio.Event()
        await on_started(thread_id, turn_id)
        await on_event({'kind': 'progress', 'message': f'{self.label} started'})
        if self.block:
            await event.wait()
        await asyncio.sleep(0)
        status = 'interrupted' if (thread_id, turn_id) in self.cancellations else 'completed'
        return {'status': status, 'thread_id': thread_id, 'turn_id': turn_id,
                'text': f'{self.label}: {len(self.history[thread_id])} turns'}

    async def cancel(self, thread_id, turn_id):
        self.cancellations.append((thread_id, turn_id))
        event = self.events.get((thread_id, turn_id))
        if event:
            event.set()
        return {'cancelled': True}


class CoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-bridge-test-')
        self.root = Path(self.temp.name)
        self.bridges = {}
        self.adapters = {}
        self.servers = {}
        self.configs = {}
        for node, other in [('alpha', 'beta'), ('beta', 'alpha')]:
            projects = {}
            for project in ['first', 'second']:
                workspace = self.root / node / project
                workspace.mkdir(parents=True)
                export = workspace / 'export'
                imported = workspace / 'import'
                export.mkdir()
                imported.mkdir()
                projects[project] = {'workspace': str(workspace),
                    'export_root': str(export), 'import_root': str(imported),
                    'allowed_peers': [other], 'allowed_ops': ['tasks', 'messages', 'artifacts', 'context'],
                    'policy': 'read-only'}
            cfg = {'version': 1, 'peer_id': node, 'state_dir': str(self.root / node / 'state'),
                'local_token': 'local-secret-' + node, 'listen_port': 0, 'codex_path': 'unused-fake',
                'peers': {other: {'enabled': True, 'url': 'http://127.0.0.1:1',
                    'incoming_token': f'{other}-to-{node}', 'outgoing_token': f'{node}-to-{other}'}},
                'projects': projects}
            path = self.root / f'{node}.json'
            path.write_text(json.dumps(cfg), encoding='utf-8')
            adapter = self.adapters[node] = FakeAdapter(node)
            bridge = self.bridges[node] = Bridge(path, adapter=adapter)
            self.servers[node] = await asyncio.start_server(bridge.handle_http, '127.0.0.1', 0, limit=16384)
            cfg['listen_port'] = self.servers[node].sockets[0].getsockname()[1]
            self.configs[node] = cfg
        for node, other in [('alpha', 'beta'), ('beta', 'alpha')]:
            self.configs[node]['peers'][other]['url'] = f"http://127.0.0.1:{self.configs[other]['listen_port']}"
            self.save_config(node)

    def save_config(self, node):
        self.bridges[node].config_path.write_text(json.dumps(self.configs[node]), encoding='utf-8')

    async def asyncTearDown(self):
        for server in self.servers.values():
            server.close()
            await server.wait_closed()
        for bridge in self.bridges.values():
            await bridge.adapter.close()
            for task in list(bridge.running.values()):
                task.cancel()
            await asyncio.gather(*list(bridge.running.values()), return_exceptions=True)
            bridge.store.db.close()
        self.temp.cleanup()

    async def rpc(self, node, method, params, *, token=None):
        cfg = self.configs[node]
        def call():
            request = urllib.request.Request(f"http://127.0.0.1:{cfg['listen_port']}/rpc",
                data=canonical({'method': method, 'params': params}).encode(),
                headers={'Authorization': 'Bearer ' + (token or cfg['local_token']),
                         'Content-Type': 'application/json'}, method='POST')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=5) as response:
                result = json.load(response)
            if not result['ok']:
                error = result['error']
                raise BridgeError(error['code'], error['message'], error.get('retryable', False))
            self.assertIn('timestamp', result)
            self.assertIn('request_id', result)
            return result['result']
        return await asyncio.to_thread(call)

    async def create(self, project='first', session='session-first'):
        return await self.rpc('alpha', 'session_create', {
            'peer_id': 'beta', 'project_id': project, 'session_id': session,
            'name': f'{project} project', 'goal': 'Harmless bridge test',
            'context': 'Remember context blue.', 'responsibilities': {'alpha': 'review', 'beta': 'test'}})

    async def terminal(self, node, request_id, session='session-first'):
        for _ in range(200):
            result = await self.rpc(node, 'task_status', {'session_id': session, 'request_id': request_id})
            if result['status'] in TERMINAL:
                return result
            await asyncio.sleep(.01)
        self.fail(f'Task {request_id} did not reach a terminal state: {result}')

    async def started(self, node, count=1):
        for _ in range(200):
            if len(self.adapters[node].runs) >= count:
                return
            await asyncio.sleep(.01)
        self.fail(f'{node} fake adapter did not start {count} runs')

    async def test_availability_bidirectional_tasks_and_retained_context(self):
        for node in ['alpha', 'beta']:
            result = await self.rpc(node, 'peer_status', {})
            self.assertTrue(result['peers'][0]['available'])
            self.assertIn('durable_dedup', result['peers'][0]['capabilities'])
        created = await self.create()
        self.assertEqual(created['status'], 'ready')
        self.assertEqual(set(created['workspaces']), {'alpha', 'beta'})
        for node, request in [('alpha', 'a-first'), ('beta', 'b-first'), ('alpha', 'a-followup')]:
            await self.rpc(node, 'task_send', {'session_id': 'session-first', 'request_id': request, 'prompt': request})
            result = await self.terminal(node, request)
            self.assertEqual(result['status'], 'completed')
            self.assertIn('timestamp', result['progress'][0])
        self.assertEqual(len(self.adapters['alpha'].runs), 1)
        self.assertEqual(len(self.adapters['beta'].runs), 2)
        first, second = self.adapters['beta'].runs
        self.assertEqual(first['thread_id'], second['thread_id'])
        self.assertIn('Remember context blue.', second['prompt'])
        session = await self.rpc('alpha', 'session_get', {'session_id': 'session-first'})
        self.assertTrue(all(session['conversation_ids'].values()))

    async def test_dedup_persists_after_restart_and_rejects_different_scope(self):
        await self.create()
        await self.create('second', 'session-second')
        params = {'session_id': 'session-first', 'request_id': 'durable-1', 'prompt': 'Execute once'}
        await self.rpc('alpha', 'task_send', params)
        await self.terminal('alpha', 'durable-1')
        old = self.bridges['beta']
        self.servers['beta'].close()
        await self.servers['beta'].wait_closed()
        await asyncio.gather(*list(old.running.values()), return_exceptions=True)
        old.store.db.close()
        replacement = self.bridges['beta'] = Bridge(old.config_path, adapter=self.adapters['beta'])
        self.servers['beta'] = await asyncio.start_server(replacement.handle_http, '127.0.0.1', self.configs['beta']['listen_port'])
        repeated = await self.bridges['alpha'].remote('beta', 'peer.task_accept', params)
        self.assertEqual(repeated['status'], 'completed')
        self.assertEqual(len(self.adapters['beta'].runs), 1)
        for conflicting in [{**params, 'prompt': 'Changed'}, {**params, 'session_id': 'session-second'}]:
            with self.assertRaises(BridgeError) as caught:
                await self.bridges['alpha'].remote('beta', 'peer.task_accept', conflicting)
            self.assertEqual(caught.exception.code, 'duplicate_conflict')

    async def test_lost_accept_response_retry_does_not_duplicate_execution(self):
        await self.create()
        bridge = self.bridges['alpha']
        remote = bridge.remote
        lost = False
        async def unreliable(peer_id, method, params):
            nonlocal lost
            result = await remote(peer_id, method, params)
            if method == 'peer.task_accept' and not lost:
                lost = True
                raise BridgeError('peer_unavailable', 'Injected lost acceptance response', True)
            return result
        bridge.remote = unreliable
        params = {'session_id': 'session-first', 'request_id': 'lost-reply', 'prompt': 'Execute once after dropped reply'}
        first = await self.rpc('alpha', 'task_send', params)
        self.assertEqual(first['status'], 'pending_delivery')
        second = await self.rpc('alpha', 'task_send', params)
        final = await self.terminal('alpha', 'lost-reply')
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(len(self.adapters['beta'].runs), 1)

    async def test_transport_disconnect_retains_pending_request_for_retry(self):
        await self.create()
        server = self.servers['beta']
        server.close()
        await server.wait_closed()
        params = {'session_id': 'session-first', 'request_id': 'offline', 'prompt': 'Wait for route'}
        result = await self.rpc('alpha', 'task_send', params)
        self.assertEqual(result['status'], 'pending_delivery')
        self.assertTrue(result['error']['retryable'])
        self.assertEqual(self.adapters['beta'].runs, [])
        self.servers['beta'] = await asyncio.start_server(self.bridges['beta'].handle_http, '127.0.0.1', self.configs['beta']['listen_port'])
        await self.rpc('alpha', 'task_send', params)
        self.assertEqual((await self.terminal('alpha', 'offline'))['status'], 'completed')
        self.assertEqual(len(self.adapters['beta'].runs), 1)

    async def test_two_concurrent_projects_use_separate_threads_and_workspaces(self):
        await self.create()
        await self.create('second', 'session-second')
        await asyncio.gather(*[self.rpc('alpha', 'task_send', {'session_id': session,
            'request_id': 'isolated-' + session, 'prompt': 'Read only this project'})
            for session in ['session-first', 'session-second']])
        await asyncio.gather(*[self.terminal('alpha', 'isolated-' + session, session)
            for session in ['session-first', 'session-second']])
        first, second = self.adapters['beta'].runs
        self.assertNotEqual(first['thread_id'], second['thread_id'])
        self.assertNotEqual(first['workspace'], second['workspace'])
        for run in self.adapters['beta'].runs:
            self.assertEqual(run['policy'], 'read-only')
            self.assertEqual(run['writable_roots'], [])
        with self.assertRaises(BridgeError) as caught:
            await self.rpc('beta', 'task_status', {'session_id': 'session-second', 'request_id': 'isolated-session-first'})
        self.assertEqual(caught.exception.code, 'task_not_found')
        for node in ['alpha', 'beta']:
            for session_id, project_id in [('session-first', 'first'), ('session-second', 'second')]:
                view = await self.rpc(node, 'session_get', {'session_id': session_id})
                self.assertEqual(set(view['transfer_locations']), {'alpha', 'beta'})
                for owner in ['alpha', 'beta']:
                    expected = self.configs[owner]['projects'][project_id]
                    self.assertEqual(view['transfer_locations'][owner],
                        {key: expected[key] for key in ['export_root', 'import_root']})
                    for root in view['transfer_locations'][owner].values():
                        self.assertTrue(Path(root).is_relative_to(Path(expected['workspace'])))
                        other_project = 'second' if project_id == 'first' else 'first'
                        self.assertFalse(Path(root).is_relative_to(
                            Path(self.configs[owner]['projects'][other_project]['workspace'])))

    async def test_workspace_change_requires_new_session_and_new_conversation(self):
        await self.create()
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first',
            'request_id': 'before-workspace-change', 'prompt': 'Remember original workspace'})
        await self.terminal('alpha', 'before-workspace-change')
        previous_thread = self.adapters['beta'].runs[0]['thread_id']
        for node in ['alpha', 'beta']:
            workspace = self.root / node / 'replacement-workspace'
            workspace.mkdir()
            export = workspace / 'export'
            imported = workspace / 'import'
            export.mkdir()
            imported.mkdir()
            self.configs[node]['projects']['first'].update(workspace=str(workspace),
                export_root=str(export), import_root=str(imported))
            self.save_config(node)
        with self.assertRaises(BridgeError) as caught:
            await self.rpc('alpha', 'task_send', {'session_id': 'session-first',
                'request_id': 'reject-rebound-workspace', 'prompt': 'Cannot silently reuse this conversation'})
        self.assertEqual(caught.exception.code, 'scope_changed')
        self.assertIsNone(self.bridges['alpha'].store.get('outgoing', 'reject-rebound-workspace'))
        self.assertEqual(len(self.adapters['beta'].runs), 1)
        created = await self.create('first', 'session-replacement')
        for node in ['alpha', 'beta']:
            self.assertEqual(Path(created['workspaces'][node]).resolve(),
                Path(self.configs[node]['projects']['first']['workspace']).resolve())
        await self.rpc('alpha', 'task_send', {'session_id': 'session-replacement',
            'request_id': 'new-workspace-task', 'prompt': 'Use the newly selected workspace'})
        result = await self.terminal('alpha', 'new-workspace-task', 'session-replacement')
        self.assertEqual(result['status'], 'completed')
        newest = self.adapters['beta'].runs[-1]
        self.assertEqual(newest['workspace'], self.configs['beta']['projects']['first']['workspace'])
        self.assertNotEqual(newest['thread_id'], previous_thread)

    async def test_binary_artifact_send_fetch_hash_and_project_isolation(self):
        await self.create()
        await self.create('second', 'session-second')
        data = bytes(range(256)) * 513 + b'\x00\xff\x80'
        exported = Path(self.configs['alpha']['projects']['first']['export_root']) / 'sample.bin'
        exported.write_bytes(data)
        offered = await self.rpc('alpha', 'artifact_send', {'session_id': 'session-first',
            'request_id': 'binary-offer', 'path': 'sample.bin', 'destination': 'nested/received.bin'})
        self.assertEqual(offered['sha256'], hashlib.sha256(data).hexdigest())
        received = Path(self.configs['beta']['projects']['first']['import_root']) / 'nested/received.bin'
        self.assertEqual(received.read_bytes(), data)
        fetched = await self.rpc('alpha', 'artifact_fetch', {'session_id': 'session-first',
            'request_id': 'binary-fetch', 'artifact_id': offered['artifact_id'], 'destination': 'roundtrip.bin'})
        self.assertEqual(Path(fetched['path']).read_bytes(), data)
        with self.assertRaises(BridgeError) as caught:
            await self.rpc('alpha', 'artifact_fetch', {'session_id': 'session-second',
                'request_id': 'wrong-project-fetch', 'artifact_id': offered['artifact_id'], 'destination': 'bad.bin'})
        self.assertEqual(caught.exception.code, 'artifact_not_found')

    async def test_transfer_rejects_traversal_and_windows_aliases(self):
        await self.create()
        source = Path(self.configs['alpha']['projects']['first']['export_root']) / 'ok.txt'
        source.write_text('Allowed selected file', encoding='utf-8')
        for index, destination in enumerate(['../bad.txt', '/absolute.txt', 'C:\\bad.txt',
                'sub/../../bad.txt', 'sub\\..\\bad.txt', 'NUL', 'CON.txt', 'tail. ', 'stream:secret']):
            with self.subTest(destination=destination):
                with self.assertRaises(BridgeError) as caught:
                    await self.rpc('alpha', 'artifact_send', {'session_id': 'session-first',
                        'request_id': f'bad-path-{index}', 'path': 'ok.txt', 'destination': destination})
                self.assertEqual(caught.exception.code, 'path_denied')

    async def test_transfer_rejects_symlink_source_and_destination(self):
        await self.create()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('Never export me', encoding='utf-8')
        export = Path(self.configs['alpha']['projects']['first']['export_root'])
        imported = Path(self.configs['beta']['projects']['first']['import_root'])
        try:
            (export / 'link').symlink_to(outside, target_is_directory=True)
            (imported / 'link').symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Symlinks unavailable in this environment: {exc}')
        (export / 'ok.txt').write_text('Allowed', encoding='utf-8')
        for request, path, dest in [('source-link', 'link/secret.txt', 'ok.txt'),
                                    ('destination-link', 'ok.txt', 'link/placed.txt')]:
            with self.assertRaises(BridgeError) as caught:
                await self.rpc('alpha', 'artifact_send', {'session_id': 'session-first',
                    'request_id': request, 'path': path, 'destination': dest})
            self.assertEqual(caught.exception.code, 'path_denied')
        self.assertFalse((outside / 'placed.txt').exists())

    async def test_cancellation_interrupts_running_request(self):
        await self.create()
        self.adapters['beta'].block = True
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': 'cancel-me', 'prompt': 'Wait'})
        await self.started('beta')
        await self.rpc('alpha', 'task_cancel', {'session_id': 'session-first', 'request_id': 'cancel-me'})
        result = await self.terminal('alpha', 'cancel-me')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(self.adapters['beta'].cancellations), 1)

    async def test_revocation_denies_peer_and_cancels_running_request(self):
        await self.create()
        self.adapters['beta'].block = True
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': 'revoke-me', 'prompt': 'Wait'})
        await self.started('beta')
        result = await self.rpc('beta', 'peer_revoke', {'peer_id': 'alpha'})
        self.assertTrue(result['revoked'])
        self.assertIn('revoke-me', result['cancelled_requests'])
        self.assertEqual(len(self.adapters['beta'].cancellations), 1)
        with self.assertRaises(BridgeError) as caught:
            await self.bridges['alpha'].remote('beta', 'peer.status', {})
        self.assertEqual(caught.exception.code, 'unauthorized')
        self.assertFalse(self.bridges['beta'].config()['peers']['alpha']['enabled'])

    async def test_revoking_pending_cancellation_keeps_maintenance_alive(self):
        await self.create()
        self.adapters['beta'].block = True
        params = {'session_id': 'session-first', 'request_id': 'cancel-offline', 'prompt': 'Wait'}
        await self.rpc('alpha', 'task_send', params)
        await self.started('beta')
        self.servers['beta'].close()
        await self.servers['beta'].wait_closed()
        cancelled = await self.rpc('alpha', 'task_cancel', {'session_id': 'session-first', 'request_id': 'cancel-offline'})
        self.assertEqual(cancelled['status'], 'cancel_pending')
        await self.rpc('alpha', 'peer_revoke', {'peer_id': 'beta'})
        worker = asyncio.create_task(self.bridges['alpha'].maintenance())
        try:
            await asyncio.sleep(.05)
            self.assertFalse(worker.done(), repr(worker.exception()) if worker.done() else '')
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_status_poll_after_reconnect_preserves_cancellation_intent(self):
        await self.create()
        self.adapters['beta'].block = True
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first',
            'request_id': 'cancel-reconnect', 'prompt': 'Wait until cancelled'})
        await self.started('beta')
        self.servers['beta'].close()
        await self.servers['beta'].wait_closed()
        query = {'session_id': 'session-first', 'request_id': 'cancel-reconnect'}
        response = await self.rpc('alpha', 'task_cancel', query)
        self.assertEqual(response['status'], 'cancel_pending')
        self.servers['beta'] = await asyncio.start_server(self.bridges['beta'].handle_http,
            '127.0.0.1', self.configs['beta']['listen_port'])
        await self.rpc('alpha', 'task_status', query)
        persisted = self.bridges['alpha'].store.get('outgoing', 'cancel-reconnect')
        self.assertIn(persisted['status'], {'cancel_pending', 'cancel_requested', 'cancelled'})

    async def test_peer_credentials_cannot_invoke_local_admin_or_other_scope(self):
        await self.create()
        for method in ['shutdown', 'peer_revoke', 'diagnostics', 'task_send', 'session_list']:
            with self.subTest(method=method):
                with self.assertRaises(BridgeError) as caught:
                    await self.rpc('beta', method, {'peer_id': 'alpha'}, token='alpha-to-beta')
                self.assertEqual(caught.exception.code, 'scope_denied')
        self.configs['beta']['projects']['second']['allowed_peers'] = []
        self.save_config('beta')
        with self.assertRaises(BridgeError) as caught:
            await self.create('second', 'scope-denied')
        self.assertEqual(caught.exception.code, 'scope_denied')

    async def test_restart_marks_started_execution_uncertain_without_replaying(self):
        await self.create()
        old = self.bridges['beta']
        for index, status in enumerate(['starting', 'running', 'cancel_requested']):
            old.store.put('incoming', f'restart-{index}', {'request_id': f'restart-{index}',
                'session_id': 'session-first', 'peer_id': 'alpha', 'status': status})
        replacement = Bridge(old.config_path, adapter=FakeAdapter('replacement'))
        try:
            for task in replacement.store.all('incoming'):
                self.assertEqual(task['status'], 'uncertain')
                self.assertFalse(task['error']['retryable'])
            self.assertFalse(replacement.running)
        finally:
            replacement.store.db.close()

    async def test_context_update_is_applied_to_followup_and_deduplicated(self):
        await self.create()
        params = {'session_id': 'session-first', 'request_id': 'context-1',
            'expected_revision': 1, 'context': 'Remember context green.'}
        first = await self.rpc('alpha', 'session_context_update', params)
        self.assertEqual(first['revision'], 2)
        second = await self.rpc('alpha', 'session_context_update', params)
        self.assertEqual(first, second)
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': 'green-task', 'prompt': 'Use context'})
        await self.terminal('alpha', 'green-task')
        self.assertIn('Remember context green.', self.adapters['beta'].runs[0]['prompt'])

    async def test_followup_message_task_is_inspectable_from_sender(self):
        await self.create()
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': 'initial', 'prompt': 'Remember number 7'})
        await self.terminal('alpha', 'initial')
        reply = await self.rpc('alpha', 'message_send', {'session_id': 'session-first',
            'request_id': 'followup-message', 'kind': 'question', 'text': 'What number?', 'continue_conversation': True})
        self.assertIn('task', reply)
        result = await self.terminal('alpha', 'followup-message')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.adapters['beta'].runs[0]['thread_id'], self.adapters['beta'].runs[1]['thread_id'])

    async def test_opposite_direction_context_updates_do_not_diverge(self):
        await self.create()
        gate = asyncio.Event()
        entered = 0
        for node in ['alpha', 'beta']:
            original = self.bridges[node].remote
            async def simultaneous(peer_id, method, params, original=original):
                nonlocal entered
                if method in ('peer.context_update', 'peer.context_sync'):
                    entered += 1
                    if entered == 2:
                        gate.set()
                    await asyncio.wait_for(gate.wait(), 3)
                return await original(peer_id, method, params)
            self.bridges[node].remote = simultaneous
        responses = await asyncio.gather(*[self.rpc(node, 'session_context_update', {
            'session_id': 'session-first', 'request_id': 'simultaneous-' + node,
            'expected_revision': 1, 'context': node + ' context'}) for node in ['alpha', 'beta']],
            return_exceptions=True)
        successes = [response for response in responses if isinstance(response, dict)]
        conflicts = [response for response in responses if isinstance(response, BridgeError)]
        self.assertEqual(len(successes), 1, responses)
        self.assertEqual(len(conflicts), 1, responses)
        self.assertEqual(conflicts[0].code, 'revision_conflict')
        views = [await self.rpc(node, 'session_get', {'session_id': 'session-first'}) for node in ['alpha', 'beta']]
        self.assertEqual((views[0]['revision'], views[0]['context']), (views[1]['revision'], views[1]['context']), responses)

    async def test_lost_context_reply_retry_commits_one_revision(self):
        await self.create()
        bridge = self.bridges['beta']
        remote = bridge.remote
        lost = False
        async def unreliable(peer_id, method, params):
            nonlocal lost
            result = await remote(peer_id, method, params)
            if method == 'peer.context_update' and not lost:
                lost = True
                raise BridgeError('peer_unavailable', 'Injected lost context response', True)
            return result
        bridge.remote = unreliable
        params = {'session_id': 'session-first', 'request_id': 'context-lost',
            'expected_revision': 1, 'context': 'Commit this context once.'}
        with self.assertRaises(BridgeError) as caught:
            await self.rpc('beta', 'session_context_update', params)
        self.assertTrue(caught.exception.retryable)
        result = await self.rpc('beta', 'session_context_update', params)
        self.assertEqual(result['revision'], 2)
        for node in ['alpha', 'beta']:
            view = await self.rpc(node, 'session_get', {'session_id': 'session-first'})
            self.assertEqual(view['revision'], 2)
            self.assertEqual(view['context'], 'Commit this context once.')

    async def test_session_create_retry_after_context_update_is_idempotent(self):
        await self.create()
        await self.rpc('alpha', 'session_context_update', {'session_id': 'session-first',
            'request_id': 'create-retry-context', 'expected_revision': 1, 'context': 'Latest context'})
        repeated = await self.create()
        self.assertEqual(repeated['revision'], 2)
        self.assertEqual(repeated['context'], 'Latest context')
        self.assertEqual(len(self.bridges['beta'].store.all('sessions')), 1)

    async def test_context_replication_recovers_before_followup_execution(self):
        await self.create()
        bridge = self.bridges['alpha']
        remote = bridge.remote
        dropped = False
        async def lose_context_sync(peer_id, method, params):
            nonlocal dropped
            if method == 'peer.context_sync' and not dropped:
                dropped = True
                raise BridgeError('peer_unavailable', 'Injected unavailable replication route', True)
            return await remote(peer_id, method, params)
        bridge.remote = lose_context_sync
        update = await self.rpc('alpha', 'session_context_update', {'session_id': 'session-first',
            'request_id': 'replication-pending', 'expected_revision': 1, 'context': 'Latest context after reconnect'})
        self.assertTrue(update.get('replication_pending'))
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': 'after-replication-failure',
            'prompt': 'Use the current shared context'})
        self.assertEqual((await self.terminal('alpha', 'after-replication-failure'))['status'], 'completed')
        self.assertIn('Latest context after reconnect', self.adapters['beta'].runs[0]['prompt'])

    async def test_cancel_during_inflight_delivery_prevents_late_execution(self):
        await self.create()
        bridge = self.bridges['alpha']
        remote = bridge.remote
        accepting = asyncio.Event()
        release = asyncio.Event()
        self.adapters['beta'].block = True
        async def delayed_accept(peer_id, method, params):
            if method == 'peer.task_accept':
                accepting.set()
                await release.wait()
            return await remote(peer_id, method, params)
        bridge.remote = delayed_accept
        params = {'session_id': 'session-first', 'request_id': 'cancel-during-send', 'prompt': 'This pending request gets cancelled'}
        sender = asyncio.create_task(self.rpc('alpha', 'task_send', params))
        await asyncio.wait_for(accepting.wait(), 2)
        cancellation = asyncio.create_task(self.rpc('alpha', 'task_cancel', {
            'session_id': 'session-first', 'request_id': 'cancel-during-send'}))
        await asyncio.sleep(.05)
        release.set()
        await asyncio.gather(sender, cancellation)
        for _ in range(100):
            incoming = self.bridges['beta'].store.get('incoming', 'cancel-during-send')
            outgoing = self.bridges['alpha'].store.get('outgoing', 'cancel-during-send')
            if incoming and incoming['status'] in TERMINAL:
                break
            await asyncio.sleep(.01)
        if incoming:
            self.assertEqual(incoming['status'], 'cancelled')
        self.assertIn(outgoing['status'], {'cancelled', 'cancel_requested', 'cancel_pending'})

    async def test_existing_artifact_id_cannot_change_its_bytes(self):
        await self.create()
        def payload(request, data, destination):
            return {'session_id': 'session-first', 'request_id': request, 'artifact_id': 'stable-artifact',
                'sha256': hashlib.sha256(data).hexdigest(), 'data': base64.b64encode(data).decode(),
                'destination': destination}
        await self.bridges['alpha'].remote('beta', 'peer.artifact_offer', payload('offer-one', b'first', 'one.bin'))
        with self.assertRaises(BridgeError):
            await self.bridges['alpha'].remote('beta', 'peer.artifact_offer', payload('offer-two', b'changed', 'two.bin'))
        original = await self.bridges['alpha'].remote('beta', 'peer.artifact_get', {'session_id': 'session-first', 'artifact_id': 'stable-artifact'})
        self.assertEqual(base64.b64decode(original['data']), b'first')
        self.assertEqual(original['sha256'], hashlib.sha256(b'first').hexdigest())


if __name__ == '__main__':
    unittest.main()
