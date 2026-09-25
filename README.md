# Codex Bridge

Let two people's Codex installations exchange project tasks, replies, and selected files over SSH. Each person uses **their own Codex account**; conversations, execution, and authentication stay on their computer. This is an independent community project.

## Waiting is separate from connecting

| Component | What starts automatically, when enabled |
| --- | --- |
| Private network and SSH receiving service | Wait for authorized connections using the owner's OS settings |
| Bridge daemon | Starts at owner sign-in and waits; no SSH dialing |
| SSH connection | Starts for the selected peer when you ask to collaborate |
| Persistent connection | Separate per-peer opt-in, with finite retries |

**A waiting Bridge does not continually dial an offline friend.** Both computers need receiving authorization before either can initiate. One verified tunnel carries work in both directions.

## Install on both computers

**Windows upgrade hold:** a live rc.3 upgrade hit `local_endpoint_unverified` before daemon launch. Keep an existing working Windows installation until the follow-up fix is verified. The Linux service checks passed; see [current verification details](docs/TESTING.md).

Use local Python 3.11+, a complete Codex installation signed into your own account, and OpenSSH. Download and extract the matching installer from the [0.3.2 release candidate](https://github.com/AhmadotEng/codex-bridge/releases/tag/v0.3.2-rc.3). Compare its SHA-256 with that release's `SHA256SUMS`.

| Download | Run inside the extracted `codex-bridge` folder |
| --- | --- |
| [Windows ZIP](https://github.com/AhmadotEng/codex-bridge/releases/download/v0.3.2-rc.3/codex-bridge-0.3.2-rc.3-windows.zip) | PowerShell: `.\scripts\setup.ps1` |
| [Linux tar.gz](https://github.com/AhmadotEng/codex-bridge/releases/download/v0.3.2-rc.3/codex-bridge-0.3.2-rc.3-linux.tar.gz) | Terminal: `sh scripts/setup.sh` |

Both contain the same Bridge source and guides. Setup creates this computer's private configuration and registers its MCP tools. It does not copy accounts, install SSH services, or alter unrelated routes. The default location is `~/plugins/codex-bridge`; existing settings are preserved and conflicts are reported.

**Experimental prerelease:** Windows loopback connections have been tested. Live Windows/Linux migration and actual sign-in/reboot acceptance remain pending; see the [test evidence](docs/TESTING.md).

Follow the **[two-computer setup guide](docs/SETUP.md)** to exchange invitations, authorize SSH in both directions, and choose project folders. Select the same project ID on both computers. Waiting startup is optional; persistent connections are off by default.

## Collaborate on demand

In your ordinary local Codex task, ask:

> Use Codex Bridge with computer-b and sample-project. Establish or reuse its connection, find or create our project session, ask it to summarize its README, and wait for the result. Keep follow-ups in that session.

Codex checks local readiness, connects the selected peer, and verifies its Bridge before sending work. A local connection request can start your own daemon. If the other daemon is stopped, its owner must start it locally; restricted SSH does not provide a remote shell.

A session links **two local conversations**, rather than mirroring all account chats. Use `session_chat` to find this computer's project task. Another project needs new workspace selection and a session, not another installation.

Projects default to **read-only**. An owner can choose `workspace-write` for project edits or explicitly enable **`full-access`** for a dedicated maintenance project. Full access lets its Bridge worker use that owner's filesystem, commands, and network to complete authorized setup/maintenance tasks and return results directly. It does not grant root/administrator elevation or copy account credentials. See [project permissions and owner setup](docs/PERMISSIONS.md).

## Local controls when tools are unavailable

Set your installed launcher:

```powershell
# Windows
$bridge = Join-Path $env:USERPROFILE 'plugins\codex-bridge\scripts\bridge.ps1'
& $bridge start --interactive
& $bridge autostart-enable --component daemon
& $bridge connection-status --peer computer-b
& $bridge connect --peer computer-b --request-id ([guid]::NewGuid().ToString())
```

```sh
# Linux
bridge="$HOME/plugins/codex-bridge/scripts/bridge.sh"
sh "$bridge" start --background
sh "$bridge" autostart-enable --component daemon
sh "$bridge" connection-status --peer computer-a
sh "$bridge" connect --peer computer-a --request-id "$(python3 -c 'import uuid; print(uuid.uuid4())')"
```

Retain the request ID when repeating an interrupted operation. Use `connect --retry` with a new demand ID to explicitly retry an exhausted connection. `disconnect --peer ID --request-id UUID` deliberately pauses that peer until a local owner resumes. `autostart-disable --component daemon` changes future startup; `stop` stops the daemon now.

Status distinguishes local readiness, network/SSH, authentication, forwarding, remote Bridge authorization, and remote Codex readiness:

> Not connected; remote Bridge not checked.

> SSH connected; remote Bridge unavailable at the configured port.

A Tailscale listing, SSH process, or listening port is not evidence of completed collaboration. Initial attempts are bounded (default two launches within 45 seconds); active-work recovery has a separate budget. See [connection behavior](docs/CONNECTIONS.md).

## Details

- [Setup, projects, and first task](docs/SETUP.md)
- [Project permissions and full-access maintenance](docs/PERMISSIONS.md)
- [SSH receiving access on Windows and Linux](docs/RECEIVING-SSH.md)
- [Tailscale and reciprocal network permission](docs/TAILSCALE.md)
- [Connection selection, retries, idle lifetime, and migration](docs/CONNECTIONS.md)
- [Linux startup and optional one-file launcher](docs/LINUX.md)
- [Upgrades, rollback, revocation, and uninstall](docs/ADVANCED.md)
- [Runtime readiness](docs/RUNTIME.md), [security boundaries](docs/SECURITY.md), and [actual test evidence](docs/TESTING.md)

This prerelease's configuration checks, simulated tests, real connection tests, and reboot observations are documented separately. Do not assume a registered startup service proves reboot recovery.

Licensed under [MIT](LICENSE).
