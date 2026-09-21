# Connect with Tailscale

This is the recommended Windows route: **ordinary Windows OpenSSH over Tailscale**, using each person's own Codex account and locally held SSH identity. Bridge still keeps its APIs on loopback and sends requests in both directions through one SSH connection.

Computer **A** starts SSH. Computer **B** accepts it. These labels describe their network roles; either person can send Codex work.

## 1. Connect the computers privately

Install and sign into Tailscale on both PCs using the [Windows installation guide](https://tailscale.com/docs/install/windows). The computers can belong to the same tailnet, or B's owner can share B with A's Tailscale user and A can accept the invitation.

For a shared device, use its Tailscale IP or full DNS name, such as `computer-b.example-tailnet.ts.net`; replace this placeholder with the actual device name. Shared devices require the full name across tailnets. Access policies still apply. [Tailscale device sharing](https://tailscale.com/docs/features/sharing)

This Bridge topology needs A to initiate the connection to B. B's replies and B-to-A Bridge requests travel inside its established reverse forward, so B does not need a separate SSH connection to A. This follows the documented sharing rule that a shared device can respond to incoming connections.

## 2. Prepare ordinary OpenSSH

On B, confirm Windows OpenSSH Server is installed and its `sshd` service runs. On A, confirm the OpenSSH client is installed. If needed, each owner follows Microsoft's [OpenSSH installation guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).

Reuse an existing verified key-based connection where available. For a new SSH identity, A creates and retains its private key locally; B authorizes only the **public** key for the selected Windows account. Follow Microsoft's [key-based authentication guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement), including its different authorized-key locations for standard and administrator accounts.

Prepare these values before running Bridge setup:

| Value on A | What to select |
| --- | --- |
| SSH host | B's Tailscale IP or full device DNS name |
| SSH port | B's OpenSSH port, normally `22` |
| SSH account | The existing authorized Windows account on B |
| Identity file | A's existing local SSH identity; never copy the private key to B |
| Known-hosts file | A's file containing B's independently verified host key |
| Host-key alias | The matching name already stored in that known-hosts file |

Verify the server's host-key fingerprint with B's owner through a trusted channel before trusting it. If switching from another route to the same computer, keep the already verified host key and alias; a new IP does not justify bypassing host verification.

Bridge uses SSH batch mode. The chosen key must work without a password or passphrase prompt in A's Windows login, using the owner's local SSH agent where needed. Bridge does not store SSH passwords or unlock encrypted keys.

B's SSH configuration must permit both local and reverse loopback forwarding. The Bridge SSH command uses `-N -T` and does not request a shell. Tailnet policies and Windows firewall rules must allow A to reach B's SSH port. Review overlapping rules before claiming access is restricted; adding a narrow allow rule does not remove a broader existing allowance.

This Windows guide does not use the separately named **Tailscale SSH** server. Its server component supports Linux and the open-source macOS daemon. Do not enable `tailscale set --ssh` for this setup. [Tailscale SSH platforms](https://tailscale.com/docs/features/tailscale-ssh)

## 3. Keep the network available after restart

On **both Windows PCs**, open Tailscale's tray menu, choose **Preferences → Run unattended**, and enable it. This keeps Tailscale connected independently of the interactive user. [Tailscale unattended mode](https://tailscale.com/docs/how-to/run-unattended)

If the installed `tailscale set --help` lists `--unattended`, the equivalent targeted command is `tailscale set --unattended=true`. `set` changes only specified preferences; do not reset unrelated exit-node, routing, or DNS choices. Otherwise use the tray setting. [Tailscale CLI preferences](https://tailscale.com/docs/reference/tailscale-cli#set)

On B, its owner should configure the existing `sshd` service for automatic startup. In an elevated PowerShell window:

```powershell
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Get-Service sshd
```

These are separate network-service settings, documented by [Microsoft](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse). Bridge setup does not install or modify them.

Tailscale and SSH can be available before Windows login. **Codex Bridge execution still requires each owner to sign in.** Its optional login startup is configured separately below.

## 4. Pair and configure Bridge

Run `.\scripts\setup.ps1` from the downloaded Bridge source on both PCs. Follow the [setup guide](SETUP.md) to generate distinct computer identities, exchange private Bridge invitations, and choose a project workspace on each PC.

On A, select the prepared Tailscale-backed SSH connection when the assistant asks whether this computer starts SSH. To configure it later:

```powershell
$bridge = Join-Path $env:USERPROFILE 'plugins\codex-bridge\scripts\bridge.ps1'
& $bridge transport-config --peer-id computer-b
```

Default forwarding is:

| Local listener | Destination |
| --- | --- |
| A `127.0.0.1:47322` | B's Bridge at `127.0.0.1:47321` |
| B `127.0.0.1:47322` | A's Bridge at `127.0.0.1:47321` |

B does not need its own transport supervisor for this pairing. Both peer URLs remain loopback URLs. Do not put a Tailscale IP into a Bridge `peers.*.url` or expose Bridge ports on a public/LAN interface.

The [Tailscale configuration example](../config.tailscale.example.json) shows A's named `ssh_transports` block with placeholders and empty credentials. It is explanatory: let setup generate the real configuration, and never overwrite an existing pair with the example.

## 5. Enable owner-login startup and verify now

On **both** PCs:

```powershell
$bridge = Join-Path $env:USERPROFILE 'plugins\codex-bridge\scripts\bridge.ps1'
& $bridge autostart-enable
& $bridge start --interactive
```

On **A**:

```powershell
& $bridge transport-start --peer computer-b
```

On **both**:

```powershell
& $bridge autostart-status
& $bridge preflight --project-id sample-project
& $bridge status
```

`autostart-enable` registers future startup for the local daemon and currently enabled paired transports. It does not start them now. The named registrations run as the signed-in owner without storing a Windows password or requesting elevated Codex execution.

Readiness requires more than registration: the local daemon, correct peer identity, forwarding, Codex sign-in, and both project scopes must pass. Exchange a harmless task and retained-context follow-up in each direction, then transfer a selected file and compare SHA-256.

Schedule a restart/login check after active work finishes. Confirm recovery without manually starting Bridge and reuse the existing session IDs. A reboot/login test does not substitute for separately testing sign-out/sign-in, screen lock/unlock, or sleep/wake. Current evidence and remaining checks are in [TESTING.md](TESTING.md).

## Migrate an existing pair without losing its work

1. Finish or cancel active work and record uncertain requests locally. Keep the former route available for rollback.
2. Back up private configuration and the installed version locally. Preserve current state; do not export keys, tokens, or private backups.
3. Verify B's Tailscale endpoint with the existing SSH identity and pinned host key.
4. On A, run `transport-stop --peer computer-b` and confirm the old Bridge forwards have stopped.
5. Update only that route with `transport-config --peer-id computer-b --ssh-host YOUR_PEER_TAILSCALE_ADDRESS --ssh-port 22 --batch`. Existing fields are preserved. Keep pairing credentials, project scopes, loopback ports, and B's peer URL unchanged.
6. Run `transport-start --peer computer-b`, then preflight and harmless retained-session checks in both directions.
7. Enable the chosen startup components explicitly and inspect `autostart-status`.

Named `ssh_transports` are preferred. A legacy single `ssh_transport` remains supported only when its peer is unambiguous; stop its old supervisor before migrating. Legacy startup registrations must be rebound explicitly to the intended peer. Do not replace the current source tree wholesale with an older deployment.

Leave unrelated port shares, game routes, exit nodes, and subnet routing alone. This changes only the Bridge's selected SSH route.

## Everyday recovery and maintenance

- `stop` and `transport-stop --peer ID` deliberately stop that component for the current Windows login. Explicit start resumes it; the next owner login can start it again if enabled.
- `autostart-disable` disables future login startup and preserves running work. To stop now and after later logins, disable startup and stop the selected components.
- `autostart-enable --component transport --peer ID` opts a newly paired computer into startup. Adding a project needs only `project-select` and a new session, not another transport.
- `tailscale ping YOUR_PEER_ADDRESS` and `tailscale status` distinguish direct, DERP, and peer-relay routes. Initial probes can relay before a direct path is negotiated. Measure latency rather than assuming Tailscale is always faster. [Connection types](https://tailscale.com/docs/reference/connection-types)
- Keep the owner's chosen node-key expiry policy. Expiry requires local reauthentication even when startup works; do not force reauthentication over the only recovery route. Disabling expiry is a separate owner choice. [Key expiry](https://tailscale.com/docs/features/access-control/key-expiry)

Keep detailed Tailscale and SSH output private: it can include account names, addresses, and local paths. Bridge's safe status is intended for everyday checks.

## Roll back or revoke

To roll back, stop only the affected Bridge transport, restore its former endpoint, restore that route's prerequisites, and start once. Keep current task/session/artifact state and any work completed since migration; restoring an old database can lose results and deduplication history.

To end access, revoke the Bridge pairing and separately remove any dedicated SSH/Tailscale authorization that should end. Disable startup, stop its components, and remove registrations before uninstalling. Follow [advanced removal instructions](ADVANCED.md#stop-and-uninstall). Unrelated network services, keys, projects, and conversations remain the owner's responsibility.
