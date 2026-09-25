import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_bridge.compatibility import (WINDOWS_BUNDLE_PROFILES, accepts,
    inspect_runtime, inspect_runtime_bundle, validate_schema_bundle)


def write_bundle(directory, *, full_access=False):
    string = {"type": "string"}
    policy = {"oneOf": [
        {"type": "object", "properties": {"type": {"enum": ["readOnly"]}, "networkAccess": {"type": "boolean"}}, "required": ["type"]},
        {"type": "object", "properties": {"type": {"enum": ["workspaceWrite"]}, "networkAccess": {"type": "boolean"}, "writableRoots": {"type": "array", "items": string}, "excludeTmpdirEnvVar": {"type": "boolean"}, "excludeSlashTmp": {"type": "boolean"}}, "required": ["type"]}]}
    common = {"cwd": string, "approvalPolicy": {"enum": ["never"]}, "approvalsReviewer": {"enum": ["user"]},
              "sandbox": {"enum": ["read-only", "workspace-write"]}, "config": {"type": "object", "additionalProperties": True}, "developerInstructions": string}
    if full_access:
        policy["oneOf"].append({"type": "object", "properties": {"type": {"enum": ["dangerFullAccess"]}}, "required": ["type"]})
        common["sandbox"]["enum"].append("danger-full-access")
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

    def test_full_access_is_optional_and_independently_reported(self):
        restricted = validate_schema_bundle(self.directory)
        self.assertTrue(restricted["ready"])
        self.assertFalse(restricted["full_access_supported"])
        self.assertEqual(restricted["errors"], [])
        self.assertTrue(restricted["full_access_errors"])
        write_bundle(self.directory, full_access=True)
        supported = validate_schema_bundle(self.directory)
        self.assertTrue(supported["ready"])
        self.assertTrue(supported["full_access_supported"])
        self.assertEqual(supported["full_access_errors"], [])

    def test_full_access_checks_each_native_request_and_thread_response(self):
        for missing in ("thread/start", "thread/resume", "turn/start", "v2/ThreadStartResponse", "v2/ThreadResumeResponse"):
            with self.subTest(missing=missing):
                bundle = write_bundle(self.directory, full_access=True)
                if missing.startswith("v2/"):
                    shape = bundle[missing]
                    shape["properties"]["sandbox"]["oneOf"].pop()
                    self.replace(missing, shape)
                else:
                    shape = bundle["ClientRequest"]
                    for item in shape["oneOf"]:
                        if item["properties"]["method"]["enum"] == [missing]:
                            props = item["properties"]["params"]["properties"]
                            if missing == "turn/start":
                                props["sandboxPolicy"]["oneOf"].pop()
                            else:
                                props["sandbox"]["enum"].remove("danger-full-access")
                    self.replace("ClientRequest", shape)
                result = validate_schema_bundle(self.directory)
                self.assertTrue(result["ready"], result)
                self.assertFalse(result["full_access_supported"], result)
                self.assertEqual(result["errors"], [])

    def test_full_access_undeclared_discriminator_is_not_support(self):
        for target in ("request", "response"):
            bundle = write_bundle(self.directory, full_access=True)
            if target == "request":
                shape = bundle["ClientRequest"]
                for item in shape["oneOf"]:
                    if item["properties"]["method"]["enum"] == ["turn/start"]:
                        props = item["properties"]["params"]["properties"]
                        props["sandboxPolicy"]["oneOf"][-1] = {"type": "object", "properties": {"other": {"type": "string"}}}
                self.replace("ClientRequest", shape)
            else:
                shape = bundle["v2/ThreadResumeResponse"]
                shape["properties"]["sandbox"]["oneOf"][-1] = {"type": "object", "properties": {"other": {"type": "string"}}}
                self.replace("v2/ThreadResumeResponse", shape)
            result = validate_schema_bundle(self.directory)
            self.assertFalse(result["full_access_supported"])
            self.assertTrue(result["full_access_errors"])

    def test_full_access_native_allof_response_wrapper_and_constraints(self):
        bundle = write_bundle(self.directory, full_access=True)
        for name in ("v2/ThreadStartResponse", "v2/ThreadResumeResponse"):
            shape = bundle[name]
            shape["definitions"] = {"SandboxPolicy": shape["properties"]["sandbox"]}
            shape["properties"]["sandbox"] = {"allOf": [{"$ref": "#/definitions/SandboxPolicy"}], "description": "Native compatibility field"}
            self.replace(name, shape)
        supported = validate_schema_bundle(self.directory)
        self.assertTrue(supported["full_access_supported"], supported)
        shape["properties"]["sandbox"]["allOf"].append({"properties": {"type": {"enum": ["readOnly", "workspaceWrite"]}}})
        self.replace(name, shape)
        restricted = validate_schema_bundle(self.directory)
        self.assertTrue(restricted["ready"], restricted)
        self.assertFalse(restricted["full_access_supported"])

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
        self.assertTrue(result["schema_ready"])
        self.assertEqual(result["native_tool_execution"], "not_checked")

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


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve() / "PRIVATE-OWNER-RUNTIME"
        self.directory.mkdir()
        self.executable = self.directory / "codex.exe"
        self.executable.write_bytes(b"fake CLI; never execute")
        self.required = WINDOWS_BUNDLE_PROFILES["0.153.4"]

    def complete_bundle(self):
        for name in self.required:
            (self.directory / name).write_bytes(b"fixture companion; not executable")

    def schema_process(self, version="0.153.4", fail_schema=False):
        def run(args, **kwargs):
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, ("codex-cli " + version + "\n").encode(), b"")
            self.assertEqual(args[1:3], ["app-server", "generate-json-schema"])
            if fail_schema:
                return subprocess.CompletedProcess(args, 1, b"", b"PRIVATE-OWNER-RUNTIME")
            write_bundle(args[args.index("--out") + 1])
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return run

    def test_known_profiles_require_all_nonempty_regular_companions(self):
        for version in WINDOWS_BUNDLE_PROFILES:
            with self.subTest(version=version):
                result = inspect_runtime_bundle(self.executable, version, platform="win32")
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["profile"], version)
                self.assertEqual(result["missing_files"], list(self.required))
                self.assertEqual(result["checked_files"], [])
        (self.directory / self.required[0]).write_bytes(b"")
        (self.directory / self.required[1]).mkdir()
        (self.directory / self.required[2]).write_bytes(b"present")
        result = inspect_runtime_bundle(self.executable, "0.153.4", platform="win32")
        self.assertEqual(result["empty_files"], [self.required[0]])
        self.assertEqual(result["invalid_files"], [self.required[1]])
        self.assertEqual(result["checked_files"], [self.required[2]])
        self.assertEqual(result["status"], "incomplete")

    def test_complete_profile_reads_bounded_bytes_without_executing_helpers(self):
        self.complete_bundle()
        import os
        with patch("codex_bridge.compatibility.os.read", wraps=os.read) as read, \
                patch("codex_bridge.compatibility.subprocess.run") as execute:
            result = inspect_runtime_bundle(self.executable, "0.153.4", platform="win32")
        execute.assert_not_called()
        self.assertEqual(read.call_count, 3)
        self.assertTrue(all(call.args[1] == 1 for call in read.call_args_list))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["checked_files"], list(self.required))
        self.assertEqual(result["check"], "nonempty_regular_readable_siblings")

    def test_unreadable_companions_are_sanitized_and_incomplete(self):
        self.complete_bundle()
        with patch("codex_bridge.compatibility.os.open", side_effect=PermissionError("PRIVATE-TOKEN " + str(self.directory))):
            result = inspect_runtime_bundle(self.executable, "0.153.4", platform="win32")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["unreadable_files"], list(self.required))
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn(str(self.directory), json.dumps(result))

    def test_windows_reparse_companions_are_not_regular_bundle_evidence(self):
        self.complete_bundle()
        from types import SimpleNamespace
        original = Path.lstat
        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_file_attributes=1024)
        with patch.object(Path, "lstat", reparse), patch("codex_bridge.compatibility.os.open") as opened:
            result = inspect_runtime_bundle(self.executable, "0.153.4", platform="win32")
        opened.assert_not_called()
        self.assertEqual(result["invalid_files"], list(self.required))
        self.assertEqual(result["status"], "incomplete")

    def test_unobserved_windows_version_does_not_invent_requirements(self):
        with patch.object(Path, "lstat", side_effect=AssertionError("must not inspect unknown profile")):
            result = inspect_runtime_bundle(self.executable, "0.155.0-alpha.9.2", platform="win32")
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["profile"])
        self.assertEqual(result["check"], "not_checked")
        self.assertEqual(result["required_files"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_other_platforms_are_not_applicable_even_for_known_version(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), \
                    patch.object(Path, "lstat", side_effect=AssertionError("must not inspect Windows companions")):
                result = inspect_runtime_bundle(self.executable, "0.153.4", platform=platform)
                self.assertEqual(result["status"], "not_applicable")
                self.assertEqual(result["required_files"], [])
                self.assertEqual(result["checked_files"], [])

    def test_schema_success_with_incomplete_bundle_is_not_ready(self):
        with patch("codex_bridge.compatibility.sys.platform", "win32"), \
                patch("codex_bridge.compatibility.subprocess.run", side_effect=self.schema_process()) as run:
            result = inspect_runtime(self.executable)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(result["ready"])
        self.assertTrue(result["schema_ready"])
        self.assertEqual(result["compatibility"], "schema-validated")
        self.assertEqual(result["runtime_bundle"]["status"], "incomplete")
        self.assertEqual([error["code"] for error in result["errors"]], ["runtime_bundle_incomplete"])
        self.assertEqual(result["native_tool_execution"], "not_checked")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_complete_bundle_does_not_claim_native_tool_execution(self):
        self.complete_bundle()
        with patch("codex_bridge.compatibility.sys.platform", "win32"), \
                patch("codex_bridge.compatibility.subprocess.run", side_effect=self.schema_process()):
            result = inspect_runtime(self.executable)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["runtime_bundle"]["status"], "complete")
        self.assertEqual(result["native_tool_execution"], "not_checked")
        self.assertEqual(result["errors"], [])

    def test_unknown_and_non_windows_bundles_keep_schema_fallback(self):
        for platform, version, status in (("win32", "0.155.0-alpha.9.2", "unknown"),
                ("linux", "0.153.4", "not_applicable"), ("darwin", "0.153.4", "not_applicable")):
            with self.subTest(platform=platform, version=version), \
                    patch("codex_bridge.compatibility.sys.platform", platform), \
                    patch("codex_bridge.compatibility.subprocess.run", side_effect=self.schema_process(version)):
                result = inspect_runtime(self.executable)
                self.assertTrue(result["ready"], result)
                self.assertEqual(result["runtime_bundle"]["status"], status)
                self.assertEqual(result["native_tool_execution"], "not_checked")

    def test_schema_probe_failure_preserves_bundle_failure(self):
        with patch("codex_bridge.compatibility.sys.platform", "win32"), \
                patch("codex_bridge.compatibility.subprocess.run", side_effect=self.schema_process(fail_schema=True)):
            result = inspect_runtime(self.executable)
        self.assertFalse(result["ready"])
        self.assertFalse(result["schema_ready"])
        self.assertEqual(result["compatibility"], "unavailable")
        self.assertEqual([error["code"] for error in result["errors"]], ["runtime_bundle_incomplete", "runtime_probe_failed"])
        self.assertNotIn("PRIVATE", json.dumps(result))
