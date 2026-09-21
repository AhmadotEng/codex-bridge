# Set up two computers

Call the computer that starts SSH **A**, and the other **B**. Work can flow in either direction. Each person runs setup on their own computer using their own Codex account.

## Before setup

You need Python 3.11+, a working local Codex sign-in, and an existing SSH route from A to B. Public-key authentication, a verified host key, and both local/reverse forwarding must already work.

Bridge uses that connection without requesting a remote shell. It does not install SSH, change firewall/router rules, copy private keys, or reconfigure other application tunnels. A forwarding-only relay is not a shell on the destination computer.

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

The assistant installs only allowed connector files, checks the local Codex schema, generates this computer's private configuration, and registers its MCP tools. It preserves existing computer identity, pairing, projects, and unrelated Codex settings. A conflicting `codex_bridge` MCP registration is reported for inspection rather than overwritten.

Choose short, distinct computer names such as `computer-a` and `computer-b`. These are Bridge identifiers, not your Windows login names.

On macOS/Linux:

```sh
sh scripts/install.sh
"$HOME/plugins/codex-bridge/scripts/bridge.sh" setup --guided --register-mcp
```

The default install location is `~/plugins/codex-bridge`; private configuration and state are under `~/.codex-bridge`. The Python installer supports explicit locations: `python3 scripts/install.py --help`. Platform test coverage is recorded in [TESTING.md](TESTING.md).

The same software goes on both systems. Each generates its own configuration, credentials, launcher paths, state, and local conversation IDs. Do not overwrite a generated configuration with an example or with the other computer's file.

## 2. Exchange private invitations

The assistant guides `pair-setup`. Each computer exports one invitation for the other, and imports the other computer's invitation.

Transfer the invitation files through your verified private SSH/SFTP connection. They contain **Bridge credentials**: do not paste their contents into a chat or upload them to a repository. Account tokens and SSH private keys are never part of the exchange.

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

## 3. Select the existing SSH route on A

The assistant asks whether this computer starts SSH. On A, supply the existing connection's host, port, SSH account, local identity file, verified known-hosts file, and host-key alias.

You can also run:

```powershell
& $bridge transport-config --peer-id computer-b
```

No JSON editing is needed. The original single `ssh_transport` configuration remains supported; new setup uses named `ssh_transports`.

Default forwarding:

| Listener | Destination |
| --- | --- |
| A `127.0.0.1:47322` | B `127.0.0.1:47321` |
| B `127.0.0.1:47322` | A `127.0.0.1:47321` |

B runs its local Bridge and uses A's reverse forwarding. It does not need its own SSH supervisor for this pairing. Use different ports if these are occupied; the peer URLs and both forwarding destinations must agree.

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

## 5. Start and verify

On both Windows computers, while signed into the desktop:

```powershell
& $bridge start --interactive
```

Interactive mode is remembered. It uses a hidden on-demand scheduled task without a password, elevation, or startup trigger. The owner must remain signed in.

On macOS/Linux, use the installed `scripts/bridge.sh` with `start --background`. For all subsequent examples, substitute that launcher for `& $bridge`.

On A:

```powershell
& $bridge transport-start --peer computer-b
```

On both:

```powershell
& $bridge preflight --project-id sample-project
& $bridge status
```

Read the individual checks. A listening SSH port does not prove authentication, forwarding, or a completed Codex task. Run preflight on **both** computers to prove the reverse direction.

## 6. Prove collaboration

Open a fresh Codex chat on A after registration:

> Use Codex Bridge. Create a session for sample-project with computer-b, named “Sample project collaboration.” Ask it to read its README and report the first line. Save the session ID and wait for the completed result.

In a local Codex chat on B:

> Use Codex Bridge. Find that existing sample-project session and send computer-a a task to read its own README. Wait for its result. Reuse the same session.

Then verify:

1. A follow-up remembers the prior request in the correct local conversation.
2. A selected file in `bridge-export` arrives with the same SHA-256.
3. A second project has its own session, folder, and conversation.
4. Reconnecting and retrying with the same request ID does not repeat completed work.
5. Cancelling active work and revoking a disposable pairing behave as expected.

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

## Everyday use and another project

On both computers use `start`; on A also use `transport-start`. For another project, repeat `project-select` on both, then create one new session. Installation and pairing stay the same. Reuse a project's session for follow-up work.

`status` is safe by default. `diagnostics` and `session_get` are more detailed and may include project information.

## Add another computer

Use a new peer ID with `pair-setup`, then `transport-config` for that peer on the computer that owns its SSH connection. Assign a distinct local forwarding port and matching endpoint on the added computer. Select its project permissions deliberately.

```powershell
& $bridge transport-start --peer computer-c
& $bridge transport-status --peer computer-c
& $bridge transport-stop --peer computer-c
```

Without `--peer`, transport controls apply to configured transports. Stopping one peer leaves the others running. Revocation stops that peer's supervisor and denies new Bridge operations.

## Troubleshooting

| Check or symptom | Next action |
| --- | --- |
| Runtime schema incompatible | Select a compatible local Codex executable; do not bypass the check |
| Local sign-in missing | Sign into Codex on that computer, then rerun preflight |
| Local Bridge unavailable | Start its daemon and check for an occupied listen port |
| SSH endpoint unavailable | Restore the existing SSH/tunnel connection |
| SSH authentication or host-key failure | Inspect the existing key and verified host entry locally |
| Forwarding unavailable | Check both daemons, forwarding permission, and matching ports |
| Pairing mismatch | Verify both invitation imports and the expected computer IDs |
| Project scope missing | Select that project on both computers |
| Chat open in another app | Release it in that app; Bridge does not kill unrelated processes |
| `uncertain` task after a crash | Inspect conversation and file effects before a new request ID |
| Large transfer paused/failed | Restore the route and retry the same operation ID, or cancel it |

For updates, revocation, and uninstall, see [ADVANCED.md](ADVANCED.md).
