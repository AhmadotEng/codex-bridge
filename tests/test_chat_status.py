"""Ownership and safe views use isolated, real loopback bridge endpoints."""
import json
import unittest
from unittest.mock import AsyncMock

import test_core as fixtures
from codex_bridge.core import BridgeError
from codex_bridge.codex_adapter import AdapterError, CodexAdapter, _Run


class ChatStatusTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CoreIntegrationTests.asyncSetUp
    asyncTearDown = fixtures.CoreIntegrationTests.asyncTearDown
    save_config = fixtures.CoreIntegrationTests.save_config
    rpc = fixtures.CoreIntegrationTests.rpc
    create = fixtures.CoreIntegrationTests.create
    terminal = fixtures.CoreIntegrationTests.terminal
    started = fixtures.CoreIntegrationTests.started

    async def test_local_links_never_substitute_the_peer_conversation(self):
        await self.create()
        first = await self.rpc('alpha', 'session_chat', {'session_id': 'session-first'})
        self.assertEqual(first['ownership'], 'not_started')
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': 'work', 'prompt': 'Read only'})
        await self.terminal('alpha', 'work')
        first = await self.rpc('alpha', 'session_chat', {'session_id': 'session-first'})
        second = await self.rpc('beta', 'session_chat', {'session_id': 'session-first'})
        self.assertIsNone(first['chat_url'])
        self.assertEqual(second['chat_url'], 'codex://threads/beta-thread-1')
        self.assertEqual(second['ownership'], 'release_unconfirmed')
        self.assertFalse(second['can_open'])

    async def test_observed_ownership_and_secret_free_default_view(self):
        await self.create()
        self.adapters['beta'].block = True
        await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': 'working', 'prompt': 'private prompt sentinel'})
        await self.started('beta')
        self.assertEqual((await self.rpc('beta', 'session_chat', {'session_id': 'session-first'}))['ownership'], 'working')
        await self.rpc('alpha', 'task_cancel', {'session_id': 'session-first', 'request_id': 'working'})
        await self.terminal('alpha', 'working')
        bridge = self.bridges['beta']
        task = bridge.store.get('incoming', 'working')
        task['result']['conversation_release'] = 'owned-app-server-closed-after-turn'
        bridge.store.put('incoming', 'working', task)
        status = await self.rpc('beta', 'bridge_status', {})
        self.assertEqual(status['sessions'][0]['ownership'], 'released_to_desktop')
        self.assertTrue(status['sessions'][0]['can_open'])
        raw = json.dumps(status)
        for forbidden in ('private prompt sentinel', str(self.root), 'local-secret', 'alpha-to-beta', 'workspaces', 'progress'):
            self.assertNotIn(forbidden, raw)
        task.update(status='failed', error={'code': 'conversation_in_use', 'message': 'private path sentinel'})
        bridge.store.put('incoming', 'working', task)
        result = await self.rpc('beta', 'session_chat', {'session_id': 'session-first'})
        self.assertEqual(result['ownership'], 'waiting_for_desktop_release')
        self.assertFalse(result['can_open'])
        self.assertNotIn('private path sentinel', json.dumps(result))

    async def test_name_once_and_cosmetic_failure_does_not_repeat_task(self):
        await self.create()
        adapter = self.adapters['beta']
        adapter.set_thread_name = AsyncMock(side_effect=RuntimeError('unsupported cosmetic operation'))
        for request in ('first', 'followup'):
            await self.rpc('alpha', 'task_send', {'session_id': 'session-first', 'request_id': request, 'prompt': 'Read only'})
            self.assertEqual((await self.terminal('alpha', request))['status'], 'completed')
        adapter.set_thread_name.assert_awaited_once_with('beta-thread-1', 'Codex Bridge - first project')
        self.assertEqual(len(adapter.runs), 2)
        self.assertEqual((await self.rpc('beta', 'session_chat', {'session_id': 'session-first'}))['title_status'], 'unavailable')

    async def test_peer_cannot_open_or_inspect_local_status_and_revoked_owner_can(self):
        await self.create()
        for method, params in [('bridge_status', {}), ('session_chat', {'session_id': 'session-first'})]:
            with self.assertRaises(BridgeError) as caught:
                await self.bridges['alpha'].remote('beta', method, params)
            self.assertEqual(caught.exception.code, 'scope_denied')
        await self.rpc('beta', 'peer_revoke', {'peer_id': 'alpha'})
        self.assertEqual((await self.rpc('beta', 'session_chat', {'session_id': 'session-first'}))['session_id'], 'session-first')


class OwnedTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_names_only_owned_thread_without_loading_another(self):
        adapter = CodexAdapter('unused')
        adapter._rpc = AsyncMock(return_value={})
        with self.assertRaises(AdapterError):
            await adapter.set_thread_name('other', 'Wrong')
        adapter._rpc.assert_not_awaited()
        adapter._active['owned'] = _Run('owned')
        await adapter.set_thread_name('owned', 'Sample collaboration')
        adapter._rpc.assert_awaited_once_with('thread/name/set', {'threadId': 'owned', 'name': 'Sample collaboration'})


if __name__ == '__main__':
    unittest.main()
