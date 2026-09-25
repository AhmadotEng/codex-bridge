# Set up two computers

Call the computers **A** and **B**. Either may initiate; one selected SSH tunnel carries work in both directions. Each person runs setup using their own Codex account. Both need [SSH receiving authorization](RECEIVING-SSH.md) before either-side initiation can work.

## Before setup

You need Python 3.11+, a working local Codex sign-in, and authorized SSH reachability in both directions. Follow [ordinary OpenSSH over Tailscale](TAILSCALE.md), or use your existing private network. Network presence alone does not verify SSH or Bridge.

Windows automatic startup also needs `pythonw.exe` beside the selected Python executable. Standard Windows Python installations include it; a custom runtime without it can still use manual startup.

Bridge's SSH transport uses that connection without requesting a remote shell. The installer does not install SSH, change firewall/router rules, copy private keys, or reconfigure other application tunnels. An explicitly authorized full-access maintenance worker can later perform owner-permitted local administration commands through Codex. That does not make the SSH relay a shell.

Download or clone this repository into a source folder. Keep it separate from the installation folder `~/plugins/codex-bridge`.

## 1. Run the assistant on both computers

On Windows, open PowerShell in the source folder:

```powershell
.\scripts\setup.ps1
```

If Python is not on PATH, select it explicitly:

```powershell
.\scripts\setup.ps1 -PythonExe 'C:\Path\To\python.exe'
```

The assistant installs only allowed connector files, checks the local Codex schema and known Windows runtime layout, generates this computer's private configuration, and registers its MCP tools. Select the executable inside a complete local Codex distribution, with its matching companion files. It preserves existing computer identity, pairing, projects, and unrelated Codex settings. A conflicting `codex_bridge` MCP registration is reported for inspection rather than overwritten.

Setup offers optional **daemon-only** startup for the next owner login, with **no** as the default. Saying yes registers a waiting daemon, not an SSH retry loop. Persistent dialing requires a separate explicit per-peer option. Review and disable unwanted legacy transport startup during an upgrade; setup does not silently change existing registrations.

Choose short, distinct computer names such as `computer-a` and `computer-b`. These are Bridge identifiers, not your Windows login names.

On macOS/Linux:

```sh
sh scripts/setup.sh
```

The default install location is `~/plugins/codex-bridge`; private configuration and state are under `~/.codex-bridge`. Portable setup accepts explicit locations and runtime selections: `sh scripts/setup.sh --help`. For the single `.pyz` package, Linux prerequisites, manual lifecycle, and optional owner-session startup, see [LINUX.md](LINUX.md). Platform test coverage is recorded in [TESTING.md](TESTING.md).

The same software goes on both systems. Each generates its own configuration, credentials, launcher paths, state, and local conversation IDs. Do not overwrite a generated configuration with an example or with the other computer's file.

## 2. Exchange private invitations

The assistant guides `pair-setup`. Each computer exports one invitation for the other, and imports the other computer's invitation.

Transfer invitations through an owner-selected private file exchange (for example, Tailscale file sharing, an encrypted messenger attachment, or encrypted removable storage). They contain **Bridge credentials**: do not paste their contents into public chats or repositories. A forwarding-only SSH key cannot use SFTP/SCP; do not broaden it to transfer the invitation. Only an already separately authorized file-transfer channel may use SFTP. Account tokens and SSH private keys are never part of the exchange.

If the other invitation is not ready, finish later. On each Windows computer:

```powershell
$bridge = Join-Path $env:USERPROFILE 'plugins\codex-bridge\scripts\bridge.ps1'
& $bridge pair-setup
```

For explicit import on A:

```powershell
& $bridge pair-setup --peer-id computer-b --import-file 'C:\PrivateExchange\from-b.json' --consume-import --batch
```

Use `computer-a` and A's invitation on B. `--consume-import` deletes only the selected imported invitation after successful import. Delete remaining exported exchange copies after the other computer confirms import.

Pairing is complete only when **both** computers import the other's invitation. Re-running guided setup preserves existing pairing credentials.

## 3. Configure approved candidate routes on both computers

On each computer, supply the other's private address, SSH port/account, this computer's own private identity file, verified known-hosts file, and host-key alias. Each retains its own private key. The other owner authorizes only its public key.

You can also run:

```powershell
& $bridge transport-config --peer-id computer-b
```

No JSON editing is needed. The [Tailscale example](../config.tailscale.example.json) is explanatory, with empty credentials and disabled routes. The original single `ssh_transport` configuration remains supported when its peer is unambiguous; new setup uses named `ssh_transports`.

Default forwarding:

| Listener | Destination |
| --- | --- |
| A `127.0.0.1:47322` | B `127.0.0.1:47321` |
| B `127.0.0.1:47322` | A `127.0.0.1:47321` |

This table describes a legacy one-origin route. It remains usable in explicit legacy mode while upgrading; it does not support the new selection protocol. For either-side initiation, configure the managed candidate map on both computers: two distinct reserved listeners per endpoint, one route for each possible initiator. The exact map and corresponding SSH restrictions must agree. Follow [connection configuration](CONNECTIONS.md). Do not assign an unrelated application's occupied port or run a legacy supervisor over the same ports as the new manager.

## 4. Select a project on both computers

Create an empty folder for a harmless first test and put a short README in it. Then run:

```powershell
& $bridge project-select
```

Use the same project ID, for example `sample-project`. Each person selects their own folder. The command creates dedicated `bridge-export` and `bridge-import` folders inside it and preserves unrelated projects and existing fixed local actions.

The default policy is `read-only`. To explicitly authorize project edits:

```powershell
& $bridge project-select --id sample-project --policy workspace-write
```

Project policies rely on Codex's sandbox; selecting a folder is not a hard barrier against reading everything visible to that operating-system account. See [security boundaries](SECURITY.md). A changed workspace, policy, or capability scope calls for a new collaboration session.

For direct setup/maintenance work beyond a project folder, the local owner can choose `--policy full-access` on a **new dedicated maintenance project**. The worker then has the owner's filesystem, command, and network access, subject to operating-system permissions. This is an explicit local configuration choice; a peer task or RPC cannot override the configured policy. Existing read-only/workspace-write projects remain as configured. Follow the short [Windows/Linux activation examples](PERMISSIONS.md#enable-a-dedicated-maintenance-project).

## 5. Enable startup, start now, and verify

On both Windows computers, while signed into the desktop:

```powershell
& $bridge autostart-enable --component daemon
& $bridge start --interactive
```

`autostart-enable --component daemon` opts into future waiting startup only. It does not start the daemon now or enable transport dialing. The default and retained `auto` alias have the same daemon-only meaning. A persistent connection is a separate opt-in: `autostart-enable --component transport --peer ID`; its attempts remain bounded and cannot reset an exhausted or deliberately stopped connection.

The hidden startup tasks run under this Windows owner's interactive login, without storing a password or elevating Codex. Each owner must sign in after reboot and remain signed in for execution. Tailscale unattended mode and automatic OpenSSH startup provide the network layer separately; they do not start Codex before login.

Interactive mode is remembered for later `start` calls. To use manual startup, skip `autostart-enable` and use the same start commands whenever needed.

On Linux, set `bridge="$HOME/plugins/codex-bridge/scripts/bridge.sh"`, then run `sh "$bridge" start --background` and optionally `sh "$bridge" autostart-enable --component daemon`. This registers a systemd user service on supported distributions, without root or lingering. Use manual startup when no user service manager is available. For subsequent examples, substitute `sh "$bridge"` for `& $bridge` and the other computer's peer ID. macOS requires manual lifecycle; Linux desktop and startup coverage is recorded in [TESTING.md](TESTING.md).

On whichever computer requests collaboration:

```powershell
& $bridge connect --peer computer-b --request-id ([guid]::NewGuid().ToString())
& $bridge connection-status --peer computer-b
```

On both:

```powershell
& $bridge preflight --project-id sample-project
& $bridge status
& $bridge autostart-status
```

Read the individual checks. Preflight inspects schema, known bundle files, sign-in, and connection/scope configuration; it sends no model turn or project command. Even `ok` reports native execution as `not_checked`. A registered task, running process, or listening SSH port does not prove a completed Codex task. Run preflight on **both** computers to check the reverse direction. Schedule restart/login testing after active work is finished and inspect readiness without manually starting Bridge.

## 6. Prove collaboration

Open a fresh Codex chat on A after registration:

> Use Codex Bridge. Create a session for sample-project with computer-b, named “Sample project collaboration.” Ask it to read its README and report the first line. Save the session ID and wait for the completed result.

In a local Codex chat on B:

> Use Codex Bridge. Find that existing sample-project session and send computer-a a task to read its own README. Wait for its result. Reuse the same session.

Then verify:

1. A follow-up remembers the prior request in the correct local conversation.
2. An authorized native command reads/hashes the selected test file in each direction. Inspect the completed task's structured `execution_evidence` and the file hash; a dialogue reply alone is insufficient. See the [bounded native-tool test](RUNTIME.md#prove-native-execution-in-the-existing-session).
3. A selected file in `bridge-export` arrives with the same SHA-256.
4. A second project has its own session, folder, and conversation.
5. Reconnecting and retrying with the same request ID does not repeat completed work.
6. Cancelling active work and revoking a disposable pairing behave as expected.

Files above 8 MiB use asynchronous chunked transfers. Poll `artifact_transfer_status` until completion and check the final artifact hash. Retry an interrupted transfer with the original operation and request ID; use `artifact_transfer_cancel` to abandon unfinished data. Old peers support only the direct 8 MiB path.

## Find and open your project chat

```powershell
& $bridge show-chat --session-id YOUR_SESSION_ID
& $bridge show-chat --session-id YOUR_SESSION_ID --open
```

Or ask Codex to call `session_chat`. It returns **this computer's** local chat link. No local chat exists until this computer receives its first task.

New Bridge chats receive the project title. After a task, Bridge closes its worker process and retains the conversation ID and history. The safe status view reports the last observed ownership:

| State | Meaning |
| --- | --- |
| `not_started` | This computer has not received a task |
| `queued` / `working` | Bridge is waiting or using the conversation |
| `released_to_desktop` | The Bridge worker has closed |
| `waiting_for_desktop_release` | Another app owns the conversation; Bridge did not take over |
| `failed` / `release_unconfirmed` | Inspect the request before retrying or opening |

Ownership is an observation, not a live lock query against the desktop. Automatic sidebar refresh and desktop notifications are not guaranteed. A remote conversation ID is not a local chat.

Use an ordinary local Codex task to choose project permissions and dispatch work through Bridge MCP. The Bridge-managed project conversation is the worker's retained context. If that same conversation is open elsewhere and returns `conversation_in_use`, release it there and inspect the saved result. A terminal refusal with no turn started needs a new explicit task ID in the same session; the original ID returns its saved refusal. Uncertain requests retain their IDs until their effects are established. Do not take over a conversation or create duplicate sessions. A newly authorized maintenance project is a separate scope, not a renamed or cloned copy of the read-only test conversation.

## Everyday use and another project

With waiting startup enabled, each owner signs in and Bridge is locally ready. No outgoing SSH attempt occurs until explicit collaboration/connect demand. Use `connection-status` for stage evidence, and `connect` to establish or reuse the selected peer's tunnel. `preflight` inspects runtime and scope separately.

`disconnect --peer ID --request-id UUID` writes durable peer-stop intent; incoming traffic and routine startup cannot clear it. Explicit local connect/retry resumes that peer. `stop` stops the daemon itself. `autostart-disable` changes future startup while preserving running work. Legacy `transport-stop` applies only to a deliberately retained old route; do not mix competing route managers.

For another project, repeat `project-select` on both, then create one new session. Installation, pairing, and startup stay the same. Reuse a project's session for follow-up work.

`status` is safe by default. `diagnostics` and `session_get` are more detailed and may include project information.

## Add another computer

Use a new peer ID with `pair-setup`, then configure both receiving authorizations and distinct approved candidate ports. Select its project permissions deliberately. Other peers' working routes are not changed.

```powershell
& $bridge connect --peer computer-c --request-id ([guid]::NewGuid().ToString())
& $bridge connection-status --peer computer-c
& $bridge disconnect --peer computer-c --request-id ([guid]::NewGuid().ToString())
```

A new pairing does not opt into persistent dialing. Connection commands select one peer explicitly. Revocation denies that peer's new operations while preserving unrelated work.

## Troubleshooting

| Check or symptom | Next action |
| --- | --- |
| Runtime schema incompatible | Select a compatible local Codex executable; do not bypass the check |
| Runtime bundle incomplete | Select a complete local bundle with matching companions; follow [runtime repair](RUNTIME.md) |
| Bundle layout unknown or reply completed without native execution | Inspect the local distribution and run the authorized native-tool test in [RUNTIME.md](RUNTIME.md); schema/dialogue success is insufficient |
| Local sign-in missing | Sign into Codex on that computer, then rerun preflight |
| Startup enabled but no Codex execution | Sign into the owning Windows account and inspect `autostart-status`; network services alone are insufficient |
| Local Bridge unavailable | Start its daemon and check for an occupied listen port |
| Bridge/peer deliberately stopped | Use local `start` for the daemon or explicit `connect --retry` for the peer; status does not resume either |
| Not connected; remote Bridge not checked. | No verified route exists; use explicit connect when collaboration is wanted |
| SSH connected; remote Bridge unavailable at the configured port. | Remote owner must check/start their local daemon and the configured port |
| SSH connected; remote Bridge authorization refused. | Owners must check pairing; do not keep relaunching SSH |
| SSH endpoint unavailable | Restore the existing SSH/tunnel connection |
| Tailscale works until node authentication expires | Reauthenticate locally; startup does not renew expired node credentials |
| SSH authentication or host-key failure | Inspect the existing key and verified host entry locally |
| Forwarding unavailable | Check both daemons, forwarding permission, and matching ports |
| Pairing mismatch | Verify both invitation imports and the expected computer IDs |
| Project scope missing | Select that project on both computers |
| Chat open in another app | Release it in that app; Bridge does not kill unrelated processes |
| `uncertain` task after a crash | Inspect conversation and file effects before a new request ID |
| Large transfer paused/failed | Restore the route and retry the same operation ID, or cancel it |

For updates, revocation, and uninstall, see [ADVANCED.md](ADVANCED.md).
