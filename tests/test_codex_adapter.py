import asyncio
import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge.codex_adapter import AdapterError, CodexAdapter, _Run


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


if __name__ == "__main__":
    unittest.main()
