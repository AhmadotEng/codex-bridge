"""Read-only App Server contract probe. Never starts a conversation or reads login files.

A version string is informational. The executable's generated schema must accept
the requests Bridge sends and describe the response/notification fields it uses.
Schema compatibility does not claim that an untested platform has passed gameplay
or a real Codex turn. Runtime policy and ownership checks remain in the adapter.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

TESTED_VERSIONS = frozenset({"0.153.4", "0.155.0-alpha.2.6"})
MAX_SCHEMA_BYTES = 32 * 1024 * 1024


class SchemaMismatch(ValueError):
    pass


def _resolve(node: Any, root: dict) -> Any:
    seen = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            raise SchemaMismatch("Unsupported or recursive schema reference")
        seen.add(ref)
        node = root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def accepts(node: Any, value: Any, root: dict, depth: int = 0) -> bool:
    """Conservative validation for request shapes; deliberately no external refs."""
    if depth > 40:
        raise SchemaMismatch("Schema nesting exceeds compatibility limits")
    node = _resolve(node, root)
    if isinstance(node, bool):
        return node
    if not isinstance(node, dict):
        return False
    if "enum" in node and value not in node["enum"]:
        return False
    if "const" in node and value != node["const"]:
        return False
    if "oneOf" in node and sum(accepts(part, value, root, depth+1) for part in node["oneOf"]) != 1:
        return False
    if "anyOf" in node and not any(accepts(part, value, root, depth+1) for part in node["anyOf"]):
        return False
    if "allOf" in node and not all(accepts(part, value, root, depth+1) for part in node["allOf"]):
        return False
    # Do not silently accept newly introduced constraints we cannot evaluate.
    if set(node) & {"not", "if", "then", "else", "dependentRequired", "dependencies", "patternProperties", "unevaluatedProperties"}:
        raise SchemaMismatch("Request schema uses an unsupported validation constraint")
    kind = node.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    checks = {"null": value is None, "boolean": isinstance(value, bool),
              "integer": isinstance(value, int) and not isinstance(value, bool),
              "number": isinstance(value, (float, int)) and not isinstance(value, bool),
              "string": isinstance(value, str), "object": isinstance(value, dict),
              "array": isinstance(value, list)}
    if kind is not None and not any(checks.get(item, False) for item in kinds):
        return False
    if isinstance(value, dict):
        props = node.get("properties", {})
        if any(key not in value for key in node.get("required", [])):
            return False
        for key, item in value.items():
            if key not in props and node.get("additionalProperties") is False:
                return False
            schema = props.get(key, node.get("additionalProperties", True))
            if not accepts(schema, item, root, depth+1):
                return False
    elif isinstance(value, list):
        if len(value) < node.get("minItems", 0) or len(value) > node.get("maxItems", float("inf")):
            return False
        if node.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            return False
        items = node.get("items", True)
        if isinstance(items, list):
            raise SchemaMismatch("Tuple request schema is unsupported")
        if not all(accepts(items, item, root, depth+1) for item in value):
            return False
    elif isinstance(value, str):
        if len(value) < node.get("minLength", 0) or len(value) > node.get("maxLength", float("inf")):
            return False
        if "pattern" in node and not re.search(node["pattern"], value):
            return False
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < node.get("minimum", -float("inf")) or value > node.get("maximum", float("inf")):
            return False
    return True


def _method(schema: dict, method: str) -> dict:
    for item in schema.get("oneOf", schema.get("anyOf", [])):
        properties = _resolve(item, schema).get("properties", {})
        discriminator = _resolve(properties.get("method", {}), schema)
        if method in discriminator.get("enum", []) or discriminator.get("const") == method:
            return _resolve(properties.get("params", {}), schema)
    raise SchemaMismatch("Missing method: " + method)


def _declares(node: Any, value: Any, root: dict) -> bool:
    """Unknown JSON properties being accepted is not evidence of API support."""
    node = _resolve(node, root)
    if node is True:
        return True  # Deliberately arbitrary JSON, such as dynamic inputSchema.
    if not isinstance(node, dict):
        return False
    choices = node.get("oneOf", node.get("anyOf"))
    if choices is not None:
        return any(accepts(part, value, root) and _declares(part, value, root) for part in choices)
    if isinstance(value, dict):
        props = node.get("properties", {})
        if not props and isinstance(node.get("additionalProperties"), (bool, dict)):
            return accepts(node, value, root)  # Explicit typed/freeform map.
        return all(key in props and _declares(props[key], item, root) for key, item in value.items())
    if isinstance(value, list):
        return all(_declares(node.get("items", False), item, root) for item in value)
    return accepts(node, value, root)


def _field(schema: dict, fields: str, example: Any) -> None:
    node = schema
    for field in fields.split("."):
        node = _resolve(node, schema)
        if field not in node.get("properties", {}):
            raise SchemaMismatch("Missing response field: " + fields)
        node = node["properties"][field]
    if not accepts(node, example, schema):
        raise SchemaMismatch("Incompatible response field: " + fields)


def validate_schema_bundle(directory: str | Path, *, enable_local_actions: bool = False) -> dict:
    directory = Path(directory)
    digest = hashlib.sha256()
    loaded = {}
    def read(name):
        if name not in loaded:
            file = directory / (name + ".json")
            if not file.is_file() or file.stat().st_size > MAX_SCHEMA_BYTES:
                raise SchemaMismatch("Missing or oversized generated schema: " + name)
            data = file.read_bytes()
            loaded[name] = json.loads(data)
            digest.update(name.encode()); digest.update(data)
        return loaded[name]
    requests = read("ClientRequest")
    workspace = str(Path(tempfile.gettempdir()).resolve())
    common = {"cwd": workspace, "approvalPolicy": "never", "approvalsReviewer": "user",
              "sandbox": "read-only", "config": {}, "developerInstructions": "Scoped project task"}
    samples = {
        "initialize": {"clientInfo": {"name": "codex_bridge", "title": "Codex Bridge", "version": "0.2.0"}, "capabilities": {"experimentalApi": enable_local_actions}},
        "thread/start": {**common, "ephemeral": False},
        "thread/resume": {**common, "threadId": "bridge-thread", "excludeTurns": True},
        "thread/name/set": {"threadId": "bridge-thread", "name": "Bridge: Example"},
        "turn/start": {"threadId": "bridge-thread", "input": [{"type": "text", "text": "Hello"}], "cwd": workspace, "sandboxPolicy": {"type": "readOnly", "networkAccess": False}, "approvalPolicy": "never", "approvalsReviewer": "user"},
        "turn/interrupt": {"threadId": "bridge-thread", "turnId": "bridge-turn"},
        "mcpServerStatus/list": {"threadId": "bridge-thread", "limit": 100, "cursor": "cursor"},
        "account/read": {"refreshToken": False},
        "config/batchWrite": {"edits": [{"keyPath": "plugins.example.enabled", "value": False, "mergeStrategy": "replace"}], "reloadUserConfig": True},
    }
    errors = []
    for method, params in samples.items():
        try:
            if not accepts(_method(requests, method), params, requests) or not _declares(_method(requests, method), params, requests):
                raise SchemaMismatch("Request fields are incompatible: " + method)
        except (SchemaMismatch, KeyError, TypeError, ValueError) as exc:
            errors.append({"code": "schema_incompatible", "message": str(exc)[:240]})
    try:
        start = _method(requests, "thread/start")
        if not accepts(start, {**samples["thread/start"], "sandbox": "workspace-write"}, requests):
            raise SchemaMismatch("Workspace-write thread policy is incompatible")
        turn = _method(requests, "turn/start")
        write_sample = {**samples["turn/start"], "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [workspace], "networkAccess": False, "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}}
        if not accepts(turn, write_sample, requests) or not _declares(turn, write_sample, requests):
            raise SchemaMismatch("Workspace-write turn policy is incompatible")
        for name in ("v2/ThreadStartResponse", "v2/ThreadResumeResponse"):
            schema = read(name)
            _field(schema, "thread.id", "bridge-thread")
            _field(schema, "approvalPolicy", "never")
            _field(schema, "sandbox", {"type": "readOnly", "networkAccess": False})
            _field(schema, "sandbox", write_sample["sandboxPolicy"])
        _field(read("v2/TurnStartResponse"), "turn.id", "bridge-turn")
        _field(read("v2/ListMcpServerStatusResponse"), "data", [])
        notifications = read("ServerNotification")
        for method, fields in {"turn/started": ["threadId", "turn"], "turn/completed": ["threadId", "turn"], "item/completed": ["threadId", "turnId", "item"]}.items():
            shape = _method(notifications, method)
            if any(field not in shape.get("properties", {}) for field in fields):
                raise SchemaMismatch("Notification fields are incompatible: " + method)
        turn_schema = _resolve(_method(notifications, "turn/completed")["properties"]["turn"], notifications)
        for status in ("completed", "failed", "interrupted"):
            if not accepts(turn_schema.get("properties", {}).get("status", False), status, notifications):
                raise SchemaMismatch("Terminal turn status is incompatible")
        if enable_local_actions:
            dynamic = {"type": "function", "name": "bridge_local_action", "description": "Fixed local action", "inputSchema": {"type": "object"}}
            if not accepts(start, {**samples["thread/start"], "dynamicTools": [dynamic]}, requests) or not _declares(start, {**samples["thread/start"], "dynamicTools": [dynamic]}, requests):
                raise SchemaMismatch("Dynamic tool registration is incompatible")
            _method(read("ServerRequest"), "item/tool/call")
            _field(read("DynamicToolCallResponse"), "success", True)
            _field(read("DynamicToolCallResponse"), "contentItems", [{"type": "inputText", "text": "Done"}])
    except (SchemaMismatch, KeyError, TypeError, ValueError) as exc:
        errors.append({"code": "schema_incompatible", "message": str(exc)[:240]})
    return {"ready": not errors, "compatibility": "schema-validated" if not errors else "incompatible",
            "schema_sha256": digest.hexdigest(), "checked_methods": list(samples),
            "local_actions_checked": enable_local_actions, "errors": errors}


def inspect_runtime(codex_path: str | Path, *, timeout: float = 30, enable_local_actions: bool = False) -> dict:
    """Return sanitized local compatibility status. Executable/config paths stay local."""
    result = {"ready": False, "codex_version": None, "compatibility": "unavailable", "errors": []}
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        version = subprocess.run([str(codex_path), "--version"], capture_output=True, timeout=timeout, **kwargs)
        match = re.fullmatch(rb"codex-cli (\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\s*", version.stdout)
        if version.returncode or not match:
            raise SchemaMismatch("Configured executable did not report a Codex CLI version")
        result["codex_version"] = match.group(1).decode("ascii")
        result["version_previously_tested"] = result["codex_version"] in TESTED_VERSIONS
        with tempfile.TemporaryDirectory(prefix="codex-bridge-schema-") as directory:
            args = [str(codex_path), "app-server", "generate-json-schema", "--out", directory]
            if enable_local_actions:
                args.append("--experimental")
            generated = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
            if generated.returncode:
                raise SchemaMismatch("Codex could not generate its App Server schema; update Codex or select another local executable")
            result.update(validate_schema_bundle(directory, enable_local_actions=enable_local_actions))
    except subprocess.TimeoutExpired:
        result["errors"].append({"code": "runtime_probe_timeout", "message": "Codex compatibility probe timed out"})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never surface executable stderr, config values, or full local paths.
        message = str(exc) if isinstance(exc, SchemaMismatch) else "Codex executable or generated App Server schema could not be read"
        result["errors"].append({"code": "runtime_probe_failed", "message": message[:240]})
    return result
