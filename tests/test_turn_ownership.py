import asyncio
import tempfile
import unittest

from codex_bridge.codex_adapter import TurnScopedCodexAdapter


class SimulatedServer:
    def __init__(self, number):
        self.number = number
        self.closed = False
        self.close_calls = 0
        self.calls = []
        self.ready = asyncio.Event()
        self.finish = asyncio.Event()
        self.thread = None
        self.cancelled = False

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        self.thread = kwargs['thread_id'] or f'thread-{self.number}'
        await kwargs['on_started'](self.thread, f'turn-{self.number}')
        self.ready.set()
        if kwargs['prompt'] == 'wait': await self.finish.wait()
        return {'status': 'interrupted' if self.cancelled else 'completed', 'thread_id': self.thread,
            'turn_id': f'turn-{self.number}', 'text': kwargs['prompt']}

    async def cancel(self, thread_id, turn_id):
        self.cancelled = True; self.finish.set()
        return {'status': 'cancellation_requested'}

    async def close(self):
        self.close_calls += 1
        self.closed = True
        self.finish.set()
        await asyncio.sleep(0)


class TestAdapter(TurnScopedCodexAdapter):
    def __init__(self):
        super().__init__('unused')
        self.created = []
        self.metadata = SimulatedServer(0)

    def _new_worker(self):
        worker = SimulatedServer(len(self.created)+1)
        self.created.append(worker)
        return worker


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = TestAdapter()

    async def asyncTearDown(self):
        await self.adapter.close()
        self.temp.cleanup()

    async def test_completion_releases_process_and_resumes_exact_context_id(self):
        first = await self.adapter.run(self.temp.name, 'first', policy='read-only')
        self.assertTrue(self.adapter.created[0].closed)
        second = await self.adapter.run(self.temp.name, 'followup', thread_id=first['thread_id'], policy='read-only')
        self.assertEqual(first['thread_id'], second['thread_id'])
        self.assertEqual(len(self.adapter.created), 2)
        self.assertTrue(all(worker.closed for worker in self.adapter.created))
        self.assertFalse(self.adapter.by_thread)
        self.assertEqual(self.adapter.created[1].calls[0]['policy'], 'read-only')

    async def test_completed_project_releases_without_stopping_concurrent_project(self):
        ready = asyncio.Event(); ids = []
        async def started(thread, turn): ids.extend([thread, turn]); ready.set()
        pending = asyncio.create_task(self.adapter.run(self.temp.name, 'wait', on_started=started))
        await ready.wait()
        result = await self.adapter.run(self.temp.name, 'independent')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(self.adapter.created[1].closed)
        self.assertFalse(self.adapter.created[0].closed)
        self.assertFalse(pending.done())
        await self.adapter.cancel(*ids)
        cancelled = await pending
        self.assertEqual(cancelled['status'], 'interrupted')
        self.assertTrue(self.adapter.created[0].closed)

    async def test_same_conversation_busy_and_shutdown_do_not_double_close(self):
        ready = asyncio.Event(); ids = []
        async def started(thread, turn): ids.extend([thread, turn]); ready.set()
        pending = asyncio.create_task(self.adapter.run(self.temp.name, 'wait', on_started=started))
        await ready.wait()
        blocked = await self.adapter.run(self.temp.name, 'duplicate', thread_id=ids[0])
        self.assertEqual(blocked['error']['code'], 'thread_busy')
        self.assertEqual(len(self.adapter.created), 1)
        await self.adapter.close()
        await pending
        self.assertEqual(self.adapter.created[0].close_calls, 1)
        self.assertFalse(self.adapter.workers)


if __name__ == '__main__': unittest.main()
