# Set up two computers

This guide calls the computer that starts SSH **A**, and the other computer **B**. Work flows in either direction. Each person runs the commands on their own computer.

The first release expects an authorized SSH connection with public-key authentication, a verified host key, and local/reverse forwarding permitted. It does not install SSH, change router/firewall rules, or create public listeners. A forwarding-only relay is not a shell on the destination computer.

## 1. Install on both computers

Clone this repository into a source folder such as `~/Source/codex-bridge`, then open PowerShell there. Do not clone into the installation destination `~/plugins/codex-bridge`.

Set these paths to the actual local executables. Codex Desktop may keep its executable below `%LOCALAPPDATA%\OpenAI\Codex\bin`; `Get-Command codex` helps if it is on PATH.

```powershell
$python = 'C:\Path\To\python.exe'
$codex = 'C:\Path\To\codex.exe'
& $python --version
& $codex --version
```

Python must be 3.11+. The adapter accepts only Codex **0.153.4** and **0.155.0-alpha.2.6**. If another version is installed, stop here; don't disable the compatibility check. Each person must already be able to run Codex using their own local sign-in.

On **both**:

```powershell
& .\scripts\install.ps1 -PythonExe $python
$plugin = Join-Path $env:USERPROFILE 'plugins\codex-bridge'
$bridge = Join-Path $plugin 'scripts\bridge.ps1'
$config = Join-Path $env:USERPROFILE '.codex-bridge\config.json'
```

The installer copies only allowed connector files, preserves configuration, and does not register a marketplace or start a service.

## 2. Generate each computer's private configuration

On **A**:

```powershell
& $bridge init --peer-id computer-a --codex $codex --port 47321
```

On **B**:

```powershell
& $bridge init --peer-id computer-b --codex $codex --port 47321
```

Run `init` only for a new installation. It refuses to overwrite existing configuration. It creates this PC's credentials and paths; never replace that file with the other computer's configuration or with an example.

On **both**, register the connector using the supported [Codex MCP command](https://learn.chatgpt.com/docs/extend/mcp?surface=cli):

```powershell
& $codex mcp add codex_bridge -- $python -u (Join-Path $plugin 'codex_bridge\mcp.py') --config $config
```

This is the simplest route and does not require an internal plugin-scaffolding helper. If a `codex_bridge` MCP entry already exists, inspect it before replacing it. Use this registration **or** the [optional plugin route](ADVANCED.md#optional-local-plugin), not both. Start a fresh local Codex chat after registration.

## 3. Pair once

On **A**:

```powershell
$exchange = Join-Path $env:USERPROFILE '.codex-bridge\exchange'
& $bridge pair-export --peer-id computer-b --file (Join-Path $exchange 'from-a.json') --url http://127.0.0.1:47322
```

On **B**:

```powershell
$exchange = Join-Path $env:USERPROFILE '.codex-bridge\exchange'
& $bridge pair-export --peer-id computer-a --file (Join-Path $exchange 'from-b.json') --url http://127.0.0.1:47322
```

Privately copy `from-a.json` into B's exchange folder and `from-b.json` into A's exchange folder using the verified SSH/SFTP connection. These files contain **bridge credentials**. Never put them in Git, a public download, or a chat.

On **A**:

```powershell
& $bridge pair-import --file (Join-Path $exchange 'from-b.json') --url http://127.0.0.1:47322
```

On **B**:

```powershell
& $bridge pair-import --file (Join-Path $exchange 'from-a.json') --url http://127.0.0.1:47322
```

After successful import, delete both invitation files from each exchange folder. Keep the generated private configuration.

## 4. Connect the bridges

Only **A** adds an `ssh_transport` block to its generated private configuration. Use the block in [computer-a.example.json](../examples/computer-a.example.json), replacing host, port, account, and file paths with the verified SSH connection's values. Set `enabled` to `true`.

Preserve generated tokens and peer records. The SSH private key remains on A; B authorizes its public key. `host_key_alias` must match the verified entry in the configured known-hosts file. Ordinary SSH uses port 22; an existing tunnel may use another configured port.

| Local endpoint | Forwarded destination |
| --- | --- |
| A `127.0.0.1:47322` | B `127.0.0.1:47321` |
| B `127.0.0.1:47322` | A `127.0.0.1:47321` |

These ports must be unused. Update peer URLs and transport fields together if you change them. Bridge binds only to loopback.

On **both**, while signed into the Windows desktop:

```powershell
& $bridge start --interactive
```

On **A**:

```powershell
& $bridge transport-start
& $bridge status
```

The peer should report `available: true`. Run `& $bridge status` on B too to verify the reverse direction. `& $bridge diagnostics` reports runtime/version details.

Interactive mode is remembered, so future starts use `start` alone. It uses a hidden on-demand scheduled task with no password or login trigger. Both Windows owners must remain signed in.

## 5. Add a project on each PC

Start with an empty test workspace. On **both**:

```powershell
$workspace = Join-Path $env:USERPROFILE 'CodexBridgeProjects\sample-project'
New-Item -ItemType Directory -Path $workspace -Force | Out-Null
Set-Content -LiteralPath (Join-Path $workspace 'README.md') -Value 'A harmless Codex Bridge test project.'
```

Use the same project ID with each person's own workspace. On **A**:

```powershell
& $bridge project-add --id sample-project --name 'Sample project' --workspace $workspace --peer-id computer-b --export-root (Join-Path $workspace 'bridge-export') --import-root (Join-Path $workspace 'bridge-import') --policy read-only
```

On **B**, run that command with `--peer-id computer-a`.

Use `workspace-write` when the local owner authorizes task edits. Do not select an entire user profile or a credentials folder. Both policies rely on the Codex sandbox; they are not a hard barrier against reading other files visible to that Windows account. See [security boundaries](SECURITY.md).

## 6. Prove it works

In a fresh local Codex chat on A:

> Use Codex Bridge. Check computer-b, create a session for sample-project named “Sample project collaboration,” and ask the peer to read its README.md and report its first line. Save the session ID and wait for the result.

Then ask B's Codex:

> Use Codex Bridge. Find that existing sample-project session, inspect its context, and send computer-a a task to read its own README.md. Wait for the reply. Reuse the session.

Ask a follow-up that recalls the previous result. Put a small test file in `bridge-export`, ask Codex to send it with `artifact_send`, and compare the returned SHA-256. A task ID proves dispatch; the actual result and file hash prove success.

## Find your project chat

Ask Codex to call `session_list` and `session_get`. The session's `conversation_ids` maps each computer's ID to **that computer's local chat**. An ID appears after that computer receives its first task.

On the computer that owns a conversation, the tested desktop app opens it with:

```text
codex://threads/<that-computer-local-conversation-id>
```

Paste the link into Windows **Win + R**. A peer's local ID is not a chat on your computer. Automatic sidebar refresh and unsolicited notifications are not guaranteed.

Reuse one project session for follow-ups. Rename its chat in the desktop app to the project name if its generated title is unclear.

A Bridge turn owns its local conversation while running. After completion this release closes that worker and preserves history. If desktop Codex owns the writer when Bridge tries to resume, the request fails with `conversation_in_use` before starting a turn. Release it in the owning app before submitting a new attempt.

## Add another project

Repeat **step 5** with another project ID and workspace on each PC, then create one new session. Pairing and installation stay the same.

`project-add` replaces that project's configuration. Preserve intended permissions when updating it; advanced `local_actions` must be reapplied deliberately. Create a new session after changing execution policy or action capabilities.

## Common problems

| Symptom | Check |
| --- | --- |
| Peer unavailable | Both bridges, SSH route, A's supervisor, forwarding permissions, matching ports |
| Unsupported Codex version | Check the selected executable's version; preserve the guard |
| “Open in another app” | Let the current owner finish and release the chat; don't kill unrelated processes |
| Native commands fail when started through SSH | Use `start --interactive` while the owner is logged in |
| No writable root capability SIDs | Choose a workspace outside a profile folder protected because it contains SSH credentials |
| `uncertain` after a crash | Inspect saved history and file effects; never blindly replay with a new ID |
| File too large | Stay below 8 MiB per file; automatic chunking is not implemented |

After reboot, sign in, start Bridge on both PCs, and start the SSH supervisor on A. Existing SSH/relay prerequisites must already work.
