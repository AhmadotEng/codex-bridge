"""Local Codex App Server stdio adapter; no authentication material is handled.

The bridge owns authorization, persistent request IDs and per-thread locking.
Only designated thread IDs supplied by that bridge are ever resumed.  App Server
0.153.4's generated JSON schemas are the compatibility baseline.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .compatibility import TESTED_VERSIONS, inspect_runtime

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
StartedCallback = Callable[[str, str], Awaitable[None]]
ActionCallback = Callable[[str, str], Awaitable[dict]]
SUPPORTED_VERSIONS = TESTED_VERSIONS  # Kept for callers; runtime uses schema validation.
MAX_TEXT = 256_000
MAX_EVENT_TEXT = 8_000
MAX_WIRE_LINE = 4_000_000
MAX_EXECUTION_ITEMS = 32
MAX_EXECUTION_TRACKED = 4096


def _safe_item_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value))


def _command_completion(item: Any) -> dict | None:
    """Allowlist completed native-command metadata; never return command data."""
    if not isinstance(item, dict) or item.get("type") != "commandExecution":
        return None
    if not _safe_item_identifier(item.get("id")):
        return None
    status, code, duration = item.get("status"), item.get("exitCode"), item.get("durationMs")
    if status not in ("completed", "failed", "declined"):
        return None
    if code is not None and (type(code) is not int or not -(2 ** 31) <= code <= 2 ** 32 - 1):
        return None
    if duration is not None and (type(duration) is not int or not 0 <= duration <= 2 ** 53 - 1):
        return None
    if status == "declined" and code is not None:
        return None
    return {"item_id": item["id"], "status": status, "exit_code": code, "duration_ms": duration}


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _Run:
    thread_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=512))
    turn_id: str | None = None
    terminal: dict | None = None
    messages: dict[str, dict] = field(default_factory=dict)
    blocked: list[dict] = field(default_factory=list)
    dropped_events: int = 0
    action_handler: ActionCallback | None = None
    action_ids: frozenset[str] = frozenset()
    action_tasks: set = field(default_factory=set)
    cancelled: bool = False
    turn_confirmed: bool = False
    command_completions: dict[tuple[str, str], dict] = field(default_factory=dict)
    command_tracking_limited: bool = False

    def record_command(self, turn_id: Any, item: Any) -> None:
        if not _safe_item_identifier(turn_id):
            return
        completion = _command_completion(item)
        if completion is None:
            return
        key = (turn_id, completion["item_id"])
        if key in self.command_completions:
            return
        if len(self.command_completions) >= MAX_EXECUTION_TRACKED:
            self.command_tracking_limited = True
            return
        self.command_completions[key] = completion

    def execution_evidence(self) -> dict:
        # turn/started and item events may arrive before turn/start's RPC reply.
        # Only that reply confirms which turn belongs to this request. Retain
        # bounded early candidates, then count only its exact confirmed turn.
        items = [item for (turn, _), item in self.command_completions.items()
                 if self.turn_confirmed and turn == self.turn_id]
        executed = [item for item in items if item["status"] in ("completed", "failed")
                    and item["exit_code"] is not None]
        successful = sum(item["status"] == "completed" and item["exit_code"] == 0 for item in executed)
        return {"source": "app_server_item_completed", "native_command_execution": "observed" if executed else "not_observed",
                "thread_id": self.thread_id, "turn_id": self.turn_id if self.turn_confirmed else None,
                "command_items_total": len(items), "execution_observed_count": len(executed),
                "successful_exit_count": successful, "unsuccessful_exit_count": len(executed) - successful,
                "items": items[:MAX_EXECUTION_ITEMS], "omitted_items": max(0, len(items) - MAX_EXECUTION_ITEMS),
                "tracking_limit_reached": self.command_tracking_limited}

    def put(self, event: dict) -> None:
        # A slow consumer cannot block the App Server response reader. Terminal
        # state is held separately so a bounded progress queue cannot lose it.
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped_events += 1
        self.queue.put_nowait(event)


class CodexAdapter:
    """One local App Server, supporting concurrent independent conversations.

    Callbacks run outside the JSON-RPC reader. ``on_started(thread_id, turn_id)``
    should durably save both IDs. Failed/unknown starts are never retried here.
    The caller must only pass bridge-owned conversation IDs and approved roots.
    """

    def __init__(self, codex_path: str | os.PathLike, *, request_timeout: float = 45,
                 run_timeout: float = 1800, allowed_mcp_servers: list[str] | None = None,
                 allowed_plugins: list[str] | None = None, enable_local_actions: bool = False):
        self.codex_path = str(codex_path)
        self.request_timeout = request_timeout
        self.run_timeout = run_timeout
        # Empty defaults isolate worker turns from personal integrations. An
        # installation may explicitly allow a vetted, scoped bridge-only MCP.
        self.allowed_mcp_servers = frozenset(allowed_mcp_servers or [])
        self.allowed_plugins = frozenset(allowed_plugins or [])
        self.enable_local_actions = enable_local_actions
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._active: dict[str, _Run] = {}
        self._next_id = 0
        self._version: str | None = None
        self._compatibility: dict = {}
        self._initialized: dict = {}
        self._closing = False
        self._server_request_tasks: set[asyncio.Task] = set()

    async def _probe_version(self) -> str:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = await asyncio.create_subprocess_exec(
            self.codex_path, "--version", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, **kwargs)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.request_timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        match = re.fullmatch(r"codex-cli (\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\s*", out.decode("utf-8", "replace"))
        if proc.returncode or not match:
            raise AdapterError("version_probe_failed", "Configured executable did not report a Codex CLI version")
        return match.group(1)

    async def _spawn(self) -> asyncio.subprocess.Process:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        overrides = self._integration_overrides(None)
        arguments = []
        for key, value in overrides.items():
            # These values are locally computed booleans/strings only. Each
            # config override is a distinct argument; no shell is involved.
            arguments.extend(["-c", key + "=" + json.dumps(value)])
        # Local login and configured model stay with the local Codex process.
        # No CODEX_HOME, token environment, model, or browser state is replaced.
        return await asyncio.create_subprocess_exec(
            self.codex_path, *arguments, "app-server", "--listen", "stdio://",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=MAX_WIRE_LINE, **kwargs)

    async def start(self) -> None:
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None and self._initialized:
                return
            if self._proc is not None:
                await self.close()
            self._closing = False
            self._compatibility = await asyncio.to_thread(inspect_runtime, self.codex_path,
                timeout=self.request_timeout, enable_local_actions=self.enable_local_actions)
            self._version = self._compatibility.get("codex_version")
            if self._compatibility.get("runtime_bundle", {}).get("status") == "incomplete":
                raise AdapterError("runtime_bundle_incomplete", "The selected Codex runtime lacks usable companion files. Select a complete installed runtime folder and restart the idle Bridge daemon; do not mix individual executables.")
            if not self._compatibility.get("ready"):
                errors = self._compatibility.get("errors") or [{"message": "Codex App Server schema is incompatible"}]
                raise AdapterError("unsupported_codex_schema", errors[0]["message"])
            self._proc = await self._spawn()
            self._reader = asyncio.create_task(self._read_loop())
            self._stderr = asyncio.create_task(self._drain_stderr())
            try:
                self._initialized = await self._rpc("initialize", {
                    "clientInfo": {"name": "codex_bridge", "title": "Codex Bridge", "version": "0.3.2-rc.6"},
                    "capabilities": {"experimentalApi": self.enable_local_actions},
                })
                await self._send({"method": "initialized", "params": {}})
            except BaseException:
                await self.close()
                raise

    async def capabilities(self) -> dict:
        await self.start()
        full_access = self._compatibility.get("full_access_supported") is True
        policies = ["read-only", "workspace-write"] + (["full-access"] if full_access else [])
        policy_details = {
            "read-only": {"supported": True, "network_access": False, "filesystem": "read-only"},
            "workspace-write": {"supported": True, "network_access": False, "filesystem": "selected-workspace"},
            "full-access": {"supported": full_access, "network_access": True, "filesystem": "owner-account",
                            "os_permissions": "existing-owner-privileges", "owner_selection_required": True},
        }
        return {
            "adapter": "codex-app-server-stdio", "codex_version": self._version,
            "tested_versions": sorted(TESTED_VERSIONS), "ready": True,
            "compatibility": self._compatibility,
            "native_tool_execution": "not_checked",
            "context_retention": "thread/resume", "cancellation": "turn/interrupt",
            "policies": policies, "policy_details": policy_details, "network_access": False,
            "network_access_by_policy": {policy: policy_details[policy]["network_access"] for policy in policies},
            "approval_behavior": "deny-and-report", "model": "local-configured-default",
            "authentication": "local-codex-login", "unrelated_thread_discovery": False,
            "allowed_mcp_servers": sorted(self.allowed_mcp_servers),
            "allowed_plugins": sorted(self.allowed_plugins),
            "personal_apps_and_hooks": False,
            "owner_configured_local_actions": self.enable_local_actions,
            "platform": self._initialized.get("platformOs"),
        }

    def _integration_overrides(self, workspace: str | None) -> dict:
        """Read integration NAMES, then disable non-allowlisted local config.

        No auth.json/keychain/browser store is opened, and no configuration
        values or token-bearing command/environment fields are exported.
        CLI overrides apply before startup; per-thread overrides also account
        for project configuration discovered under the selected workspace.
        """
        codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        paths = [codex_dir / "config.toml"]
        if workspace:
            root = Path(workspace).resolve()
            paths.extend(path / ".codex" / "config.toml" for path in [root, *root.parents])
        mcp_names, plugin_names = set(), set()
        for path in dict.fromkeys(paths):
            if not path.exists():
                continue
            try:
                config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
                mcp_names.update(config.get("mcp_servers", {}))
                plugin_names.update(config.get("plugins", {}))
            except (OSError, ValueError, TypeError) as exc:
                raise AdapterError("integration_scope_unavailable", "Cannot safely load local integration names to scope the worker") from exc
        result = {"web_search": "disabled"}
        for feature in ("apps", "hooks", "remote_plugin", "computer_use", "browser_use", "browser_use_external", "in_app_browser", "in_app_local_automation", "image_generation", "multi_agent", "memories", "skill_mcp_dependency_install", "tool_suggest"):
            result["features." + feature] = False
        result["features.plugins"] = bool(self.allowed_plugins)
        for name in mcp_names:
            if not re.fullmatch(r"[A-Za-z0-9_@/-]+", name):
                raise AdapterError("integration_scope_unavailable", "An MCP name cannot be safely expressed in this Codex version's dotted config overrides")
            result["mcp_servers." + name + ".enabled"] = name in self.allowed_mcp_servers
        if self.allowed_plugins:
            for name in plugin_names:
                if not re.fullmatch(r"[A-Za-z0-9_@/-]+", name):
                    raise AdapterError("integration_scope_unavailable", "A plugin name cannot be safely expressed in this Codex version's dotted config overrides")
                result["plugins." + name + ".enabled"] = name in self.allowed_plugins
        return result

    async def _validate_integrations(self, thread_id: str) -> None:
        # Inventory is limited to this bridge-owned thread. Disabled configured
        # servers can still appear by name, but must expose no tools/resources.
        cursor = None
        for _ in range(10):
            params = {"threadId": thread_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            status = await self._rpc("mcpServerStatus/list", params)
            for item in status.get("data", []):
                if item.get("name") not in self.allowed_mcp_servers and any(item.get(key) for key in ("tools", "resources", "resourceTemplates")):
                    raise AdapterError("integration_scope_mismatch", "An unapproved integration remains active; worker turn was not started")
            cursor = status.get("nextCursor")
            if not cursor:
                return
        raise AdapterError("integration_scope_unavailable", "Integration inventory exceeded the scope-verification limit")

    async def _send(self, message: dict) -> None:
        if self._proc is None or self._proc.returncode is not None or self._proc.stdin is None:
            raise AdapterError("app_server_unavailable", "Local Codex App Server is not running")
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_WIRE_LINE:
            raise AdapterError("request_too_large", "Codex request exceeds the bridge message limit")
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        ident = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise AdapterError("app_server_timeout", f"{method} response timed out; execution is not automatically retried") from exc
        finally:
            self._pending.pop(ident, None)

    async def _drain_stderr(self) -> None:
        # Diagnostic stderr may include paths or provider details. It is drained
        # to avoid deadlock, never copied into peer-facing results.
        assert self._proc and self._proc.stderr
        while await self._proc.stderr.read(8192):
            pass

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        failure = AdapterError("app_server_disconnected", "Local Codex App Server disconnected; request outcome may require inspection")
        try:
            while raw := await self._proc.stdout.readline():
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("non-object JSON-RPC message")
                if "id" in message and "method" not in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            err = message["error"]
                            detail = str(err.get("message", "RPC failed"))[:MAX_EVENT_TEXT]
                            if "already has an active writer" in detail:
                                future.set_exception(AdapterError("conversation_in_use", "This collaboration conversation is open for writing in another Codex app or process. Release it there before sending a new bridge request; no turn was started. The bridge will not take over or retry automatically."))
                            else:
                                future.set_exception(AdapterError("app_server_rpc_error", detail))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message and "method" in message:
                    task = asyncio.create_task(self._deny_server_request(message))
                    self._server_request_tasks.add(task)
                    task.add_done_callback(self._server_request_tasks.discard)
                elif "method" in message:
                    self._notification(message["method"], message.get("params", {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = AdapterError("app_server_protocol_error", f"Local Codex stream failed ({type(exc).__name__})")
        finally:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)
            for state in list(self._active.values()):
                if state.terminal is None:
                    state.terminal = {"status": "unknown", "error": {"code": failure.code, "message": str(failure)}}
                    state.put({"type": "connection_lost", "error": state.terminal["error"]})

    @staticmethod
    def _event(method: str, params: dict) -> dict | None:
        # Never forward raw reasoning, account, authentication, or command output.
        base = {"type": method, "timestamp": time.time(), "thread_id": params.get("threadId"), "turn_id": params.get("turnId")}
        if method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if item.get("type") in {"reasoning", "userMessage"}:
                return None
            base.update(item_id=item.get("id"), item_type=item.get("type"), status=item.get("status"))
            if item.get("type") == "commandExecution":
                if method == "item/completed":
                    completion = _command_completion(item)
                    if completion is None:
                        return None
                    base.update(item_id=completion["item_id"], status=completion["status"],
                                exit_code=completion["exit_code"], duration_ms=completion["duration_ms"])
                elif not _safe_item_identifier(item.get("id")) or item.get("status") not in (None, "inProgress"):
                    return None
            if item.get("type") == "agentMessage":
                base.update(text=str(item.get("text", ""))[:MAX_EVENT_TEXT], phase=item.get("phase"))
            return base
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn", {})
            base.update(turn_id=turn.get("id"), status=turn.get("status"))
            return base
        if method == "error":
            base.update(error={"message": str(params.get("error", {}).get("message", "Codex error"))[:MAX_EVENT_TEXT]}, will_retry=params.get("willRetry", False))
            return base
        if method == "turn/plan/updated":
            base["plan"] = [{"step": str(p.get("step", ""))[:1000], "status": p.get("status")} for p in params.get("plan", [])[:30]]
            return base
        return None

    def _notification(self, method: str, params: dict) -> None:
        state = self._active.get(params.get("threadId"))
        if state is None:
            return
        incoming_turn = params.get("turnId") or params.get("turn", {}).get("id")
        if state.turn_id is not None and incoming_turn is not None and incoming_turn != state.turn_id:
            return
        if method == "item/completed" and isinstance(params.get("item"), dict):
            state.record_command(params.get("turnId"), params["item"])
        if method == "turn/started":
            state.turn_id = params.get("turn", {}).get("id") or state.turn_id
        if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            item = params["item"]
            if len(state.messages) < 200:
                state.messages[str(item.get("id", len(state.messages)))] = {"text": str(item.get("text", ""))[:MAX_TEXT], "phase": item.get("phase")}
        if method == "turn/completed":
            turn = params.get("turn", {})
            state.turn_id = turn.get("id") or state.turn_id
            state.terminal = {"status": turn.get("status", "unknown"), "error": turn.get("error")}
        event = self._event(method, params)
        if event is not None:
            state.put(event)

    async def _deny_server_request(self, message: dict) -> None:
        method = message["method"]
        params = message.get("params", {})
        state = self._active.get(params.get("threadId") or params.get("conversationId"))
        if method == "item/tool/call" and state and state.action_handler and params.get("tool") == "bridge_local_action":
            arguments = params.get("arguments")
            valid = (not state.cancelled and state.terminal is None and
                     params.get("turnId") == state.turn_id and
                     params.get("namespace") in (None, "", "functions") and
                     isinstance(arguments, dict) and set(arguments) == {"action_id", "request_id"} and
                     isinstance(arguments.get("action_id"), str) and
                     arguments.get("action_id") in state.action_ids and
                     isinstance(arguments.get("request_id"), str) and
                     re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", arguments["request_id"]))
            outcome = {"status": "failed", "error": {"code": "action_scope_denied", "message": "Action call is outside this active turn's fixed capability"}}
            current = asyncio.current_task()
            if valid:
                state.action_tasks.add(current)
                state.put({"type": "local_action_started", "action_id": arguments["action_id"], "request_id": arguments["request_id"], "timestamp": time.time()})
                try:
                    outcome = await state.action_handler(arguments["action_id"], arguments["request_id"])
                except asyncio.CancelledError:
                    outcome = {"status": "cancelled", "error": {"code": "action_cancelled", "message": "Fixed local action cancelled"}}
                except Exception as exc:
                    outcome = {"status": "failed", "error": {"code": getattr(exc, "code", "action_failed"), "message": "Fixed local action was denied or failed; inspect local diagnostics"}}
                finally:
                    state.action_tasks.discard(current)
                state.put({"type": "local_action_completed", "action_id": arguments["action_id"], "request_id": arguments["request_id"], "status": outcome.get("status"), "timestamp": time.time()})
            with contextlib.suppress(AdapterError, BrokenPipeError, ConnectionResetError):
                await self._send({"id": message["id"], "result": {"success": outcome.get("status") == "completed", "contentItems": [{"type": "inputText", "text": json.dumps(outcome, ensure_ascii=False)}]}})
            return
        blocked = {"type": "approval_denied", "timestamp": time.time(), "method": method, "reason": "Codex Bridge does not grant interactive approvals or credential requests"}
        if state:
            if len(state.blocked) < 100:
                state.blocked.append(blocked)
            state.put(blocked)
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            response = {"result": {"decision": "decline"}}
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            response = {"result": {"decision": "denied"}}
        elif method == "item/permissions/requestApproval":
            response = {"result": {"permissions": {}, "scope": "turn"}}
        elif method == "item/tool/requestUserInput":
            response = {"result": {"answers": {}}}
        elif method == "mcpServer/elicitation/request":
            response = {"result": {"action": "decline"}}
        else:
            response = {"error": {"code": -32601, "message": "Server-initiated operation is not allowed by Codex Bridge"}}
        with contextlib.suppress(AdapterError, BrokenPipeError, ConnectionResetError):
            await self._send({"id": message["id"], **response})

    @staticmethod
    def _sandbox(workspace: str, policy: str, writable_roots: list[str] | None) -> tuple[str, dict]:
        root = Path(workspace)
        if not root.is_absolute() or not root.is_dir():
            raise AdapterError("invalid_workspace", "Workspace must be an existing absolute directory")
        root = root.resolve()
        if policy == "read-only":
            if writable_roots:
                raise AdapterError("invalid_policy", "Read-only requests cannot include writable roots")
            return str(root), {"type": "readOnly", "networkAccess": False}
        if policy == "full-access":
            if writable_roots:
                raise AdapterError("invalid_policy", "Full-access execution does not use writable-root restrictions")
            return str(root), {"type": "dangerFullAccess"}
        if policy != "workspace-write":
            raise AdapterError("invalid_policy", "Execution policy must be read-only, workspace-write, or full-access")
        roots = []
        for value in writable_roots or [str(root)]:
            path = Path(value)
            if not path.is_absolute() or not path.is_dir():
                raise AdapterError("invalid_writable_root", "Writable roots must be existing absolute directories")
            path = path.resolve()
            # Additional write roots outside the selected project are not a
            # hidden escape hatch. Select another session for another project.
            if not path.is_relative_to(root):
                raise AdapterError("invalid_writable_root", "Writable roots must remain inside the selected workspace")
            roots.append(str(path))
        return str(root), {"type": "workspaceWrite", "writableRoots": roots, "networkAccess": False, "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}

    async def run(self, workspace: str, prompt: str, thread_id: str | None = None,
                  on_event: EventCallback | None = None, on_started: StartedCallback | None = None,
                  policy: str = "read-only", writable_roots: list[str] | None = None,
                  local_actions: list[dict] | None = None, action_handler: ActionCallback | None = None) -> dict:
        workspace, sandbox = self._sandbox(workspace, policy, writable_roots)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_TEXT:
            raise AdapterError("invalid_prompt", "Prompt must contain 1 to 256000 characters")
        if local_actions and (not self.enable_local_actions or action_handler is None):
            raise AdapterError("action_unavailable", "Restart the bridge with its owner-configured local actions enabled")
        await self.start()
        state: _Run | None = None
        resuming = bool(thread_id)
        requested_thread_id = thread_id
        try:
            if policy == "full-access" and self._compatibility.get("full_access_supported") is not True:
                raise AdapterError("unsupported_full_access", "The selected Codex runtime has not validated full-access thread, resume, turn, and response policies. Select a compatible local runtime. No turn was started.")
            if thread_id in self._active:
                raise AdapterError("thread_busy", "A bridge turn is already active on this conversation")
            config = self._integration_overrides(workspace)
            if policy != "full-access":
                config.update({"sandbox_workspace_write.writable_roots": sandbox.get("writableRoots", []), "sandbox_workspace_write.network_access": False, "sandbox_workspace_write.exclude_tmpdir_env_var": True, "sandbox_workspace_write.exclude_slash_tmp": True})
            instructions = "This is a Codex Bridge collaboration task. Work only on the selected project and explicit request. "
            if policy == "full-access":
                instructions += ("The local owner explicitly selected full-access execution for this project. You may execute the requested commands, use the network, and perform expressly requested software installation, configuration changes (including Codex configuration), and service maintenance using the owner's existing OS privileges. Use existing owner-local authentication through normal software and tool flows. Never extract, expose, copy, or transfer account tokens, credentials, private keys, or browser sessions. Do not access unrelated projects or conversations. Do not bypass OS or administrator authorization; report requirements that the current account cannot satisfy. Personal MCP servers, apps, and plugins are not automatically authorized by full access. ")
            else:
                instructions += "Do not read credentials, browser sessions, private keys, unrelated projects, or unrelated conversations. Do not change Codex configuration or install plugins. "
            instructions += "Keep returned results relevant to this project. Do not delegate or send another bridge task unless the task explicitly asks you to. If blocked by scope or permissions, report the limitation."
            params = {"cwd": workspace, "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "danger-full-access" if policy == "full-access" else policy, "config": config,
                      "developerInstructions": instructions}
            if thread_id:
                params.update(threadId=thread_id, excludeTurns=True)
                reply = await self._rpc("thread/resume", params)
            else:
                params["ephemeral"] = False
                if local_actions:
                    params["dynamicTools"] = [{"type": "function", "name": "bridge_local_action",
                        "description": "Run a fixed owner-authorized project action. Only these reviewed operations are available: " + json.dumps(local_actions) + ". Supply a stable request_id for deduplication; reuse it when checking the same invocation. This capability does not authorize arbitrary commands or changes to action configuration.",
                        "inputSchema": {"type": "object", "properties": {"action_id": {"type": "string", "enum": [a["action_id"] for a in local_actions]}, "request_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"}}, "required": ["action_id", "request_id"], "additionalProperties": False}}]
                reply = await self._rpc("thread/start", params)
            returned_thread_id = reply.get("thread", {}).get("id")
            if not returned_thread_id:
                raise AdapterError("invalid_thread_response", "Codex did not return a conversation ID")
            if resuming and returned_thread_id != requested_thread_id:
                raise AdapterError("invalid_thread_response", "Codex did not resume the designated conversation. No turn was started.")
            thread_id = returned_thread_id
            if resuming and reply.get("sandbox", {}).get("type") != sandbox["type"]:
                raise AdapterError("policy_change_requires_new_session", "Codex retained the loaded conversation's previous sandbox policy. Start a new collaboration session with the selected policy, or restart the local bridge before resuming. No turn was started.")
            if reply.get("sandbox", {}).get("type") != sandbox["type"] or (policy != "full-access" and reply.get("sandbox", {}).get("networkAccess", False)) or reply.get("approvalPolicy") != "never":
                raise AdapterError("policy_mismatch", "Codex did not accept the requested execution policy")
            await self._validate_integrations(thread_id)
            state = _Run(thread_id)
            if local_actions:
                state.action_handler = action_handler
                state.action_ids = frozenset(a["action_id"] for a in local_actions)
            self._active[thread_id] = state
            turn_reply = await self._rpc("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}], "cwd": workspace, "sandboxPolicy": sandbox, "approvalPolicy": "never", "approvalsReviewer": "user"})
            returned_turn_id = turn_reply.get("turn", {}).get("id")
            if not returned_turn_id or (state.turn_id is not None and state.turn_id != returned_turn_id):
                raise AdapterError("invalid_turn_response", "Codex returned an inconsistent turn ID")
            state.turn_id = returned_turn_id
            state.turn_confirmed = True
            state.command_completions = {key: item for key, item in state.command_completions.items()
                                         if key[0] == returned_turn_id}
            if on_started:
                await on_started(thread_id, returned_turn_id)
            async with asyncio.timeout(self.run_timeout):
                while state.terminal is None or not state.queue.empty():
                    event = await state.queue.get()
                    if on_event:
                        await on_event(event)
            terminal = state.terminal or {"status": "unknown", "error": None}
            final = [item["text"] for item in state.messages.values() if item.get("phase") == "final_answer"]
            if not final:
                final = [item["text"] for item in state.messages.values()]
            return {"thread_id": thread_id, "turn_id": state.turn_id, "status": terminal["status"], "text": "\n\n".join(final)[-MAX_TEXT:], "error": terminal.get("error"), "blocked_requests": state.blocked, "dropped_progress_events": state.dropped_events,
                    "execution_evidence": state.execution_evidence()}
        except asyncio.CancelledError:
            if state and state.turn_id:
                with contextlib.suppress(Exception):
                    await self.cancel(state.thread_id, state.turn_id)
            raise
        except Exception as exc:
            if state and state.turn_id:
                with contextlib.suppress(Exception):
                    await self.cancel(state.thread_id, state.turn_id)
            code = exc.code if isinstance(exc, AdapterError) else ("run_timeout" if isinstance(exc, TimeoutError) else "adapter_error")
            return {"thread_id": thread_id, "turn_id": state.turn_id if state else None, "status": "unknown" if code in {"app_server_timeout", "app_server_disconnected"} else "failed", "text": "", "error": {"code": code, "message": str(exc)[:MAX_EVENT_TEXT] or code}, "blocked_requests": state.blocked if state else [],
                    "execution_evidence": (state or _Run(thread_id)).execution_evidence()}
        finally:
            if state and self._active.get(state.thread_id) is state:
                state.cancelled = True
                for action in list(state.action_tasks): action.cancel()
                if state.action_tasks:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.gather(*list(state.action_tasks), return_exceptions=True), 5)
                self._active.pop(state.thread_id, None)

    async def set_thread_name(self, thread_id: str, name: str) -> dict:
        """Name an already owned conversation; never load a desktop-owned one."""
        if thread_id not in self._active:
            raise AdapterError("thread_not_owned", "Only an active bridge conversation can be named")
        return await self._rpc("thread/name/set", {"threadId": thread_id, "name": name})

    async def cancel(self, thread_id: str, turn_id: str) -> dict:
        state = self._active.get(thread_id)
        if state is None or state.turn_id != turn_id:
            return {"status": "not_running", "thread_id": thread_id, "turn_id": turn_id}
        state.cancelled = True
        for action in list(state.action_tasks): action.cancel()
        await self._rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return {"status": "cancellation_requested", "thread_id": thread_id, "turn_id": turn_id}

    async def close(self) -> None:
        self._closing = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            cancellations = [self.cancel(state.thread_id, state.turn_id)
                             for state in list(self._active.values()) if state.turn_id]
            if cancellations:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.gather(*cancellations, return_exceptions=True), 3)
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    # On Windows a child can retain an inherited pipe after its
                    # parent exits. Bound shutdown rather than wait forever.
        for task in [self._reader, self._stderr, *self._server_request_tasks]:
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if proc is not None:
            # asyncio.Process has no public close() API. Closing its owned
            # transport releases Windows pipe handles (including Python 3.14
            # Proactor handles) after the process has exited or been killed.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
        self._proc = None
        self._initialized = {}
        self._reader = self._stderr = None


class TurnScopedCodexAdapter:
    """Release every completed conversation without disturbing another turn.

    Installed App Server versions keep unsubscribed threads loaded for thirty
    minutes. A dedicated process per active turn lets its writer lease end at
    turn completion while persisted conversation IDs/context remain resumable.
    The metadata process never loads or resumes a conversation.
    """

    def __init__(self, codex_path, **options):
        self.codex_path = codex_path
        self.options = options
        self.metadata = CodexAdapter(codex_path, **options)
        self.workers: set[CodexAdapter] = set()
        self.by_thread: dict[str, CodexAdapter] = {}
        self.release_tasks: dict[CodexAdapter, asyncio.Task] = {}
        self.closing = False

    def _new_worker(self):
        return CodexAdapter(self.codex_path, **self.options)

    async def start(self):
        self.closing = False
        await self.metadata.start()

    async def capabilities(self):
        result = await self.metadata.capabilities()
        return {**result, "conversation_ownership": "dedicated-app-server-per-turn; released-after-completion",
                "conversation_conflict": "fail-without-dispatch; owner-releases-other-app-before-new-request"}

    async def _release(self, worker):
        # Shutdown may race the normal turn-finally path. Both await the same
        # owned close operation instead of concurrently closing its pipes.
        task = self.release_tasks.get(worker)
        if task is None:
            task = asyncio.create_task(worker.close())
            self.release_tasks[worker] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self.release_tasks.get(worker) is task:
                self.release_tasks.pop(worker, None)

    async def run(self, workspace: str, prompt: str, thread_id: str | None = None,
                  on_event: EventCallback | None = None, on_started: StartedCallback | None = None,
                  policy: str = "read-only", writable_roots: list[str] | None = None,
                  local_actions: list[dict] | None = None, action_handler: ActionCallback | None = None):
        if self.closing:
            raise AdapterError("adapter_closing", "Local bridge is shutting down")
        if thread_id and thread_id in self.by_thread:
            return {"thread_id": thread_id, "turn_id": None, "status": "failed", "text": "",
                    "error": {"code": "thread_busy", "message": "A bridge turn already owns this conversation"}, "blocked_requests": [],
                    "execution_evidence": _Run(thread_id).execution_evidence()}
        worker = self._new_worker()
        self.workers.add(worker)
        if thread_id:
            self.by_thread[thread_id] = worker
        async def started(actual_thread, actual_turn):
            self.by_thread[actual_thread] = worker
            if on_started:
                await on_started(actual_thread, actual_turn)
        try:
            result = await worker.run(workspace=workspace, prompt=prompt, thread_id=thread_id,
                on_event=on_event, on_started=started, policy=policy, writable_roots=writable_roots,
                local_actions=local_actions, action_handler=action_handler)
            return {**result, "conversation_release": "owned-app-server-closed-after-turn"}
        finally:
            # Closing this worker never closes the metadata process, desktop
            # app, or a different project's active worker.
            await self._release(worker)
            self.workers.discard(worker)
            for owned_thread, owner in list(self.by_thread.items()):
                if owner is worker:
                    self.by_thread.pop(owned_thread, None)

    async def set_thread_name(self, thread_id, name):
        worker = self.by_thread.get(thread_id)
        if worker is None:
            raise AdapterError("thread_not_owned", "Bridge does not own this conversation")
        return await worker.set_thread_name(thread_id, name)

    async def cancel(self, thread_id, turn_id):
        worker = self.by_thread.get(thread_id)
        if worker is None:
            return {"status": "not_running", "thread_id": thread_id, "turn_id": turn_id}
        return await worker.cancel(thread_id, turn_id)

    async def close(self):
        self.closing = True
        await asyncio.gather(*(self._release(worker) for worker in [*self.workers, self.metadata]), return_exceptions=True)
        self.workers.clear()
        self.by_thread.clear()
