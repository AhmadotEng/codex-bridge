# Advanced operations

The [setup guide](SETUP.md) covers normal two-computer use. This page covers optional packaging, maintenance permissions, fixed actions, additional peers, and removal.

## Full-access maintenance

An owner may configure a dedicated project with `project-select --policy full-access` to let its Bridge worker execute authorized maintenance directly under the owner's account, including commands outside the workspace and network access. `read-only` remains the default; existing projects are not upgraded automatically. The initial policy is selected locally, never by peer RPC. See [Windows/Linux activation and conversation ownership](PERMISSIONS.md).

Full access does not enable an SSH shell, copy account/private-key credentials, or confer root/administrator privileges. Plan a recovery path before a maintenance task stops or upgrades its own daemon. A stopped daemon still needs owner-local startup.

## Optional local plugin

The repository includes `.codex-plugin/plugin.json` and the `collaborate` skill. MCP-only installation exposes the named tools; plugin installation also exposes the skill and plugin UI metadata.

For a new personal plugin installation on a Codex Desktop installation that includes the official Plugin Creator helper, create the personal marketplace entry **before** copying Bridge into its destination:

```powershell
$python = (Get-Command python -CommandType Application -ErrorAction Stop).Source
$codex = (Get-Command codex -CommandType Application -ErrorAction Stop).Source
$creator = Join-Path $env:USERPROFILE '.codex\skills\.system\plugin-creator\scripts'
$scaffold = Join-Path $creator 'create_basic_plugin.py'
if (-not (Test-Path -LiteralPath $scaffold)) { throw 'Use the MCP setup route; this helper is unavailable.' }
& $python $scaffold codex-bridge --with-marketplace --with-mcp --with-skills
if ($LASTEXITCODE -ne 0) { throw 'Inspect the existing plugin/marketplace before continuing.' }
& .\scripts\install.ps1 -PythonExe $python
$marketplace = & $python (Join-Path $creator 'read_marketplace_name.py')
if ($LASTEXITCODE -ne 0) { throw 'Marketplace validation failed.' }
& $codex plugin add "codex-bridge@$marketplace"
```

Run those commands from the source checkout. If either executable is absent from PATH, assign its actual full path to `$python` or `$codex` first. The default personal marketplace is discovered automatically. Do not run the scaffold over an existing installation; inspect its current marketplace entry instead.

For an existing personal plugin, run the source installer, then the helper `update_plugin_cachebuster.py` against `~/plugins/codex-bridge`, and repeat `plugin add` using the validated marketplace name. Start a fresh desktop conversation afterward. Do not simultaneously register the same server through both the plugin and a separate `codex mcp add` entry.

Optional automatic approval for this plugin's defined Bridge tools:

```powershell
& $bridge tool-approvals --mode approve --marketplace $marketplace
```

This is only for the plugin installation route. `--mode prompt` restores per-call prompts. It does not relax the peer's local project policy or approve unrelated tools.

## Fixed local actions

An owner may explicitly configure one fixed project-specific operation while keeping the rest of a worker's commands sandboxed. This is useful when broad full access is unnecessary; it is optional and separate from first-time pairing.

The project's private `local_actions` registry maps IDs to a title, fixed `argv`, absolute `cwd`, SHA-256 `guard_files`, timeout (up to 600 seconds), and bounded JSON output. Keep executable scripts, configuration and dependencies in owner-protected locations outside worker-writable project folders. Do not point an action at a general shell or a script that reads executable commands from untrusted project files.

The local worker receives `bridge_local_action(action_id, request_id)`. It cannot supply additional arguments or paths. The operation runs as the local Bridge owner; other commands retain that project's chosen execution policy. Windows processes enter an owned Job before they start; cancellation closes only that process tree. It cannot undo external effects already completed.

Each ID is durable. Reusing the same ID returns the original result. After an interrupted process, an action can be `uncertain`; inspect its effects before issuing a new request. The wrapper must emit sanitized JSON only. Raw stderr is not returned.

This feature uses experimental App Server dynamic tools verified on the documented tested versions. Other versions must pass the optional dynamic-tool schema checks; that alone is not behavioral verification. Create a new collaboration session after adding/changing action capabilities. Dynamic tools need the Bridge's handler; opening their conversation directly in desktop does not install those handlers there. Use a normal owner chat with Bridge MCP tools to dispatch such actions.

## Additional peers

Use `pair-setup` with a new peer ID, then configure outbound SSH settings and the shared candidate map on both endpoints. Each peer needs distinct forwarding listeners and independent receiving authorization. Use `connect`, `connection-status`, and `disconnect` with an explicit peer. See [candidate configuration](CONNECTIONS.md#pairing-and-route-configuration).

The legacy `ssh_transport`/`ssh_transports` configuration remains readable. Legacy `transport-start`, `transport-stop`, and `transport-status` are migration controls, not the normal on-demand workflow. If an old route lacks `peer_id`, its original peer must be unambiguous before adding another. Retire its old supervisor through its verified owner before reusing its ports; the managed route must not compete for them.

Do not expand an existing peer's project permissions just because another project needs broader access.

## Retries and progress

Mutations have caller-chosen `request_id` values. Preserve the exact ID and arguments after a timeout. A new ID means new work. `duplicate_conflict` means the same ID was reused with different intent.

If a process dies after dispatch, the request may be marked `uncertain`. This avoids automatic replay of side effects; it does not promise exactly-once completion. Inspect history and actual effects before deciding what to do next.

`task_wait` waits at most 30 seconds. Messages are available through `session_get`. A message starts a peer turn only when `continue_conversation: true` is set. There is no guaranteed desktop popup or autonomous message loop.

Files over 8 MiB use negotiated chunking. The initial send/fetch returns a transfer request; use `artifact_transfer_status` to inspect progress and the completed artifact. Reuse the original operation and request ID to resume. `artifact_transfer_cancel` abandons an unfinished transfer without deleting an already completed destination. Receiver quotas and expiry bound staging storage. Keep both computers on this release for large transfers.

## Update an existing installation

Test the candidate in a separate empty directory/configuration first. Before an in-place upgrade, drain local workers, record process/startup ownership, and retain the previous source and local settings for rollback. Coordinate any daemon maintenance interval. Preserve a working recovery connection; do not stop unrelated tunnels to copy code.

Source installers refuse unrelated nonempty directories, conflicting local `.mcp.json`, and changed installed code unless you explicitly request an upgrade. After stopping the affected local workers, use Windows `scripts/setup.ps1 -Upgrade` or Linux `sh scripts/setup.sh --upgrade`. All selected payload files are validated and staged before replacing installed files. Matching local launchers are preserved byte-for-byte; conflicts must be resolved locally. This never copies source `.mcp.json`, private configuration, or state. The optional zipapp deliberately refuses upgrades; use a fresh directory or the source installer.

Keep the same private configuration and MCP registration path. A different selected configuration is not an upgrade: use a distinct installation/registration, or deliberately repair the local launcher after backup. If MCP registration conflicts, inspect it rather than deleting the other registration. Private config and database files should remain outside the source installation. Installer file staging does not replace the requirement to stop workers.

After both peers are ready, run local runtime checks and retained-session tests. Choose waiting daemon startup explicitly. Review old transport startup separately so an upgrade does not preserve unwanted automatic dialing unnoticed. Follow the [connection migration](CONNECTIONS.md#migrate-without-losing-work) before managed dual initiation. Roll back source/registration if needed, without replacing newer state/results with an old database snapshot.

An MCP-only installation at the same path needs no new registration. A plugin installation needs the cachebuster/reinstall flow above and a fresh Codex conversation to load the new tools. Do not register both routes.

## Change only the Codex runtime

For missing runtime helpers or a different local Codex selection, use the [path-only repair procedure](RUNTIME.md#repair-only-the-selected-runtime). Finish active tasks, stop only the local daemon, run `setup --codex FULL_PATH --batch`, and use `start` with the existing launch configuration. Preserve working transports, pairing, model/authentication settings, project scopes, sessions, and startup registration. Do not copy individual helpers between versions or overwrite state with an old backup.

Static schema and bundle checks do not execute a native command. Verify the repair with an authorized command in the retained conversation and inspect `execution_evidence`; verify selected file bytes/hash separately where applicable.

## Owner-login startup

`autostart-enable` is an explicit local-owner operation. Its default and retained `--component auto` alias register **only the daemon**. `--component daemon` is the recommended explicit form. Persistent transport startup requires `--component transport --peer ID`; adding a peer never opts it in. Waiting startup launches no SSH clients, including when results are pending.

Enabling registers future startup and does not start anything immediately. Use `start --interactive` on Windows, `start --background` on Linux, and an explicit `connect --peer ID --request-id UUID` to collaborate. Windows tasks use the owner's interactive login at limited privilege; they do not store a Windows password or run Codex before login. This uses Windows' [scheduled-task principal modes](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal?view=windowsserver2025-ps).

`autostart-status` distinguishes registration, enabled configuration, stop requests, and observed supervisor identity. Process observations do not establish remote readiness; use `preflight` and a completed harmless task.

A daemon `stop` is distinct from a peer `disconnect`. Peer disconnect intent is durable across login/restart and requires explicit local resume; incoming traffic cannot clear it. The legacy daemon/startup wrapper also respects its owner stop marker, with platform-specific login semantics. Only Bridge-owned, identity-verified processes may be recovered or stopped.

`autostart-disable` disables future login triggers while preserving running work. It is separate from stopping a component. `autostart-disable --remove` removes selected registrations only after their work and wrappers have stopped. Additional routes retain separate startup, stop markers, and scopes.

When upgrading a legacy single transport, stop its old supervisor first and explicitly enable the intended named peer. An unbound legacy transport registration is not permission to start every new peer. Preserve current task/session state and existing request IDs during migration.

Linux registration uses a systemd user unit where available; it does not enable root execution or lingering. Other environments use manual starts. [Tailscale network startup](TAILSCALE.md#3-keep-the-network-available-after-restart) and the receiving SSH service remain separate OS prerequisites. Configuration/registration checks and actual sign-in/reboot tests are reported separately.

## Revocation

Cancel unwanted outgoing tasks before withdrawing a peer's access:

```powershell
& $bridge revoke --peer-id computer-b
```

This disables the local pairing, rejects new requests, and requests cancellation of that peer's local work. Revoke on both PCs to end the relationship. Already completed work cannot be undone; already delivered remote work can need separate cancellation.

Revoking Bridge does not remove separately authorized SSH or Tailscale access. Disable the peer's startup with `autostart-disable --component transport --peer ID` when ending that route permanently.

## Stop and uninstall

On Windows, first disable future startup on both computers:

```powershell
& $bridge autostart-disable
```

On the SSH transport owner, stop its supervisors. On both computers, stop Bridge:

```powershell
& $bridge transport-stop
& $bridge stop
```

After confirming the selected components and startup wrappers have stopped, remove Windows registrations:

```powershell
& $bridge autostart-disable --remove
```

For MCP registration:

```powershell
& $codex mcp remove codex_bridge
```

For plugin registration instead:

```powershell
& $codex plugin remove "codex-bridge@$marketplace"
```

These commands preserve project files and retained conversations. The installed source under `~/plugins/codex-bridge` and private configuration/state under `~/.codex-bridge` can then be kept or deliberately removed by their owner. Confirm the processes are stopped before removing them. Do not delete unrelated SSH configuration, keys, projects or tunnels.

For transport rollback, restore only the affected route's former endpoint and prerequisites, then start once. Keep current databases, work, and deduplication records. Reverting an older live-state snapshot can discard new results. See the [Tailscale migration and rollback guide](TAILSCALE.md).
