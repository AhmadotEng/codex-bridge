import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_bridge.compatibility import accepts, inspect_runtime, validate_schema_bundle


def write_bundle(directory):
    string = {"type": "string"}
    policy = {"oneOf": [
        {"type": "object", "properties": {"type": {"enum": ["readOnly"]}, "networkAccess": {"type": "boolean"}}, "required": ["type"]},
        {"type": "object", "properties": {"type": {"enum": ["workspaceWrite"]}, "networkAccess": {"type": "boolean"}, "writableRoots": {"type": "array", "items": string}, "excludeTmpdirEnvVar": {"type": "boolean"}, "excludeSlashTmp": {"type": "boolean"}}, "required": ["type"]}]}
    common = {"cwd": string, "approvalPolicy": {"enum": ["never"]}, "approvalsReviewer": {"enum": ["user"]},
              "sandbox": {"enum": ["read-only", "workspace-write"]}, "config": {"type": "object", "additionalProperties": True}, "developerInstructions": string}
    methods = {
        "initialize": {"clientInfo": True, "capabilities": True},
        "thread/start": {**common, "ephemeral": {"type": "boolean"}, "dynamicTools": True},
        "thread/resume": {**common, "threadId": string, "excludeTurns": {"type": "boolean"}},
        "thread/name/set": {"threadId": string, "name": string},
        "turn/start": {"threadId": string, "input": True, "cwd": string, "sandboxPolicy": policy,
                       "approvalPolicy": {"enum": ["never"]}, "approvalsReviewer": {"enum": ["user"]}},
        "turn/interrupt": {"threadId": string, "turnId": string},
        "mcpServerStatus/list": {"threadId": string, "limit": {"type": "integer"}, "cursor": string},
        "account/read": {"refreshToken": {"type": "boolean"}},
        "config/batchWrite": {"edits": True, "reloadUserConfig": {"type": "boolean"}},
    }
    turn = {"type": "object", "properties": {"id": string, "status": {"enum": ["completed", "failed", "interrupted"]}}}
    bundle = {"ClientRequest": {"oneOf": [{"properties": {"method": {"enum": [name]}, "params": {"type": "object", "properties": props}}} for name, props in methods.items()]},
        "ServerRequest": {"oneOf": [{"properties": {"method": {"enum": ["item/tool/call"]}, "params": {"properties": {}}}}]},
        "DynamicToolCallResponse": {"properties": {"success": {"type": "boolean"}, "contentItems": True}},
        "v2/TurnStartResponse": {"properties": {"turn": turn}},
        "v2/ListMcpServerStatusResponse": {"properties": {"data": {"type": "array"}}},
        "ServerNotification": {"oneOf": [{"properties": {"method": {"enum": [name]}, "params": {"properties": fields}}} for name, fields in {
            "turn/started": {"threadId": string, "turn": turn}, "turn/completed": {"threadId": string, "turn": turn},
            "item/completed": {"threadId": string, "turnId": string, "item": True}}.items()]}}
    for name in ("v2/ThreadStartResponse", "v2/ThreadResumeResponse"):
        bundle[name] = {"properties": {"thread": {"properties": {"id": string}}, "approvalPolicy": {"enum": ["never"]}, "sandbox": policy}}
    for name, value in bundle.items():
        target = Path(directory)/(name+".json"); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))
    return bundle


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bundle = write_bundle(self.directory)

    def replace(self, name, value):
        (self.directory/(name+".json")).write_text(json.dumps(value))

    def test_validates_core_and_optional_dynamic_tools(self):
        for enabled in (False, True):
            result = validate_schema_bundle(self.directory, enable_local_actions=enabled)
            self.assertTrue(result["ready"], result)
            self.assertEqual(len(result["schema_sha256"]), 64)
            self.assertIn("thread/name/set", result["checked_methods"])

    def test_missing_method_and_new_required_field_fail_closed(self):
        schema = copy.deepcopy(self.bundle["ClientRequest"])
        schema["oneOf"] = [item for item in schema["oneOf"] if item["properties"]["method"]["enum"] != ["turn/interrupt"]]
        self.replace("ClientRequest", schema)
        self.assertFalse(validate_schema_bundle(self.directory)["ready"])
        schema = copy.deepcopy(self.bundle["ClientRequest"])
        schema["oneOf"][0]["properties"]["params"]["required"] = ["newRequiredField"]
        self.replace("ClientRequest", schema)
        self.assertFalse(validate_schema_bundle(self.directory)["ready"])

    def test_ignored_security_fields_are_not_assumed_supported(self):
        schema = copy.deepcopy(self.bundle["ClientRequest"])
        for item in schema["oneOf"]:
            if item["properties"]["method"]["enum"] == ["turn/start"]:
                del item["properties"]["params"]["properties"]["sandboxPolicy"]["oneOf"][1]["properties"]["networkAccess"]
        self.replace("ClientRequest", schema)
        self.assertFalse(validate_schema_bundle(self.directory)["ready"])

    def test_missing_response_field_rejected(self):
        self.replace("v2/TurnStartResponse", {"properties": {"turn": {"properties": {}}}})
        self.assertFalse(validate_schema_bundle(self.directory)["ready"])

    def test_new_version_requires_a_valid_schema_not_an_allowlist(self):
        def run(args, **kwargs):
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, b"codex-cli 9.8.7\n", b"")
            write_bundle(args[args.index("--out")+1])
            return subprocess.CompletedProcess(args, 0, b"", b"")
        with patch("codex_bridge.compatibility.subprocess.run", side_effect=run):
            result = inspect_runtime("local-codex")
        self.assertTrue(result["ready"])
        self.assertFalse(result["version_previously_tested"])
        self.assertEqual(result["codex_version"], "9.8.7")

    def test_probe_errors_do_not_expose_private_stderr(self):
        with patch("codex_bridge.compatibility.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"PRIVATE-TOKEN-CONTENTS")):
            result = inspect_runtime("local-codex")
        self.assertFalse(result["ready"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        with patch("codex_bridge.compatibility.subprocess.run", side_effect=subprocess.TimeoutExpired("private-path", 30)):
            result = inspect_runtime("local-codex")
        self.assertEqual(result["errors"][0]["code"], "runtime_probe_timeout")
        self.assertNotIn("private-path", json.dumps(result))

    def test_references_and_unsupported_constraints_fail_closed(self):
        root = {"definitions": {"x": {"type": "string"}}}
        self.assertTrue(accepts({"$ref": "#/definitions/x"}, "value", root))
        self.assertFalse(accepts({"$ref": "#/definitions/x"}, 4, root))
        from codex_bridge.compatibility import SchemaMismatch
        with self.assertRaises(SchemaMismatch):
            accepts({"$ref": "https://example.invalid/schema"}, "value", root)
        with self.assertRaises(SchemaMismatch):
            accepts({"if": {}}, "value", root)
