# Onboarding plan: implementation status

## Target experience

Two people install the same connector, sign into Codex separately, pair their computers, and choose project folders. They exchange tasks and results without manually relaying every prompt. Another project needs workspace selection and a new session, not another installation.

Account authentication, computer pairing, and project permissions are separate. Sharing a Codex account is unnecessary.

## Files on each system

| Item | Same or different? | Created by |
| --- | --- | --- |
| Bridge source / plugin bundle | Same version | This repository |
| Installed `.mcp.json` | Different executable/config paths | Local installer |
| `~/.codex-bridge/config.json` | Different identity, credentials, runtime, scopes | Setup assistant |
| `~/.codex-bridge/state/` | Different requests, progress, and local state | Bridge |
| Private invitations | One Bridge credential from each computer | `pair-setup` |
| SSH identity and known-host files | Existing local files; private key stays local | Each SSH owner |
| Project and transfer folders | Locally selected | `project-select` |
| Conversation IDs | One local conversation per participating computer/session | First received task |

## Phase 1 — published

The repository includes the account explanation, A/B examples, optional plugin packaging, a source-allowlist installer, and tested scoped collaboration primitives. Completed worker processes release their conversation writers.

## Phase 2 — implemented

| Planned feature | Implementation |
| --- | --- |
| Detect tools and check Codex compatibility | `setup.ps1` / `setup`; generated App Server schema validation |
| Guided private pairing | `pair-setup`; resumable invitation export/import; no credential contents printed |
| Identify connection failures | `preflight` separates runtime, sign-in, local daemon, SSH, forwarding, pairing, and project scope |
| Choose/update project folders | `project-select` preserves other projects, peers, and fixed local actions |
| Find a meaningful local chat | `show-chat` / `session_chat`; automatic project titles on new Bridge conversations |
| Show writer ownership | Working, released, desktop-owned conflict, failure, and unconfirmed states |
| Safe everyday status | `status` / `bridge_status` omit prompts, tokens, logs, and workspace paths |

## Phase 3 — implemented, with verification limits

| Planned feature | Implementation |
| --- | --- |
| Additional Codex versions | Structural contract checks against the installed binary, with negative tests for incompatible schemas |
| macOS/Linux launchers | Explicit-allowlist Python installer and POSIX wrappers; cross-platform CI |
| Files above 8 MiB | Asynchronous verified chunks, durable progress, bounded quotas, retry/resume, cancellation and expiry |
| Additional peers | Named SSH transports with separate lifecycle/state, pairing credentials, and project scopes |

Passing a generated-schema check does not prove every behavior of a new Codex version. Launcher/unit CI does not prove desktop integration or authenticated model execution on every operating system. See [verification evidence](TESTING.md).

## Acceptance for two new users

Use separate local accounts and empty workspaces. Verify both directions, retained-context follow-up, file integrity, and a second isolated project. Retry a disconnected request with the same ID and confirm it does not execute twice. Cancel active work, revoke the disposable pairing, and confirm later requests fail.

Open a finished worker chat without a lingering Bridge writer. While the desktop owns it, confirm Bridge reports `conversation_in_use` without taking it over. Treat completed results and file hashes as evidence; a queued request or connected tunnel alone is insufficient.

Automated fresh-directory tests cover the setup and protocol boundaries. Real local Codex tests check title persistence, retained context, and writer release. Final acceptance by two new human users with separate accounts remains a distinct deployment check.
