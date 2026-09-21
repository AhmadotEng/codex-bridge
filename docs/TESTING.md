# Verification

## Automated tests

The public release passed all **78 tests** on Windows with Python 3.11.

Run from the repository root:

```powershell
python -m unittest discover -s tests -v
```

The suite exercises authentication boundaries, workspace/transfer scope, path traversal and links, integrity hashes, version/configuration checks, MCP schemas, request deduplication, reconnect/recovery, cancellation, revocation, Windows launcher lifecycle, fixed actions, and conversation ownership.

Installer tests use temporary directories containing fake private-file sentinels. They verify that Git metadata, runtime state and untracked files do not enter the installed connector. Tests do not log into Codex or contact another computer. Windows-specific checks may be skipped on other platforms.

## Manual verification performed during development

Two Windows computers, each using its own local Codex installation and login, exchanged model-driven tasks, replies and selected files in both directions. Follow-ups retained context; independent projects stayed separate. Disconnect/retry, cancellation and revocation were tested. SHA-256 checks confirmed selected file transfers.

Installed App Server schemas were inspected for versions **0.153.4** and **0.155.0-alpha.2.6**. A real two-client App Server test confirmed that closing the Bridge-owned process releases its writer, preserves the conversation ID/context, and permits another local client to resume. While that client owns the conversation, Bridge returns `conversation_in_use` without starting a turn. Persistent fixed-action tools also survived release and resume.

That ownership fix is included in this source release. These tests do not claim every existing deployment was upgraded. Real-life diagnostic transcripts, conversation IDs, usernames, private paths and device logs are excluded from the public repository.

## Still to verify

- End-to-end onboarding by two new users following only the public setup guide.
- Other Codex versions and macOS/Linux deployment.
- Automatic large-file chunking and more than one supervisor-managed transport per configuration.
- Guaranteed desktop notifications or automatic sidebar refresh.

Bridge tests establish collaboration behavior. They do not establish the correctness of a particular project, game, device plugin or deployment.
