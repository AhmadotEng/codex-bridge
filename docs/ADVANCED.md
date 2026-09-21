# Advanced operations

The [setup guide](SETUP.md) covers normal two-computer use. This page covers optional packaging, fixed actions, additional peers, and removal.

## Optional local plugin

The repository includes `.codex-plugin/plugin.json` and the `collaborate` skill. MCP-only installation exposes the named tools; plugin installation also exposes the skill and plugin UI metadata.

For a new personal plugin installation on a Codex Desktop installation that includes the official Plugin Creator helper, create the personal marketplace entry **before** copying Bridge into its destination:

```powershell
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

Run those commands from the source checkout, with `$python` and `$codex` set as in the setup guide. The default personal marketplace is discovered automatically. Do not run the scaffold over an existing installation; inspect its current marketplace entry instead.

For an existing personal plugin, run the source installer, then the helper `update_plugin_cachebuster.py` against `~/plugins/codex-bridge`, and repeat `plugin add` using the validated marketplace name. Start a fresh desktop conversation afterward. Do not simultaneously register the same server through both the plugin and a separate `codex mcp add` entry.

Optional automatic approval for this plugin's defined Bridge tools:

```powershell
& $bridge tool-approvals --mode approve --marketplace $marketplace
```

This is only for the plugin installation route. `--mode prompt` restores per-call prompts. It does not relax the peer's local project policy or approve unrelated tools.

## Fixed local actions

An owner may explicitly configure a project-specific operation that ordinary sandboxed Codex commands cannot perform. It is an optional local capability, not part of first-time pairing.

The project's private `local_actions` registry maps IDs to a title, fixed `argv`, absolute `cwd`, SHA-256 `guard_files`, timeout (up to 600 seconds), and bounded JSON output. Keep executable scripts, configuration and dependencies in owner-protected locations outside worker-writable project folders. Do not point an action at a general shell or a script that reads executable commands from untrusted project files.

The local worker receives `bridge_local_action(action_id, request_id)`. It cannot supply additional arguments or paths. The operation runs as the local Bridge owner, while other worker commands keep their sandbox. Windows processes enter an owned Job before they start; cancellation closes only that process tree. It cannot undo external effects already completed.

Each ID is durable. Reusing the same ID returns the original result. After an interrupted process, an action can be `uncertain`; inspect its effects before issuing a new request. The wrapper must emit sanitized JSON only. Raw stderr is not returned.

This feature uses experimental App Server dynamic tools verified for the supported versions. Create a new collaboration session after adding/changing action capabilities. Dynamic tools need the Bridge's handler; opening their conversation directly in desktop does not install those handlers there. Use a normal owner chat with Bridge MCP tools to dispatch such actions.

## Additional peers

Use a new peer ID, exchange separate invitations, and choose dedicated forwarding endpoints. A daemon supports multiple peer records; the built-in SSH supervisor manages **one transport block per configuration**. More simultaneous SSH connections require separately managed forwarding or separate configurations in distinct parent directories.

Do not expand an existing peer's project permissions just because another project needs broader access.

## Retries and progress

Mutations have caller-chosen `request_id` values. Preserve the exact ID and arguments after a timeout. A new ID means new work. `duplicate_conflict` means the same ID was reused with different intent.

If a process dies after dispatch, the request may be marked `uncertain`. This avoids automatic replay of side effects; it does not promise exactly-once completion. Inspect history and actual effects before deciding what to do next.

`task_wait` waits at most 30 seconds. Messages are available through `session_get`. A message starts a peer turn only when `continue_conversation: true` is set. There is no guaranteed desktop popup or autonomous message loop.

## Revocation

Cancel unwanted outgoing tasks before withdrawing a peer's access:

```powershell
& $bridge revoke --peer-id computer-b
```

This disables the local pairing, rejects new requests, and requests cancellation of that peer's local work. Revoke on both PCs to end the relationship. Already completed work cannot be undone; already delivered remote work can need separate cancellation.

Revoking Bridge does not remove separately authorized SSH access.

## Stop and uninstall

On the SSH transport owner, stop its supervisor. On both computers, stop Bridge:

```powershell
& $bridge transport-stop
& $bridge stop
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
