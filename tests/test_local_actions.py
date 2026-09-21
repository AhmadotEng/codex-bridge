import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from codex_bridge.core import BridgeError, Store, digest
from codex_bridge.local_actions import LocalActions, registry
from codex_bridge.codex_adapter import _Run
from test_codex_adapter import FakeAdapter


class OwnerConfig:
    def __init__(self, root):
        self.store = Store(root/'state.sqlite3')
        self.workspace = root/'workspace'; self.workspace.mkdir()
        self.protected = root/'owner-actions'; self.protected.mkdir()
        self.script = self.protected/'action.py'
        self.script.write_text("import json\nfrom pathlib import Path\np=Path('counter.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nprint(json.dumps({'ok':True,'secret':'withheld'}))\n", encoding='utf-8')
        self.action = {'title': 'Harmless fixed test action', 'argv': [str(Path(sys.executable).resolve()), str(self.script)],
            'cwd': str(self.protected), 'guard_files': [{'path': str(self.script), 'sha256': hashlib.sha256(self.script.read_bytes()).hexdigest()}],
            'timeout_seconds': 3, 'max_output_bytes': 1000}
        self.settings = {'workspace': str(self.workspace), 'policy': 'workspace-write', 'local_actions': {'fixed': self.action}}
        self.active = True
        self.store.put('incoming', 'task', {'session_id': 'session', 'status': 'running'})

    def session(self, session_id, operation=None):
        if not self.active: raise BridgeError('access_revoked', 'Revoked')
        return {'session_id': session_id, 'project_id': 'project', 'peer_id': 'peer'}

    def project(self, project_id, peer_id):
        return self.settings


class LocalActionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.owner = OwnerConfig(Path(self.temp.name).resolve())
        self.actions = LocalActions(self.owner)

    async def asyncTearDown(self):
        self.owner.store.db.close()
        self.temp.cleanup()

    async def run_action(self, request_id='once', action_id='fixed', session_id='session'):
        return await self.actions.execute(session_id=session_id, task_id='task', expected_registry=digest(self.owner.settings['local_actions']), action_id=action_id, request_id=request_id)

    def change_script(self, source):
        self.owner.script.write_text(source, encoding='utf-8')
        self.owner.action['guard_files'][0]['sha256'] = hashlib.sha256(self.owner.script.read_bytes()).hexdigest()

    async def test_real_fixed_process_and_durable_no_duplicate_side_effect(self):
        first, concurrent = await asyncio.gather(self.run_action(), self.run_action())
        self.assertEqual(first['status'], 'completed')
        self.assertTrue(concurrent['deduplicated'])
        self.assertEqual(first['output']['secret'], '[redacted]')
        self.actions = LocalActions(self.owner)
        repeated = await self.run_action()
        self.assertEqual(repeated['status'], 'completed')
        self.assertTrue(repeated['deduplicated'])
        self.assertEqual((self.owner.protected/'counter.txt').read_text(), 'x')

    async def test_guard_mismatch_prevents_dispatch(self):
        self.owner.script.write_text('raise Exception("changed")', encoding='utf-8')
        with self.assertRaises(BridgeError) as error:
            await self.run_action()
        self.assertEqual(error.exception.code, 'action_guard_mismatch')
        self.assertIsNone(self.owner.store.get('local_actions', 'once'))

    async def test_actions_cannot_be_mutated_in_worker_workspace(self):
        script = self.owner.workspace/'modifiable.py'
        script.write_text('pass')
        self.owner.action['guard_files'][0]['path'] = str(script)
        with self.assertRaises(BridgeError): registry(self.owner.settings)

    async def test_unknown_action_and_revocation_prevent_dispatch(self):
        with self.assertRaises(BridgeError): await self.run_action(action_id='arbitrary-shell')
        self.owner.active = False
        with self.assertRaises(BridgeError): await self.run_action()
        self.assertFalse((self.owner.protected/'counter.txt').exists())

    async def test_changed_config_cannot_reuse_action_id(self):
        await self.run_action()
        self.owner.action['title'] = 'Changed meaning'
        with self.assertRaises(BridgeError) as error: await self.run_action()
        self.assertEqual(error.exception.code, 'duplicate_conflict')

    async def test_recovery_preserves_uncertain_no_dispatch(self):
        self.owner.store.put('local_actions', 'once', {'request_id': 'once', 'status': 'running',
            'intent_digest': digest({'session_id': 'session', 'action_id': 'fixed', 'registry': digest(self.owner.settings['local_actions'])})})
        self.actions = LocalActions(self.owner)
        result = await self.run_action()
        self.assertEqual(result['status'], 'uncertain')
        self.assertFalse((self.owner.protected/'counter.txt').exists())

    async def test_cancel_stops_only_owned_process_tree_and_does_not_retry(self):
        self.change_script("import time\ntime.sleep(30)\nprint('{}')")
        pending = asyncio.create_task(self.run_action())
        for _ in range(50):
            item = self.owner.store.get('local_actions', 'once')
            if item and item['status'] == 'running': break
            await asyncio.sleep(.01)
        pending.cancel()
        result = await asyncio.wait_for(pending, 5)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual((await self.run_action())['status'], 'cancelled')

    async def test_timeout_and_raw_output_are_not_exposed(self):
        self.owner.action['timeout_seconds'] = 1
        self.change_script("import time\ntime.sleep(30)")
        result = await self.run_action('timeout')
        self.assertEqual(result['error']['code'], 'action_timeout')
        self.change_script("print('sensitive raw diagnostic')")
        result = await self.run_action('raw-output')
        self.assertEqual(result['error']['code'], 'action_output_invalid')
        self.assertNotIn('sensitive', json.dumps(result))

    async def test_cancelled_parent_task_denies_action(self):
        self.owner.store.put('incoming', 'task', {'session_id': 'session', 'status': 'cancel_requested'})
        with self.assertRaises(BridgeError) as error: await self.run_action()
        self.assertEqual(error.exception.code, 'action_cancelled')

    @unittest.skipUnless(os.name == 'nt', 'Windows Job ownership test')
    async def test_windows_job_cancellation_stops_spawned_descendant(self):
        import ctypes
        from ctypes import wintypes
        self.change_script("import subprocess,sys,time\nfrom pathlib import Path\nchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\nPath('child.pid').write_text(str(child.pid))\ntime.sleep(30)\n")
        pending = asyncio.create_task(self.run_action())
        pidfile = self.owner.protected/'child.pid'
        for _ in range(200):
            if pidfile.exists(): break
            await asyncio.sleep(.01)
        self.assertTrue(pidfile.exists())
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, int(pidfile.read_text()))
        self.assertTrue(handle)
        try:
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)  # WAIT_TIMEOUT: alive
            pending.cancel()
            result = await asyncio.wait_for(pending, 5)
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)  # exact process terminated
        finally:
            kernel.CloseHandle(handle)
            if not pending.done():
                pending.cancel(); await pending


class DynamicToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_fixed_name_args_and_active_turn_are_dispatched(self):
        adapter = FakeAdapter()
        calls = []
        async def execute(action_id, request_id):
            calls.append((action_id, request_id)); return {'status': 'completed'}
        state = _Run('thread', turn_id='turn', action_handler=execute, action_ids=frozenset({'fixed'}))
        adapter._active['thread'] = state
        base = {'threadId': 'thread', 'turnId': 'turn', 'tool': 'bridge_local_action', 'arguments': {'action_id': 'fixed', 'request_id': 'unique'}}
        await adapter._deny_server_request({'id': 1, 'method': 'item/tool/call', 'params': base})
        self.assertEqual(calls, [('fixed', 'unique')])
        for update in ({'turnId': 'other'}, {'arguments': {**base['arguments'], 'argv': ['cmd.exe']}}, {'arguments': {'action_id': 'other', 'request_id': 'x'}}, {'arguments': {'action_id': [], 'request_id': 'x'}}, {'namespace': 'other'}):
            await adapter._deny_server_request({'id': 2, 'method': 'item/tool/call', 'params': {**base, **update}})
        self.assertEqual(len(calls), 1)
        state.cancelled = True
        await adapter._deny_server_request({'id': 3, 'method': 'item/tool/call', 'params': base})
        self.assertEqual(len(calls), 1)

    async def test_default_denies_dynamic_tools_and_action_threads_keep_sandbox(self):
        adapter = FakeAdapter()
        await adapter._deny_server_request({'id': 1, 'method': 'item/tool/call', 'params': {'threadId': 'none', 'tool': 'bridge_local_action'}})
        self.assertIn('error', adapter.sent[-1])
        adapter.enable_local_actions = True
        async def execute(*args): return {'status': 'completed'}
        with tempfile.TemporaryDirectory() as workspace:
            result = await adapter.run(workspace, 'hello', local_actions=[{'action_id': 'fixed', 'title': 'Fixed action'}], action_handler=execute)
        self.assertEqual(result['status'], 'completed')
        params = next(p for m, p in adapter.calls if m == 'thread/start')
        self.assertEqual(params['sandbox'], 'read-only')
        self.assertEqual(params['approvalPolicy'], 'never')
        self.assertEqual(params['dynamicTools'][0]['name'], 'bridge_local_action')


if __name__ == '__main__': unittest.main()
