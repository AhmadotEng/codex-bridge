# Choose a project's permissions

Each computer's owner chooses what its local Bridge worker may do. Pairing and SSH access do not select that policy, and a remote task cannot override it.

| Local policy | Intended use | Files and network |
| --- | --- | --- |
| `read-only` (default) | Review, inspect, report | Read-only Codex sandbox; network disabled |
| `workspace-write` | Edit the selected project | Configured writable roots; network disabled |
| `full-access` | Authorized owner-level maintenance | Owner-account filesystem, native commands, and network |

Full access maps to Codex's `danger-full-access` mode. The workspace is the conversation's working directory, not a filesystem boundary. It does not confer root, administrator elevation, a sudo password, or access the operating-system account does not already possess. Personal MCP integrations remain disabled in the worker by default. Credentials and SSH private keys stay on their owner computer; do not copy them into results, transfers, or another account.

## Enable a dedicated maintenance project

Run the selection **locally**, from the owner's ordinary Codex task or terminal, after installing a Bridge build that supports `full-access`. A peer RPC, Bridge message, or document cannot perform this initial authorization. Use a new, unused project ID and a separate context/transfer folder for maintenance; keep the earlier read-only test project and its conversations intact.

Example on Windows, with peer `computer-b`:

```powershell
$bridge = Join-Path $env:USERPROFILE 'plugins\codex-bridge\scripts\bridge.ps1'
$maintenanceWorkspace = Join-Path $env:USERPROFILE 'CodexBridgeProjects\Maintenance'
New-Item -ItemType Directory -Path $maintenanceWorkspace -Force | Out-Null
& $bridge project-select --id maintenance --name 'Bridge maintenance' `
  --workspace $maintenanceWorkspace --peer-id computer-b --policy full-access --batch
```

Example on Linux, with peer `computer-a`:

```sh
bridge="$HOME/plugins/codex-bridge/scripts/bridge.sh"
maintenance_workspace="$HOME/CodexBridgeProjects/Maintenance"
mkdir -p "$maintenance_workspace"
sh "$bridge" project-select --id maintenance --name 'Bridge maintenance' \
  --workspace "$maintenance_workspace" --peer-id computer-a --policy full-access --batch
```

Substitute the actual peer IDs and inspected installation/configuration paths. With a non-default configuration, put `--config /absolute/config.json` before `project-select`. `project-add --policy full-access` also supports explicit local configuration; use its `--help` for required project fields. Do not pass a peer-supplied token or copy the other computer's configuration.

Both owners select the same collaboration project ID, each with their own workspace and policy. Full access on one endpoint does not enable it on the other. A host that only needs to review results may retain a restricted policy for its own worker.

Create one collaboration session for this newly authorized maintenance scope, with a concrete goal and responsibility split. Reuse that session for later maintenance. Do not convert a pending read-only test request into a privileged retry: preserve its original request identity and create an explicitly authorized task in the maintenance session.

## Work directly through Bridge

From the ordinary local Codex task, request a specific maintenance operation in that session. For example:

> Use the maintenance session to inspect the other computer's Bridge installation and startup settings. Return the actual versions and whether daemon startup is waiting-only. Preserve running work and unrelated routes.

The remote worker can run commands under its configured full-access policy and return progress, selected files, and completed results through `task_status`/`task_wait`. Routine authorized work no longer requires manually relaying commands to an owner chat. Destructive changes, new scope, or OS elevation still require whatever authorization and permissions apply locally; full access is not a password or approval bypass.

SSH remains forwarding-only. Commands execute through the receiving computer's locally authenticated Codex App Server, not through an SSH shell. If that Bridge daemon is stopped, it cannot receive a repair task: use the established owner-local startup path. Coordinate daemon upgrades/restarts and preserve a recovery route before a worker interrupts its own service.

## Keep conversation ownership clear

The ordinary owner task is where the person configures scope and dispatches Bridge work. The dedicated Bridge conversation retains the maintenance worker's context. Both refer to the same authorized project but are not interchangeable execution environments.

When `conversation_in_use` is reported, let the current owner finish/release that conversation. Inspect the original request: if its saved result is a terminal refusal and confirms no turn started, submit an explicitly requested new task with a new request ID in the same session. Reusing the old ID returns its saved refusal. For a timeout or uncertain result, keep the original ID and inspect status before deciding on new work. Do not kill another app, take over its writer, clone the conversation, or create repeated sessions to evade the lock. A dedicated maintenance session is created because the owner authorized a new scope, not to work around a busy read-only conversation.

## Verify or reduce permissions

After local activation, verify the configured policy and the receiving runtime's compatibility. Then run a small authorized test that distinguishes full access from the restricted policies: create/read/remove a temporary file outside the project folder that this owner is permitted to use, and request a non-sensitive owner-approved network endpoint. Avoid credentials, private home files, and destructive commands. Inspect native command evidence and actual results; a policy string, schema check, or dialogue reply alone does not establish working access.

To reduce access, finish or cancel active tasks first, then select `read-only` or `workspace-write` locally for that project. Follow the session scope rules when changing policy; do not expect a config edit to undo an already executed command. Revoke the peer separately if the collaboration itself should end. Preserve current conversation history, task IDs, and saved results during the change.

Full-access deployment tests are separate from earlier restricted/loopback tests. Check the [verification record](TESTING.md) for completed results and remaining limitations.
