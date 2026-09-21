# Plan: make Bridge usable by two new people

## Target experience

Two people install the same connector, sign into Codex separately, pair their computers, and choose project folders. They can exchange tasks and results without copying prompts between chats. A new project requires workspace selection and one new session, not reinstallation.

Account authentication, computer pairing, and project permissions are three separate things. Sharing a Codex account is unnecessary and does not create a shared project conversation.

## Files on each system

| Item | Same or different? | Who creates it? |
| --- | --- | --- |
| Bridge source / plugin bundle | Same version on both PCs | Download from this repository |
| Installed `.mcp.json` | Different executable/config paths | `install.ps1` |
| `~/.codex-bridge/config.json` | Different ID, runtime path, credentials, and scopes | `init`, pairing, local project setup |
| `~/.codex-bridge/state/` | Different request history and local state | Bridge |
| Pairing invitations | One temporary secret from each PC | `pair-export` |
| SSH identity and known-host files | Existing local files; private key stays with its owner | Each computer's SSH owner |
| Project folder and transfer folders | Each person selects their own paths | `project-add` |
| Project conversation IDs | One local ID per participating computer | First received task on that computer |

The example files for A and B are explanatory templates, not live configurations. Real credentials are generated locally and never committed.

## Phase 1: publish an understandable preview

Included in this repository:

1. A short README with the account answer and a six-step setup overview.
2. Exact, separately labeled A/B commands in a setup guide.
3. MCP registration that does not depend on internal scaffolding tools; optional plugin packaging remains available.
4. Secret-free examples for the SSH transport owner and the receiving computer.
5. An installer that copies an explicit source allowlist, excluding Git metadata and private runtime files.
6. A worker lifecycle that releases a finished conversation and refuses to take over a desktop-owned conversation.
7. Tests and clear limits for supported versions, selected file transfer, cancellation, and revocation.

This remains a compatibility-limited Windows preview with manual pairing and transport configuration.

## Phase 2: simplify the remaining manual setup

Planned; not implemented:

- A local setup assistant that detects Python/Codex, checks the installed App Server schema, and asks only for missing values.
- Guided invitation export/import with clear computer names and private exchange instructions.
- A connection test that identifies whether the failure is SSH, forwarding, pairing, runtime authentication, or project scope.
- A project picker that writes each PC's paths without overwriting unrelated configuration.
- A “show local project chat” command and meaningful automatic chat titles.
- Visible ownership states: working, released to desktop, waiting for desktop release, failed.
- A safe status view that does not expose tokens, personal file paths, or logs by default.

Each feature must be tested using fresh configuration directories. It must not require the original developers' accounts, machine names, game files, or credentials.

## Phase 3: broaden compatibility

Planned; not implemented:

- Schema checks and test coverage for additional Codex versions.
- Tested macOS/Linux launchers.
- Optional verified chunking for files above 8 MiB.
- Easier additional peers while keeping separate trust and project scopes.

## Acceptance test for two new users

Use separate Codex accounts and an empty sample workspace on each PC. Verify both directions, a follow-up with retained context, an intact small file, and a second isolated project. Disconnect/reconnect using the same request ID and confirm no duplicate execution. Cancel an active request, revoke pairing, and verify later requests are rejected.

Open a finished worker conversation in the desktop without a lingering Bridge lock. While the desktop owns it, verify Bridge reports `conversation_in_use` without dispatching another turn. Do not treat a queued request, connected tunnel, or completed assistant response alone as proof of the requested result.
