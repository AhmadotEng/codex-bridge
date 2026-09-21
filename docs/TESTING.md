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

[GitHub Actions](https://github.com/AhmadotEng/codex-bridge/actions/workflows/test.yml) is configured for Windows, Ubuntu, and macOS with Python 3.11 and 3.13, plus Windows with Python 3.14. Check the linked workflow for the result at a specific commit. Platform-specific tests are skipped when their platform is absent. The POSIX lifecycle test installs into a temporary folder and runs init/start/status/stop against a fake App Server; it does not use a real Codex account.

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

## Deployed Tailscale and owner-login recovery evidence

A separately deployed **0.1** update was tested on two Windows computers using their existing local Codex logins, with Python 3.11 and 3.14. Its ordinary OpenSSH connection moved to Tailscale while retaining local keys, pinned host verification, existing pairing, and project/session state.

Verified in that deployment:

- Both computers actually rebooted and their owners signed in. Bridge recovered automatically without a manual start.
- Named MCP checks succeeded in both directions after both restarts.
- Existing session IDs, conversation IDs, context, revisions, and workspaces were retained.
- Both task directions, retained-context follow-ups, a second isolated project, and a selected file round trip passed.
- Repeated offline submission with the original task ID did not duplicate execution after reconnection.
- Targeted daemon, SSH-child, and transport-supervisor interruption recovered; deliberate stops and explicit restarts behaved distinctly.
- Disabling and re-enabling future startup preserved running work.

These are deployment observations for the earlier 0.1 branch. **They do not establish two-computer reboot acceptance for the integrated 0.3 public release.** The newer release combines those lifecycle concepts with the public onboarding, multiple-peer transports, and larger-file transfers; its isolated tests and CI must be assessed separately.

Private deployment reports and detailed process/network records remain outside this repository. No real addresses, computer names, task registrations, conversation IDs, or private backup locations are needed to reproduce the public setup.

## Integrated 0.3 Windows startup check

The integrated source was installed into a fresh temporary directory with its own configuration and unused loopback port. A real Windows scheduled task ran the daemon using the selected local Codex runtime. This check verified:

- Hidden registration with an Interactive, Limited owner principal.
- Startup and a repeated start retaining one daemon process.
- Disabling and re-enabling future startup without replacing the running daemon.
- A deliberate stop remaining effective when the scheduler task was started again.
- An explicit start resuming operation afterward.
- Private configuration remaining byte-for-byte unchanged.
- Cleanup stopping the temporary daemon and removing its task registration.

This used Windows, Python 3.11, and Codex 0.153.4. It executed no model turns, did not restart the computer, and did not change either existing Bridge installation. Automated tests separately cover independent peer startup, stale process identities, stop/start races, and Windows child cleanup after supervisor termination.

## Deployed runtime repair evidence

In the separately deployed **0.1** branch, a selected Windows **0.155.0-alpha.2.6** runtime contained `codex.exe` but lacked the three observed companions listed in [RUNTIME.md](RUNTIME.md). App Server accepted tasks and produced replies while native tool execution failed. The evidence did not establish when or why the files became absent.

Only `codex_path` changed, selecting an already installed complete **0.153.4** distribution. Local executable hashes matched a previously verified distribution manifest and Windows reported valid publisher signatures. No binaries were downloaded or mixed across versions. Account configuration, model settings, scopes, local actions, pairing, transports, and startup registration remained unchanged.

After an idle daemon restart through the existing owner-login task, the same retained conversation completed a native PowerShell command with exit code zero. It read a selected tiny input, refused an existing output overwrite, created a proof file, and calculated its SHA-256. The selected output was independently retrieved and matched the expected bytes/hash. Actual command evidence was inspected from App Server history; dialogue completion alone was not treated as proof.

This establishes native execution for that existing deployment and task. It does not establish gameplay readiness, a fresh two-user setup, or end-to-end acceptance of the public **0.3.1** package. Private paths, IDs, hashes, proof files, and repair reports are excluded from this repository.

## Public 0.3.1 runtime checks

The public update adds known Windows bundle checks alongside schema compatibility, explicit static-preflight limits, and per-turn structured command evidence. The [runtime guide](RUNTIME.md) distinguishes schema acceptance, companion-file checks, actual native execution, and verification of file effects.

The Windows/Python 3.11 run passed **212 isolated tests**, with three POSIX-only tests skipped. Coverage includes incomplete, empty, unreadable, and invalid companions; schema-compatible unknown layouts; configuration preservation during runtime selection; and bounded command evidence with duplicate, foreign-turn, malformed, and early notifications. Both installer allowlists include the runtime guide and exclude the private repair report. The installed complete Codex 0.153.4 bundle also passed a read-only version/schema/bundle probe, which explicitly reported native execution as `not_checked`.

Assess its isolated tests and the CI result for the published commit separately from the deployed repair above. Static preflight does not run a model turn or project command. Unknown runtime layouts remain unverified even if their generated schema is compatible.

## Remaining deployment verification

- Two new users completing the public guide with separate accounts and their own SSH connection.
- Actual two-computer reboot/login acceptance of the integrated 0.3 package.
- Separate sign-out/sign-in, screen lock/unlock, and sleep/wake cycles; these were not established by the earlier reboot tests.
- Real authenticated model turns, desktop chat behavior, and host sandbox policies on macOS/Linux.
- Behavioral testing of additional schema-compatible Codex releases.
- Desktop notifications and automatic sidebar refresh; neither is promised by Bridge.

Bridge verification does not establish the correctness of a particular project or device deployment.
