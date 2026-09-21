# Verification

## Isolated automated tests

Run from the repository root:

```powershell
python -m unittest discover -s tests -v
```

The suite runs without account credentials or a remote computer. It uses temporary configurations, fake private-file sentinels, real local HTTP endpoints, and protocol fixtures where model execution is unnecessary.

Coverage includes:

- Fresh setup, reruns, private invitations, independent computer credentials, and project preservation.
- Schema compatibility, rejection of missing security fields, and sanitized diagnostics.
- Both task directions, retained session IDs, two isolated projects, durable deduplication, cancellation, and revocation.
- Local chat links, meaningful titles, writer release/conflicts, and safe status views.
- Both file directions and fetch; large transfers, integrity failures, dropped replies, restarts, cancellation, expiry, quotas, and overwrite/path defenses.
- Independent peer SSH supervisors, loopback forwarding, host verification, error classification, and legacy migration.
- Windows and portable installer allowlists; platform-specific process and launcher tests.

[GitHub Actions](https://github.com/AhmadotEng/codex-bridge/actions/workflows/test.yml) runs the suite on Windows, Ubuntu, and macOS with Python 3.11 and 3.13. Platform-specific tests are skipped when their platform is absent. The POSIX lifecycle test installs into a temporary folder and runs init/start/status/stop against a fake App Server; it does not use a real Codex account.

## Real checks for the onboarding release

On Windows with Python 3.11 and Codex **0.153.4**:

- The selected executable's generated App Server schema passed both ordinary and experimental local-action checks.
- The actual `setup.ps1` entry point installed into a fresh temporary directory with MCP registration deliberately skipped. It detected the local runtime and saved sign-in without exposing account information.
- Running setup again preserved the generated configuration byte-for-byte. Existing live configuration was unchanged.
- Two real Codex turns used the same temporary-workspace conversation. The second recalled the first turn's marker.
- The automatic project title persisted in the stored conversation.
- The worker process was released after each turn.

These checks used one existing local account. They are not a claim that two new people completed the public guide.

## Earlier two-computer verification

During development, two Windows computers using their own local Codex installations and logins exchanged model-driven tasks, replies, and selected files in both directions. Follow-ups retained context and independent projects stayed separate. Disconnect/retry, cancellation, revocation, and SHA-256 file integrity were checked.

App Server schemas were inspected for **0.153.4** and **0.155.0-alpha.2.6**. A real two-client test verified writer release and resume; a desktop-owned conflict failed without dispatching another turn. Fixed-action tools survived release and resume.

Those earlier checks establish the collaboration foundation. They do not imply every existing deployment has been upgraded. Diagnostic transcripts, real conversation IDs, account names, machine names, private paths, and device logs are excluded from this repository.

## Remaining deployment verification

- Two new users completing the public guide with separate accounts and their own SSH connection.
- Real authenticated model turns, desktop chat behavior, and host sandbox policies on macOS/Linux.
- Behavioral testing of additional schema-compatible Codex releases.
- Desktop notifications and automatic sidebar refresh; neither is promised by Bridge.

Bridge verification does not establish the correctness of a particular project or device deployment.
