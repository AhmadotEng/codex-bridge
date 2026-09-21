"""Public MCP schemas. Trust and workspace configuration are deliberately local-only."""

from __future__ import annotations


def _string(description: str, maximum: int = 1000) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": maximum, "description": description}


SESSION = _string("Stable collaboration session ID returned by session_create.", 128)
REQUEST = _string(
    "Caller-chosen unique operation ID, preferably a UUID. Reuse exactly this ID and the same arguments after a timeout or disconnect; never generate a new ID merely to retry.",
    128,
)
CONTEXT = {"type": "string", "maxLength": 64000, "description": "Shared context retained for subsequent collaboration turns."}
RESPONSIBILITIES = {"type": "object", "description": "Agreed responsibilities for each person or computer."}
RELATIVE_PATH = _string("Relative file path within this session's configured transfer root. Absolute paths and traversal are rejected.", 4096)


def _tool(name: str, description: str, properties: dict, required: list[str], *, readonly: bool = False, idempotent: bool = True, destructive: bool = False) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        "annotations": {
            "title": name.replace("_", " ").title(),
            "readOnlyHint": readonly,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": True,
        },
    }


TOOLS = [
    _tool("peer_status", "Discover paired computers, current availability, and bridge capabilities. Pairing is configured locally outside these tools.", {"peer_id": _string("Optional configured peer identifier.", 128)}, [], readonly=True),
    _tool("session_create", "Create a collaboration conversation for an already configured project on a paired computer. Workspaces and scope come from each computer's local project configuration. Supply a stable session_id to retry safely.", {
        "peer_id": _string("Configured paired computer identifier.", 128),
        "project_id": _string("Project identifier configured on both computers.", 128),
        "name": _string("Human-readable collaboration name.", 200),
        "goal": _string("Shared objective.", 64000),
        "context": CONTEXT,
        "responsibilities": RESPONSIBILITIES,
        "session_id": _string("Optional caller-chosen UUID; preserve it for retries.", 128),
    }, ["peer_id", "project_id", "name", "goal"], idempotent=False),
    _tool("session_list", "List collaboration sessions known to this computer, including their project and peer.", {}, [], readonly=True),
    _tool("session_get", "Read a session's retained context, conversation IDs, messages, tasks, results, and artifact references. Use this to receive new messages and inspect collaboration history.", {"session_id": SESSION}, ["session_id"], readonly=True),
    _tool("session_context_update", "Update shared context with optimistic revision checking. Read the session's current revision first; a conflict requires reviewing the new context before another update.", {
        "session_id": SESSION, "context": CONTEXT,
        "goal": _string("Optional replacement shared goal.", 64000),
        "responsibilities": RESPONSIBILITIES,
        "expected_revision": {"type": "integer", "minimum": 0, "description": "Current revision from session_get; protects against overwriting a concurrent update."},
        "request_id": REQUEST,
    }, ["session_id", "context", "expected_revision", "request_id"]),
    _tool("task_send", "Send work to the peer's locally authenticated Codex conversation for this session. Returns a request ID for status, waiting, and cancellation. Execution is limited by the peer's project configuration.", {
        "session_id": SESSION, "prompt": _string("Task or follow-up to execute in the designated session conversation.", 64000), "request_id": REQUEST,
    }, ["session_id", "prompt", "request_id"]),
    _tool("task_status", "Inspect a bridge-managed task's state, progress, result, errors, and timestamps. Completed means the Codex turn finished; inspect its result and evidence to determine whether the requested work succeeded.", {"session_id": SESSION, "request_id": REQUEST}, ["session_id", "request_id"], readonly=True),
    _tool("task_wait", "Wait briefly for a bridge-managed task's progress or completion. A timeout is not a failure and must not cause resubmission with a different ID.", {
        "session_id": SESSION, "request_id": REQUEST,
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30, "default": 30},
    }, ["session_id", "request_id"], readonly=True),
    _tool("task_cancel", "Request cancellation of a bridge-managed task. Cancellation may be cooperative and cannot undo actions already completed; inspect the returned status.", {"session_id": SESSION, "request_id": REQUEST}, ["session_id", "request_id"], destructive=True),
    _tool("message_send", "Send a question, clarification, note, or result into the shared session history. Set continue_conversation only when this message should also run a new turn in the peer's retained Codex conversation; otherwise it is a message only.", {
        "session_id": SESSION, "text": _string("Message text.", 64000),
        "kind": {"type": "string", "enum": ["question", "clarification", "result", "note"]},
        "request_id": REQUEST,
        "continue_conversation": {"type": "boolean", "default": False},
    }, ["session_id", "text", "kind", "request_id"]),
    _tool("artifact_send", "Transfer one explicitly selected file from this session's local export root to its peer import root. Returns the artifact ID and integrity hash. No arbitrary filesystem access is provided.", {
        "session_id": SESSION, "path": RELATIVE_PATH, "destination": RELATIVE_PATH, "request_id": REQUEST,
    }, ["session_id", "path", "destination", "request_id"]),
    _tool("artifact_fetch", "Fetch a selected, previously registered peer artifact into this session's local import root. Verify the returned integrity hash and artifact reference.", {
        "session_id": SESSION, "artifact_id": _string("Artifact ID from the session history or artifact_send result.", 128),
        "destination": RELATIVE_PATH, "request_id": REQUEST,
    }, ["session_id", "artifact_id", "destination", "request_id"]),
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def validate_arguments(name: str, arguments: object) -> None:
    """Validate the deliberately small JSON Schema subset used above, without dependencies."""
    if name not in TOOLS_BY_NAME:
        raise ValueError("Unknown tool")
    _validate(arguments, TOOLS_BY_NAME[name]["inputSchema"], "arguments")


def _validate(value: object, schema: dict, location: str) -> None:
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if kind in valid and not valid[kind]:
        raise ValueError(f"{location} must be {kind}")
    if kind == "object":
        for required in schema.get("required", []):
            if required not in value:
                raise ValueError(f"{location}.{required} is required")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key not in properties and schema.get("additionalProperties") is False:
                raise ValueError(f"Unknown argument {location}.{key}")
            if key in properties:
                _validate(child, properties[key], f"{location}.{key}")
    if kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ValueError(f"{location} has invalid length")
    if kind == "integer":
        if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
            raise ValueError(f"{location} is out of range")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} has an unsupported value")
