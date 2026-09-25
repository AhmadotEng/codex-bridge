# Verification

## 0.3.2-rc.5 cleanup correction and receiver evidence

rc.5 keeps exact SSH process ownership until exit and cleanup journaling succeed. An unconfirmed close reports `ssh_cleanup_unconfirmed`, retains its child for an explicit local disconnect retry, and blocks competing dialing. Caller cancellation does not abandon cleanup. A failed duplicate close preserves the selected route, while rejected unselected candidates are cleaned up. Revocation cannot authorize a remote call or prevent local cleanup of the retained owned process. A restarted daemon does not adopt a saved PID as a new process handle.

Windows/Python 3.11 verification passed **409 tests: 401 passed and eight platform skips**, in 131.172 seconds. The 21 new cleanup regressions also passed independent review, including both reproduced candidate-retention and revoked-peer retry failures. Plugin manifest validation passed. Linux/package and live rc.5 acceptance remain pending.

The following receiver checks were performed separately on the existing rc.4 deployments on September 25, 2026; they do not establish rc.5 managed-connection acceptance.

| Receiver check | Observed result |
| --- | --- |
| Linux initiates to Windows | Public-key and pinned-host authentication passed. Both allowed forwarding directions carried authenticated Bridge status calls identifying the expected peer. |
| Windows receiving restrictions | Authenticated shell, SFTP, an unapproved local-forward destination and an unapproved reverse listener were rejected. Five test SSH children exited; temporary listeners were verified absent. |
| Windows runtime repair | A dedicated machine runtime fixed an authentication-child dependency-loading failure. Existing host keys, peer-restricted firewall, Bridge source/configuration and legacy carrier remained intact. |
| Linux receiving policy | A narrowly matched receiver policy and one public-key line gained the new candidate listener while preserving the existing listener. Both fixture runs passed 23 tests and all 40 native checks succeeded. One reload retained the SSH service process and working sessions. |

The Windows receiver remains manually started during acceptance. Agent-forwarding denial is supported by its effective policy, not a separate live channel test. Actual owner sign-in/reboot, managed either-origin and simultaneous initiation, and the full recovery/cancellation/revocation matrix remain pending.

## 0.3.2-rc.4 startup and live-upgrade evidence

The live rc.3 rollout revealed that Windows took about 2.05 seconds to report an explicitly refused loopback connection, exceeding the startup probe's two-second limit. Independent isolated probes reproduced a timeout at two seconds and WinError 10061 after approximately 2.03–2.05 seconds when given more time. The previous live code and exact configuration were restored; all 662 pre-window records, saved artifacts, startup registration and the existing SSH carrier were preserved.

rc.4 gives Windows startup probes five seconds; other platforms retain two seconds. Only an explicit connection refusal allows a launch. Timeouts, failed authentication and malformed responses still prevent a second daemon. Readiness probes and sleeps are capped to the remaining 30-second readiness window. Focused tests use real isolated sockets for refused, healthy, slow, wedged and unauthorized endpoints, with launches mocked. The actual packaged and deployed lifecycle checks below provide separate evidence.

Windows/Python 3.11 source verification completed with **388 tests: 380 passed and eight platform skips**, in 130.446 seconds. The 43 focused CLI/entrypoint tests passed before the full suite. These results verify the corrected probe logic and preserved rejection behavior; they do not substitute for the packaged or live upgrade checks.

The functional candidate pins source revision `b559fa0ffd228699f127b9d2e704c275436bf7e4`. Its final correction changes only a macOS test fixture. All seven [Windows, Ubuntu and macOS CI jobs for that revision](https://github.com/AhmadotEng/codex-bridge/actions/runs/36164294180) passed.

| Check | Observed result |
| --- | --- |
| Packaged installation | Matching Windows/Linux 43-file payloads; installation, reinstallation, upgrade, private fixture configuration preservation and 21 MCP tools passed. |
| Actual Windows live upgrade | Passed on September 25, 2026 at 17:18:57 UTC: rc.4 started once through the existing owner-login task; authenticated local readiness and legacy Linux availability passed. |
| Windows preservation | All 701 saved record bodies and 248 artifact blobs preserved. Pairing, project scopes, MCP launcher, v1 startup metadata/task XML and the existing SSH carrier stayed intact. Only the already-disabled legacy transport gained its required peer ID field. |
| Actual Windows startup lifecycle | Isolated background and existing-v1 scheduled-task paths passed status, repeated-start process reuse, normal stop and cleanup. The live upgrade also used its existing task. These were not sign-in/reboot tests. |
| Post-upgrade native execution | Actual native commands completed in both directions. A selected file returned with matching bytes and SHA-256. Replaying the exact original task returned its original completed turn without another execution. A subsequent follow-up recalled the correct marker/hash in the same conversation with no tools. |
| Exact rc.4 Linux acceptance | 388 source tests: 372 passed, 16 Windows-specific skips, no failures. All 14 helper fixtures passed. Installation, reinstallation, upgrade, 21 MCP tools and actual daemon start/status/reuse/stop passed. |
| Actual Linux live upgrade | Final release started once at 17:39 UTC on September 25, 2026; authenticated remote readiness and a native command in the retained conversation passed. The original SSH carrier remained intact. |
| Linux preservation | All 43 installed payloads and 173 artifact files (10,489,647 bytes) verified. All 314 previous journal keys retained: 313 record bodies identical; only the maintenance session timestamp changed. Config, pairing, scopes, MCP and startup fingerprints were preserved. No recovery or database rewind. Sanitized reports returned directly through Bridge. |

The initial Windows preservation checker encountered an inherited PowerShell module-path conflict. A separate read-only checker omitted that variable from its own child environment and passed all 34 checks. Installed Bridge code and global environment settings were unchanged; no additional daemon start or rollback was needed.

The Linux read-only verifier initially assumed the payload manifest would be installed. The installer intentionally omits that bundle metadata; checking the verified staged manifest and all installed payload hashes corrected the verifier without installation changes. Native proof creation and both report transfers exited successfully. Unprivileged socket inspection could not attribute the legacy listening socket to privileged sshd; separate SSH creation-identity checks and authenticated Bridge traffic established the preserved route.

The Linux service implementation is byte-identical to the accepted rc.3 implementation. The shared CLI readiness changes were separately reviewed and exercised on Linux; the earlier 122-second real user-service test was not repeated. After the live upgrade, rc.4 daemon-only owner-login registration succeeded once through the normal CLI. The exact owned user unit was enabled but stayed inactive with MainPID 0; registration did not start it. Seven post-registration samples spanning 131.588 seconds showed the existing manual daemon and SSH process identities unchanged, no managed connection/demand/child records, and no transport startup enabled. Config, MCP, all 43 source payloads and the Fedora vendor policy were unchanged. Sampling cannot exclude a transient process between observations. This is registration/idle verification; fresh rc.4 user-service activation and actual login/reboot remain unverified.

These results concern the existing deployments and their preserved legacy route. Managed initiation from both computers, simultaneous initiation, opposite-origin reconnection with task deduplication, and actual sign-in/reboot recovery remain separate pending checks. Published release revision `bf0d8a86a6749828ccdb8b6806e54208fd53fd10` has all 41 functional payload files byte-identical to the accepted candidate; only README and testing documentation changed. All seven [final CI jobs](https://github.com/AhmadotEng/codex-bridge/actions/runs/36166680241) passed and all seven published downloads were independently hash-verified. The release manifest and `SHA256SUMS` identify those immutable assets; subsequent deployment evidence updates these notes without replacing the assets.

## 0.3.2-rc.3 Linux user-unit correction

Actual rc.2 testing on Fedora 44 found that systemd rejected the quoted `WorkingDirectory` as a non-absolute path. rc.3 serializes that directive as a literal absolute directory, escaping percent specifiers and preserving spaces, quotes, backslashes, and trailing whitespace. Command and environment quoting remain separate.

Regression tests cover replacement of an inactive Bridge-owned rc.2 unit, unchanged unit identity/configuration, exact rollback on registration failure, and preservation of an active unit until coordinated shutdown. A Linux-only test runs the real `systemd-analyze --user verify` parser against generated units and verifies that the former quoted form is rejected. This parser test uses isolated paths and never registers or starts a service. The separate actual owner-manager test below subsequently passed; login/reboot verification remains pending.

An initial unpublished rc.3 candidate passed Fedora's parser check but its actual service lifecycle exposed another issue: the ownership guard rejected Fedora's inherited `10-timeout-abort.conf` policy. The candidate was disabled and remained inactive; live services were preserved. The corrected guard accepts only that exact vendor file with verified root ownership, safe filesystem provenance, and the sole `TimeoutStopFailureMode=abort` setting. Regression coverage rejects extra directives, unknown/per-unit overrides, unsafe ownership/permissions, symlinks, substituted file identities, and modified unit fragments. Existing explicit stops, active-unit protection, and rollback remain enforced. These unit tests do not substitute for the corrected candidate's actual service acceptance.

### Completed corrected-candidate acceptance

**Historical rc.3 Windows rollout failed:** upgrading the existing host stopped at `local_endpoint_unverified` before daemon launch. The previous code and configuration were restored while retaining the current database, artifacts and SSH carrier. The cause and successful rc.4 follow-up are recorded above. The results below do not claim a successful rc.3 Windows upgrade; its published artifact bytes remain unchanged.

The published [rc.3 release](https://github.com/AhmadotEng/codex-bridge/releases/tag/v0.3.2-rc.3) pins source revision `2e3283bd46a4008fb0edcd4b9a44138fab5d7661`. Windows ran 378 tests with 370 passes and eight platform skips. The receiving Fedora 44 owner worker ran the exact candidate under Python 3.14.7 and systemd 259: 378 tests, 363 passes, 15 platform skips, zero failures. All seven [CI jobs for that revision](https://github.com/AhmadotEng/codex-bridge/actions/runs/36161253251) passed. Windows/Linux installer payloads matched across all 43 selected files; all seven published downloads were independently hash-verified.

The actual isolated Linux user service started through the normal CLI and remained healthy for **122.68 seconds**, with stable process identity and 13 authenticated local-status samples. Daemon-only registration preserved a deliberate stop without starting it; explicit start resumed it. An offline loopback peer fixture and pending work did not trigger a connection demand: the manager remained idle at generation zero with no demand or managed-child records. The saved completed result was unchanged and pending work remained pending. No fake adapter or modified candidate code was used in this service run.

Independent cgroup process polling at nominal 0.1-second intervals observed only runner and daemon across 1,141 samples. Polling cannot exclude shorter transient processes; persistent manager records provide complementary evidence. Normal CLI stop and unregister passed, leaving no candidate unit, enablement link, listener or processes, while preserving the vendor policy and live installation fingerprints. Sanitized results and evidence returned directly through Bridge. This is actual service lifecycle acceptance, **not** sign-in/reboot or two-origin managed-connection acceptance. Both live daemons and their legacy route were unchanged during this isolated check.

## 0.3.2-rc.2 owner-selected execution policy

This candidate adds `full-access` for an explicitly selected local project. Existing project defaults and waiting-only startup are unchanged. A peer task cannot change its receiving project's policy. The installed runtime must validate the full-access request and response schemas before any such turn starts; an unsupported runtime does not fall back to another policy.

On **Windows, Python 3.11, and Codex 0.153.4**, an isolated native verification completed two turns in the same dedicated conversation. The first fetched a marker from a temporary loopback HTTP endpoint and wrote/read the exact response in an owner-selected fixture file outside the workspace. The second recalled a conversation-only marker, read the file, and removed that exact file. Both turns produced native command completion evidence with successful exits; the HTTP server independently observed requests, and the harness independently checked the file and its removal. The worker released its App Server after each turn. The existing live Bridge configuration remained byte-for-byte unchanged.

On **Fedora 44 and Codex 0.155.1**, the receiving owner reported an independently verified rc.2 candidate: all 43 payload hashes matched; installation/reinstallation and MCP initialization passed; 365 tests ran with 350 passed and 15 skipped. Two native full-access turns passed external-fixture write/read/removal, loopback HTTP, retained context, and writer release. A manually started isolated daemon waited for 120 seconds without SSH children or connection demands. The systemd start failed as described above; the temporary candidate unit was removed and live startup remained unchanged.

The owner subsequently promoted the live Linux daemon to rc.2 using manual startup and reported preservation of all 124 earlier database records and saved artifact bytes. A new paired maintenance project then completed two actual full-access turns dispatched from the Windows Bridge: an external temporary fixture was created/read/removed, a loopback HTTP request was observed, and the second turn recalled a conversation-only marker in the same thread. All four native commands exited successfully; the App Server closed after each turn. Both proof artifacts arrived directly on Windows with matching SHA-256 checksums. The old restricted project and conversation remained intact.

This verifies live Linux maintenance execution and direct task/result/artifact exchange over the preserved legacy route. Windows still used its older daemon during that paired test. It does not prove public-internet access, administrator elevation, the new either-origin connection protocol, corrected systemd startup, or actual login/reboot recovery. The earlier connection/startup results below remain version-specific evidence.

## 0.3.2-rc.1 waiting/connection candidate

This prerelease changes connection management and is **experimental**. Earlier working Windows/Linux sessions below used the legacy route and do not verify the new two-candidate selection protocol. Assess candidate source tests, package installation, real receiving authentication, model tasks, and OS startup/reboot separately.

### Maintainer verification on 2026-09-25

- **Windows / Python 3.11:** 334 isolated tests ran successfully; seven platform-specific tests were skipped. This includes 32 manager protocol cases and nine two-Bridge HTTP integration cases. Tests cover delayed cleanup ownership and committed-response replay, missing-progress clocks near their epoch, post-selection authorization failure, simultaneous selection, lost acknowledgments, opposite-origin reconnection without task replay, stop/resume, cancellation, revocation, pending results, work arriving during idle drain, interrupted retry budgets, and acknowledged-progress requirements. SSH carriers and model execution are simulated in these protocol tests.
- **Actual Windows OpenSSH loopback:** separate temporary receivers and dedicated test keys passed A-origin, B-origin, and simultaneous initiation. Both authenticated Bridge directions worked over the selected tunnel, healthy reuse created no second client, and loser cleanup retained one client. Fake-adapter tasks and deduplication worked both ways. Authenticated shell-command and SFTP session requests were denied. All test receivers/clients exited and no isolated listeners remained. This is real SSH on one Windows computer, not a two-computer Linux or native-model test.
- **Actual Windows task invocation:** an isolated hidden, limited, interactive-owner daemon task was manually invoked and observed for 126 seconds. It retained queued work and a saved result with zero SSH launches, zero connection demands, and zero connection children. Its exact task and owned processes were removed after the check. This exercised Task Scheduler startup, not an actual sign-out/sign-in or reboot. A harmless test adapter replaced account/model execution.
- **Packaged Windows installation:** the extracted ZIP installed into a separate directory, preserved a pre-existing private configuration, matched all 42 payload hashes, and exposed 21 MCP tools while its daemon was absent. The check did not register an MCP server globally or replace the live installation.
- **Existing deployment migration:** the unwanted legacy transport startup and offline supervisors were stopped while the same daemon, pairing, project scopes, and saved sessions stayed available. Saved legacy routes remain available for a coordinated migration. This did not promote either endpoint to the new protocol.

Linux systemd ownership/rollback is covered with a simulated user manager. Real Linux receiving access, new-protocol Linux tasks, and actual Linux login/reboot observation remain pending. GitHub Actions results are separate per-commit evidence, not a substitute for those deployment checks. Detailed local reports retain source hashes; private key/configuration directories and machine-specific evidence are excluded from release bundles.

Installer checks use only temporary directories: Windows setup and bridge wrappers select installed code even with a shadow `codex_bridge` in the caller's directory, while retaining relative argument semantics. Tests cover incomplete/invalid sources, unrelated destinations, explicit upgrade requirements, byte-preserved matching MCP launchers, conflicting configurations, staged rollback, unchanged private state, and zipapp integrity. The two OS package formats are generated from the same allowlist, compared byte-for-byte against their payload manifest, rebuilt for determinism, and checked for exclusion of unselected private fixture files. These checks do not modify a live account, service, pairing, or route.

The new managed release still needs exact-candidate real Windows/Linux acceptance: each initiating direction, simultaneous initiation and loss of acknowledgments, native task/result deduplication after opposite-origin reconnection, bounded recovery and offline waiting startup, and actual owner sign-in/reboot behavior. Service registration/configuration alone is not reboot verification. See [connection behavior and migration](CONNECTIONS.md).

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

### Unpublished Linux launcher preview (0.3.2-dev.0)

The source preview adds `scripts/setup.py`, `scripts/setup.sh`, and a deterministic Python `.pyz` builder/bootstrap. Windows regression testing completed **244 tests with seven platform-specific skips**. Source review and focused launcher tests cover configuration preservation, refusal to overwrite a different installation, payload integrity/path validation, bounded reads, and sanitized diagnostics.

Before the owner requested stopping further VM testing, an isolated Alpine 3.24.2 x86_64 guest with Python 3.14.7 completed the portable installer suite (five passed, one Windows-only skip), version/schema/App Server initialization against the complete verified official Codex 0.153.4 Linux musl package, and actual bidirectional OpenSSH loopback forwarding with Windows. These checks used no Codex account or model turns. The broader Linux suite and one-file lifecycle checks were subsequently interrupted; **they are not complete Linux acceptance results**. The disposable VM and its owned listeners were stopped, and existing deployments were unchanged.

The bundled zsh in that Codex package requires a glibc loader absent on the Alpine guest. Version/schema success does not establish native command or sandbox readiness. No libc or sandbox workaround was applied. The later Fedora checks below describe a different runtime and system; they do not establish Alpine or ARM64 compatibility. Desktop integration and optional Linux login startup still require verification. See [LINUX.md](LINUX.md). No preview commit, push, or release publication was performed.

### Friend-reported Fedora validation (2026-09-24)

The connection coordinator relayed the friend's results for the delivered **0.3.2-dev.0** preview on **Fedora 44 x86_64**, **Python 3.14.7**, and locally signed-in **Codex CLI 0.155.1**. These are **friend-reported results, not host-reproduced verification**:

- Supplied package hashes and all 37 launcher payload files matched.
- The test suite reported 230 passes and 14 Windows-specific skips.
- Setup, repeated setup, daemon stop, and restart passed; the local daemon listened on loopback.
- MCP initialization, listing 16 tools, and `session_list` passed.
- A real native App Server command ran with bundled zsh, a read-only sandbox, and network disabled. The friend reported independently checking a matching output SHA-256.

At the time of the report, no peers, projects, or Bridge SSH routes were configured, and the Linux SSH server was inactive. Tailscale connectivity was already complete according to the owner; installation or enrollment was not outstanding. The coordinator retained the Linux computer's chosen peer identity while preparing a separate pairing, preserving the existing Windows deployment and conversations.

This initial report established a reported local command check; remote Bridge tasks had not yet been exercised. The subsequent live verification below supersedes that connection status. Desktop behavior, ARM64, and Linux login/reboot startup remain outside this report. Personal identifiers, private paths, invitations, and detailed command output are kept out of these repository notes.

### Live Windows-to-Linux verification (2026-09-25 UTC)

The connection coordinator subsequently verified the existing **Windows 0.1** deployment against the delivered **Fedora 0.3.2-dev.0** preview and locally signed-in **Codex CLI 0.155.1**. The repository maintainer reviewed the saved report; this documentation update did not rerun its tests or change either deployment.

- Ordinary SSH over Tailscale authenticated with a pinned host key, reciprocal Bridge invitations were imported, and the scoped test project became available.
- A Windows-originated Bridge task ran a real native command on Linux. App Server recorded command completion with exit code zero; dialogue completion alone was not treated as proof.
- A selected 119-byte file completed a Windows-to-Linux-to-Windows round trip. Returned bytes and SHA-256 matched the source.
- A follow-up in the same Linux conversation recalled the requested context marker and exact file hash without tools. The owned App Server closed after both turns; no duplicate test conversation was created.
- An initial zsh here-document failed while attempting a temporary file in the read-only sandbox. The model changed the command to `python3 -c` and succeeded without relaxing permissions or sandbox policy.

The older Windows installation and its existing peers, conversations, and routes were preserved. A separate transport-only supervisor provided the new route; it did not run another Bridge daemon. SSH access for this pairing is restricted to forwarding, not an administrative shell or SFTP.

TCP port 22 initially timed out. It became reachable after the owner confirmed invitation acceptance and the coordinator refreshed the Tailscale network map. The `ShareeNode:true` status field remained present during success, so that field alone does **not** establish blocked sharing. The successful SSH and authenticated Bridge checks establish reachability; these observations do not isolate the cause of the earlier timeout.

**Still unverified:** a reverse-originated model task from the friend's ordinary Codex, Linux or new-route automatic startup and reboot persistence, and Linux desktop conversation behavior. Linux services and the new Windows route had no automatic startup configured during this check. The successful artifact round trip does not prove a reverse-originated model task. Transfers to the older Windows host remain limited to 8 MiB each. This is one existing mixed-version deployment, not acceptance of a fresh two-user public installation or any game project.

### Deployment checks still required

- Two new users completing the public guide with separate accounts and their own SSH connection.
- Actual two-computer reboot/login acceptance of the integrated 0.3 package.
- Separate sign-out/sign-in, screen lock/unlock, and sleep/wake cycles; these were not established by the earlier reboot tests.
- Real authenticated model turns on macOS, broader Linux runtime/sandbox coverage beyond the Fedora check above, and desktop chat behavior on macOS/Linux.
- Reverse-originated Linux model tasks and Linux/new-route startup and reboot persistence for the preview pairing.
- Behavioral testing of additional schema-compatible Codex releases.
- Desktop notifications and automatic sidebar refresh; neither is promised by Bridge.

Bridge verification does not establish the correctness of a particular project or device deployment.
