from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from codex_bridge.mcp import LocalBridgeClient, MAX_LINE_BYTES, MCPServer, serve
from codex_bridge.tools import TOOLS, validate_arguments


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        return {"ok": True, "result": {"name": name}, "error": None, "request_id": args.get("request_id", "response-id"), "timestamp": "2026-01-01T00:00:00Z"}


def request(method, params=None, message_id=1):
    return {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.server = MCPServer(self.client)

    def test_initialize_and_tools_are_available_without_daemon(self):
        result = self.server.handle(request("initialize", {"protocolVersion": "2025-03-26"}))["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        listed = self.server.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual(len(listed), 21)
        self.assertEqual(len({tool["name"] for tool in listed}), 21)
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in listed))
        self.assertEqual(self.client.calls, [])

    def test_task_payload_is_forwarded_without_rewriting_retry_id(self):
        args = {"session_id": "project-session", "prompt": "Check the selected file: مرحباً", "request_id": "retry-this-exact-id"}
        for _ in range(2):
            response = self.server.handle(request("tools/call", {"name": "task_send", "arguments": args}))
            result = response["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(self.client.calls, [("task_send", args), ("task_send", args)])

    def test_notifications_cannot_start_tasks(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "task_send", "arguments": {}}}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.client.calls, [])

    def test_protocol_errors(self):
        for value in ([], 3, None, {"method": "ping"}):
            self.assertEqual(self.server.handle(value)["error"]["code"], -32600)
        self.assertEqual(self.server.handle(request("does/not/exist"))["error"]["code"], -32601)
        bad = request("ping")
        bad["params"] = []
        self.assertEqual(self.server.handle(bad)["error"]["code"], -32602)
        self.assertEqual(self.server.handle(request("ping", message_id=True))["error"]["code"], -32600)

    def test_tool_schema_rejects_invalid_arguments(self):
        invalid = [
            ("task_send", {"session_id": "s", "prompt": "p"}),
            ("peer_status", {"shell": "whoami"}),
            ("task_wait", {"session_id": "s", "request_id": "r", "timeout_seconds": 31}),
            ("task_wait", {"session_id": "s", "request_id": "r", "timeout_seconds": True}),
            ("message_send", {"session_id": "s", "request_id": "r", "text": "hello", "kind": "execute"}),
            ("session_context_update", {"session_id": "s", "request_id": "r", "context": "new"}),
            ("task_status", []),
        ]
        for name, args in invalid:
            with self.subTest(tool=name, arguments=args):
                response = self.server.handle(request("tools/call", {"name": name, "arguments": args}))
                self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(self.client.calls, [])

    def test_readonly_annotations_do_not_mark_mutations_readonly(self):
        readonly = {"local_status", "connection_status", "bridge_status", "session_chat", "peer_status", "session_list", "session_get", "task_status", "task_wait", "artifact_transfer_status"}
        for tool in TOOLS:
            self.assertEqual(tool["annotations"]["readOnlyHint"], tool["name"] in readonly)

    def test_stdio_recovers_from_parse_and_oversized_lines(self):
        incoming = io.BytesIO(b"not json\n" + b" " * (MAX_LINE_BYTES + 50) + b"\n" + json.dumps(request("ping", message_id=9)).encode() + b"\n")
        outgoing = io.BytesIO()
        self.assertEqual(serve(self.server, incoming, outgoing), 0)
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([reply.get("error", {}).get("code") for reply in replies], [-32700, -32600, None])
        self.assertEqual(replies[2], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_stdio_rejects_nonfinite_json(self):
        outgoing = io.BytesIO()
        serve(self.server, io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}\n'), outgoing)
        self.assertEqual(json.loads(outgoing.getvalue())["error"]["code"], -32700)

    def test_actual_stdio_process_emits_only_jsonrpc(self):
        script = Path(__file__).resolve().parents[1] / "codex_bridge" / "mcp.py"
        stream = "\n".join(json.dumps(value) for value in [
            request("initialize", {"protocolVersion": "2025-06-18"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request("tools/list", message_id=2),
        ]) + "\n"
        process = subprocess.run([sys.executable, "-u", str(script), "--config", "a-nonexistent-config.json"], input=stream, capture_output=True, text=True, timeout=10, cwd=tempfile.gettempdir())
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[1]["id"], 2)


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "config.json"
        self.received = []
        self.http_status = 200
        self.payload = {"ok": True, "result": {"peer": "available"}, "error": None, "request_id": "abc", "timestamp": "2026-01-01T00:00:00Z"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
                self.send_response(outer.http_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(outer.payload).encode())

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.config.write_text(json.dumps({"listen_port": self.httpd.server_port, "local_token": "local-test-token"}), encoding="utf-8")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def test_local_proxy_uses_auth_and_envelope_and_ignores_env_proxy(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
            result = LocalBridgeClient(self.config).call("peer_status", {"peer_id": "p"})
        self.assertEqual(result, self.payload)
        self.assertEqual(self.received, [("/rpc", "Bearer local-test-token", {"method": "peer_status", "params": {"peer_id": "p"}})])

    def test_auth_denial_is_structured_and_never_contains_token(self):
        self.http_status = 401
        result = LocalBridgeClient(self.config).call("task_send", {"request_id": "preserve-me"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "bridge_access_denied")
        self.assertEqual(result["request_id"], "preserve-me")
        self.assertNotIn("local-test-token", json.dumps(result))

    def test_invalid_local_configuration_is_structured(self):
        for config in ({"listen_port": "123", "local_token": "x"}, {"listen_port": 123, "local_token": "a\nb"}, []):
            self.config.write_text(json.dumps(config), encoding="utf-8")
            result = LocalBridgeClient(self.config).call("peer_status", {})
            self.assertEqual(result["error"]["code"], "configuration_error")
        self.assertEqual(self.received, [])

    def test_invalid_daemon_envelope(self):
        self.payload = {"not": "an envelope"}
        result = LocalBridgeClient(self.config).call("peer_status", {})
        self.assertEqual(result["error"]["code"], "invalid_bridge_response")

    def test_missing_config_does_not_prevent_tool_discovery(self):
        self.config.unlink()
        server = MCPServer(LocalBridgeClient(self.config))
        self.assertEqual(len(server.handle(request("tools/list"))["result"]["tools"]), 21)
        result = server.handle(request("tools/call", {"name": "peer_status"}))["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "configuration_error")


if __name__ == "__main__":
    unittest.main()
