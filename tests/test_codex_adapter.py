import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge.codex_adapter import AdapterError, CodexAdapter, _Run, MAX_EXECUTION_ITEMS, MAX_EXECUTION_TRACKED


class FakeAdapter(CodexAdapter):
    def __init__(self):
        super().__init__("unused", run_timeout=1)
        self.calls = []
        self.sent = []
        self.serial = 0

    async def start(self):
        self._version = "0.153.4"
        self._initialized = {"platformOs": "windows"}

    async def _send(self, value):
        self.sent.append(value)

    async def _rpc(self, method, params):
        self.calls.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            self.serial += 1
            return {"thread": {"id": params.get("threadId", f"thread-{self.serial}")}, "sandbox": {"type": "readOnly" if params["sandbox"] == "read-only" else "workspaceWrite"}, "approvalPolicy": params["approvalPolicy"]}
        if method == "mcpServerStatus/list":
            return {"data": [], "nextCursor": None}
        if method == "turn/start":
            thread = params["threadId"]
            turn = f"turn-{self.serial}"
            self._notification("turn/started", {"threadId": thread, "turn": {"id": turn, "status": "inProgress"}})
            text = params["input"][0]["text"]
            if text != "wait":
                self._notification("item/completed", {"threadId": thread, "turnId": turn, "item": {"id": "reasoning", "type": "reasoning", "text": "hidden"}})
                self._notification("item/completed", {"threadId": thread, "turnId": turn, "item": {"id": "wrong", "type": "agentMessage", "text": "Working", "phase": "commentary"}})
                self._notification("item/completed", {"threadId": thread, "turnId": "other-turn", "item": {"id": "wrong", "type": "agentMessage", "text": "wrong project"}})
                self._notification("item/completed", {"threadId": thread, "turnId": turn, "item": {"id": "answer", "type": "agentMessage", "text": text, "phase": "final_answer"}})
                self._notification("turn/completed", {"threadId": thread, "turn": {"id": turn, "status": "completed", "error": None}})
            # All events above precede the request response by design.
            return {"turn": {"id": turn, "status": "inProgress"}}
        if method == "turn/interrupt":
            self._notification("turn/completed", {"threadId": params["threadId"], "turn": {"id": params["turnId"], "status": "interrupted", "error": None}})
            return {}
        raise AssertionError(method)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = str(Path(self.temp.name).resolve())
        self.adapter = FakeAdapter()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_early_notifications_and_final_only(self):
        events, starts = [], []
        async def event(value):
            events.append(value)
        async def started(thread_id, turn_id):
            starts.append((thread_id, turn_id))
        result = await self.adapter.run(self.workspace, "hello", on_event=event, on_started=started)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["text"], "hello")
        self.assertEqual(starts, [(result["thread_id"], result["turn_id"])])
        self.assertNotIn("hidden", str(events))
        self.assertNotIn("wrong project", str(events))

    async def test_resume_retains_designated_id_without_list_or_model_override(self):
        first = await self.adapter.run(self.workspace, "first")
        second = await self.adapter.run(self.workspace, "followup", thread_id=first["thread_id"])
        self.assertEqual(first["thread_id"], second["thread_id"])
        methods = [call[0] for call in self.adapter.calls]
        self.assertEqual(methods, ["thread/start", "mcpServerStatus/list", "turn/start", "thread/resume", "mcpServerStatus/list", "turn/start"])
        for method, params in self.adapter.calls:
            self.assertNotIn("model", params)
            if method != "mcpServerStatus/list":
                self.assertEqual(params.get("approvalPolicy"), "never")

    async def test_cancellation_and_independent_projects(self):
        running = asyncio.Event()
        ids = []
        async def started(thread_id, turn_id):
            ids.extend([thread_id, turn_id])
            running.set()
        task = asyncio.create_task(self.adapter.run(self.workspace, "wait", on_started=started))
        await running.wait()
        other = await self.adapter.run(self.workspace, "independent")
        self.assertEqual(other["text"], "independent")
        self.assertNotEqual(other["thread_id"], ids[0])
        cancelled = await self.adapter.cancel(*ids)
        self.assertEqual(cancelled["status"], "cancellation_requested")
        result = await task
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual((await self.adapter.cancel(*ids))["status"], "not_running")

    async def test_busy_thread_does_not_interrupt_first(self):
        running = asyncio.Event()
        ids = []
        async def started(thread_id, turn_id):
            ids.extend([thread_id, turn_id])
            running.set()
        task = asyncio.create_task(self.adapter.run(self.workspace, "wait", on_started=started))
        await running.wait()
        result = await self.adapter.run(self.workspace, "second", thread_id=ids[0])
        self.assertEqual(result["error"]["code"], "thread_busy")
        self.assertFalse(task.done())
        await self.adapter.cancel(*ids)
        await task

    async def test_restricted_policy_and_invalid_roots(self):
        for policy in ("danger-full-access", "externalSandbox"):
            with self.assertRaises(AdapterError):
                await self.adapter.run(self.workspace, "x", policy=policy)
        with self.assertRaises(AdapterError):
            await self.adapter.run(self.workspace, "x", policy="workspace-write", writable_roots=[str(Path(self.workspace).parent)])
        with self.assertRaises(AdapterError):
            await self.adapter.run(self.workspace, "x", writable_roots=[self.workspace])
        result = await self.adapter.run(self.workspace, "x", policy="workspace-write")
        self.assertEqual(result["status"], "completed")
        policy = self.adapter.calls[-1][1]["sandboxPolicy"]
        self.assertFalse(policy["networkAccess"])
        self.assertTrue(policy["excludeTmpdirEnvVar"])

    async def test_approval_and_credential_requests_are_denied(self):
        self.adapter._active["thread"] = _Run("thread")
        methods = ["item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval", "item/tool/requestUserInput", "mcpServer/elicitation/request", "account/chatgptAuthTokens/refresh", "unknown/newApproval"]
        for ident, method in enumerate(methods):
            await self.adapter._deny_server_request({"id": ident, "method": method, "params": {"threadId": "thread"}})
        self.assertEqual(self.adapter.sent[0]["result"]["decision"], "decline")
        self.assertEqual(self.adapter.sent[2]["result"]["permissions"], {})
        self.assertEqual(self.adapter.sent[4]["result"]["action"], "decline")
        self.assertIn("error", self.adapter.sent[5])
        self.assertIn("error", self.adapter.sent[6])
        self.assertEqual(len(self.adapter._active["thread"].blocked), len(methods))

    async def test_timeout_interrupts_no_automatic_reexecution(self):
        self.adapter.run_timeout = 0.01
        result = await self.adapter.run(self.workspace, "wait")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "run_timeout")
        self.assertEqual([c[0] for c in self.adapter.calls].count("turn/start"), 1)
        self.assertIn("turn/interrupt", [c[0] for c in self.adapter.calls])

    async def test_progress_is_bounded_and_terminal_preserved(self):
        state = _Run("thread", turn_id="turn")
        self.adapter._active["thread"] = state
        for ident in range(700):
            self.adapter._notification("item/started", {"threadId": "thread", "turnId": "turn", "item": {"id": str(ident), "type": "commandExecution"}})
        self.adapter._notification("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}})
        self.assertEqual(state.queue.qsize(), 512)
        self.assertEqual(state.terminal["status"], "completed")
        self.assertGreater(state.dropped_events, 0)

    async def test_integration_scope_uses_names_only_and_disables_defaults(self):
        codex_home = Path(self.workspace) / "isolated-codex-config"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('''
model = "configured-model-stays-local"
[mcp_servers.personal]
command = "private-tool"
[mcp_servers.personal.env]
EXAMPLE_SECRET = "never-copy-this-sentinel"
[mcp_servers.codex_bridge]
command = "bridge-tool"
[plugins."other@personal"]
enabled = true
[plugins."codex-bridge@personal"]
enabled = true
''')
        with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
            scoped = self.adapter._integration_overrides(self.workspace)
            self.assertFalse(scoped["mcp_servers.personal.enabled"])
            self.assertFalse(scoped["mcp_servers.codex_bridge.enabled"])
            self.assertFalse(scoped["features.plugins"])
            self.assertFalse(scoped["features.apps"])
            self.assertFalse(scoped["features.hooks"])
            self.assertNotIn("never-copy-this-sentinel", str(scoped))
            self.assertNotIn("model", scoped)
            allowlisted = CodexAdapter("unused", allowed_mcp_servers=["codex_bridge"], allowed_plugins=["codex-bridge@personal"])
            scoped = allowlisted._integration_overrides(self.workspace)
            self.assertTrue(scoped["mcp_servers.codex_bridge.enabled"])
            self.assertFalse(scoped["mcp_servers.personal.enabled"])
            self.assertTrue(scoped["plugins.codex-bridge@personal.enabled"])
            self.assertFalse(scoped["plugins.other@personal.enabled"])

    async def test_unapproved_live_integration_prevents_turn(self):
        original = self.adapter._rpc
        async def rpc(method, params):
            if method == "mcpServerStatus/list":
                return {"data": [{"name": "unexpected", "tools": {"send_email": {}}}]}
            return await original(method, params)
        self.adapter._rpc = rpc
        result = await self.adapter.run(self.workspace, "hello")
        self.assertEqual(result["error"]["code"], "integration_scope_mismatch")
        self.assertNotIn("turn/start", [m for m,p in self.adapter.calls])

    async def test_loaded_policy_change_fails_with_actionable_error(self):
        original = self.adapter._rpc
        async def rpc(method, params):
            result = await original(method, params)
            if method == "thread/resume":
                result["sandbox"] = {"type": "readOnly", "networkAccess": False}
            return result
        self.adapter._rpc = rpc
        result = await self.adapter.run(self.workspace, "write", thread_id="owned-thread", policy="workspace-write")
        self.assertEqual(result["error"]["code"], "policy_change_requires_new_session")
        self.assertNotIn("turn/start", [m for m,p in self.adapter.calls])

    async def test_close_is_bounded_when_child_retains_pipe(self):
        class Transport:
            closed = False
            def close(self): self.closed = True
        class Stdin:
            def close(self): pass
        class Process:
            returncode = None
            terminated = killed = False
            stdin = Stdin()
            _transport = Transport()
            async def wait(self): await asyncio.Event().wait()
            def terminate(self): self.terminated = True
            def kill(self): self.killed = True
        process = Process()
        self.adapter._proc = process
        real_wait = asyncio.wait_for
        async def short_wait(future, timeout):
            return await real_wait(future, 0.005)
        with patch("codex_bridge.codex_adapter.asyncio.wait_for", new=short_wait):
            await self.adapter.close()
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertTrue(process._transport.closed)
        self.assertIsNone(self.adapter._proc)

    @staticmethod
    def command_item(item_id='exec-proof', **changes):
        return {'id': item_id, 'type': 'commandExecution', 'status': 'completed',
                'exitCode': 0, 'durationMs': 284, 'command': 'PRIVATE-COMMAND-SENTINEL',
                'cwd': 'PRIVATE-PATH-SENTINEL', 'aggregatedOutput': 'PRIVATE-OUTPUT-SENTINEL',
                'env': {'PRIVATE_ENV': 'PRIVATE-TOKEN-SENTINEL'}, **changes}

    async def test_native_evidence_uses_completed_items_and_never_agent_claims(self):
        original = self.adapter._rpc
        async def rpc(method, params):
            result = await original(method, params)
            if method == 'turn/start':
                self.adapter._notification('item/completed', {'threadId': params['threadId'],
                    'turnId': result['turn']['id'], 'item': self.command_item()})
            return result
        self.adapter._rpc = rpc
        events = []
        async def event(value): events.append(value)
        result = await self.adapter.run(self.workspace, 'Tool worked; files are verified.', on_event=event)
        evidence = result['execution_evidence']
        self.assertEqual(evidence['native_command_execution'], 'observed')
        self.assertEqual((evidence['thread_id'], evidence['turn_id']), (result['thread_id'], result['turn_id']))
        self.assertEqual(evidence['command_items_total'], 1)
        self.assertEqual(evidence['execution_observed_count'], 1)
        self.assertEqual(evidence['successful_exit_count'], 1)
        self.assertEqual(evidence['items'], [{'item_id': 'exec-proof', 'status': 'completed',
                                             'exit_code': 0, 'duration_ms': 284}])
        self.assertNotIn('PRIVATE-', json.dumps({'evidence': evidence, 'events': events}))
        self.adapter._rpc = original
        dialogue = await self.adapter.run(self.workspace, 'I executed PowerShell successfully; exitCode 0, durationMs 284.')
        self.assertEqual(dialogue['status'], 'completed')
        self.assertEqual(dialogue['execution_evidence']['native_command_execution'], 'not_observed')
        self.assertEqual(dialogue['execution_evidence']['command_items_total'], 0)

    async def test_evidence_scopes_early_commands_to_confirmed_turn_and_deduplicates(self):
        original = self.adapter._rpc
        async def rpc(method, params):
            if method != 'turn/start':
                return await original(method, params)
            thread, turn = params['threadId'], f'turn-{self.adapter.serial}'
            self.adapter._notification('item/completed', {'threadId': thread, 'turnId': 'stale-turn',
                'item': self.command_item('exec-stale')})
            self.adapter._notification('item/completed', {'threadId': 'other-thread', 'turnId': turn,
                'item': self.command_item('exec-other-project')})
            self.adapter._notification('item/completed', {'threadId': thread,
                'item': self.command_item('exec-missing-turn')})
            event = {'threadId': thread, 'turnId': turn, 'item': self.command_item('exec-early')}
            self.adapter._notification('item/completed', event)
            self.adapter._notification('item/completed', event)
            self.assertEqual(self.adapter._active[thread].execution_evidence()['native_command_execution'], 'not_observed')
            result = await original(method, params)
            self.adapter._notification('item/completed', event)
            return result
        self.adapter._rpc = rpc
        result = await self.adapter.run(self.workspace, 'hello')
        evidence = result['execution_evidence']
        self.assertEqual(evidence['command_items_total'], 1)
        self.assertEqual(evidence['items'][0]['item_id'], 'exec-early')
        self.assertEqual(evidence['native_command_execution'], 'observed')

    async def test_native_failure_and_missing_exit_are_not_success_claims(self):
        original = self.adapter._rpc
        async def rpc(method, params):
            result = await original(method, params)
            if method == 'turn/start':
                for item in (self.command_item('exec-error', status='failed', exitCode=5),
                             self.command_item('exec-launch-error', status='failed', exitCode=None, durationMs=None),
                             self.command_item('exec-declined', status='declined', exitCode=None, durationMs=None)):
                    self.adapter._notification('item/completed', {'threadId': params['threadId'],
                        'turnId': result['turn']['id'], 'item': item})
            return result
        self.adapter._rpc = rpc
        result = await self.adapter.run(self.workspace, 'done')
        evidence = result['execution_evidence']
        self.assertEqual(evidence['command_items_total'], 3)
        self.assertEqual(evidence['native_command_execution'], 'observed')
        self.assertEqual(evidence['execution_observed_count'], 1)
        self.assertEqual(evidence['successful_exit_count'], 0)
        self.assertEqual(evidence['unsuccessful_exit_count'], 1)
        state = _Run('thread', turn_id='turn', turn_confirmed=True)
        state.record_command('turn', self.command_item(status='failed', exitCode=None))
        self.assertEqual(state.execution_evidence()['native_command_execution'], 'not_observed')

    async def test_malformed_command_metadata_and_nonterminal_events_do_not_count(self):
        state = _Run('thread', turn_id='turn', turn_confirmed=True)
        self.adapter._active['thread'] = state
        invalid = [{'exitCode': True}, {'exitCode': False}, {'exitCode': '0'}, {'exitCode': 0.0},
                   {'exitCode': 2 ** 32}, {'exitCode': -(2 ** 31) - 1}, {'durationMs': True},
                   {'durationMs': -1}, {'durationMs': 2 ** 53}, {'durationMs': '284'},
                   {'durationMs': .5}, {'status': 'inProgress'}, {'status': 'unknown'},
                   {'status': 'declined', 'exitCode': 0}, {'id': 'C:/private/path'}, {'id': 'x' * 129}]
        for change in invalid:
            self.adapter._notification('item/completed', {'threadId': 'thread', 'turnId': 'turn',
                'item': self.command_item(**change)})
        self.adapter._notification('item/started', {'threadId': 'thread', 'turnId': 'turn',
            'item': self.command_item('exec-started', status='inProgress')})
        self.adapter._notification('item/completed', {'threadId': 'thread', 'turnId': 'turn',
            'item': {'id': 'agent', 'type': 'agentMessage', 'text': json.dumps(self.command_item())}})
        self.assertEqual(state.execution_evidence()['command_items_total'], 0)
        self.assertEqual(state.execution_evidence()['native_command_execution'], 'not_observed')

    async def test_evidence_is_bounded_independently_of_dropped_progress(self):
        state = _Run('thread', turn_id='turn', turn_confirmed=True)
        self.adapter._active['thread'] = state
        for index in range(MAX_EXECUTION_TRACKED + 9):
            item = self.command_item('exec-' + str(index))
            self.adapter._notification('item/completed', {'threadId': 'thread', 'turnId': 'turn', 'item': item})
            self.adapter._notification('item/completed', {'threadId': 'thread', 'turnId': 'turn', 'item': item})
        evidence = state.execution_evidence()
        self.assertEqual(evidence['command_items_total'], MAX_EXECUTION_TRACKED)
        self.assertEqual(len(evidence['items']), MAX_EXECUTION_ITEMS)
        self.assertEqual(evidence['omitted_items'], MAX_EXECUTION_TRACKED - MAX_EXECUTION_ITEMS)
        self.assertTrue(evidence['tracking_limit_reached'])
        self.assertGreater(state.dropped_events, 0)
        self.assertLessEqual(len(state.command_completions), MAX_EXECUTION_TRACKED)
        early = _Run('other-thread')
        for index in range(MAX_EXECUTION_TRACKED + 9):
            early.record_command('foreign-' + str(index), self.command_item('exec-' + str(index)))
        self.assertEqual(len(early.command_completions), MAX_EXECUTION_TRACKED)
        self.assertEqual(early.execution_evidence()['native_command_execution'], 'not_observed')

    async def test_timeout_retains_observed_native_execution_without_claiming_turn_success(self):
        original = self.adapter._rpc
        async def rpc(method, params):
            result = await original(method, params)
            if method == 'turn/start':
                self.adapter._notification('item/completed', {'threadId': params['threadId'],
                    'turnId': result['turn']['id'], 'item': self.command_item()})
            return result
        self.adapter._rpc = rpc
        self.adapter.run_timeout = .01
        result = await self.adapter.run(self.workspace, 'wait')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'run_timeout')
        self.assertEqual(result['execution_evidence']['native_command_execution'], 'observed')
        self.assertEqual([method for method, _ in self.adapter.calls].count('turn/start'), 1)

    async def test_inconsistent_turn_reply_does_not_promote_unconfirmed_command_evidence(self):
        original = self.adapter._rpc
        async def rpc(method, params):
            result = await original(method, params)
            if method == 'turn/start':
                self.adapter._notification('item/completed', {'threadId': params['threadId'],
                    'turnId': result['turn']['id'], 'item': self.command_item()})
                return {'turn': {'id': 'inconsistent-turn', 'status': 'inProgress'}}
            return result
        self.adapter._rpc = rpc
        result = await self.adapter.run(self.workspace, 'done')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'invalid_turn_response')
        self.assertEqual(result['execution_evidence']['native_command_execution'], 'not_observed')
        self.assertIsNone(result['execution_evidence']['turn_id'])
        self.assertEqual(result['execution_evidence']['command_items_total'], 0)
        self.assertEqual([method for method, _ in self.adapter.calls].count('turn/start'), 1)


if __name__ == "__main__":
    unittest.main()
