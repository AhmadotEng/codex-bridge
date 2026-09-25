# SSH receiving access on both computers

Either-side initiation needs an SSH client **and a receiving SSH service on each computer**, reciprocal network permission, distinct locally held keys, pinned host verification, and approved loopback forwarding. This is separate from Bridge installation and owner-login daemon startup.

Perform receiving setup locally as the owner, using elevation only for the OS service/firewall changes. Keep Codex/Bridge under the ordinary owner account. Preserve other SSH accounts, services, and gameplay ports. A shared Windows/Linux pairing does not require the same Codex account.

## Network and identity checklist

1. Confirm the private network permits A to initiate TCP to B's SSH port **and** B to initiate to A's. An accepted Tailscale device share in one direction does not establish the opposite direction. Same-tailnet policy or reciprocal accepted sharing can provide it. `ShareeNode` and device presence alone are not diagnostic proof. See [Tailscale sharing](https://tailscale.com/docs/features/sharing).
2. Generate a dedicated SSH key locally on each computer if a suitable one does not already exist. Never replace a working key automatically. Exchange only `.pub` contents; private identities stay on their originating computer.
3. Each owner authorizes the other public key for a dedicated forwarding account or a deliberately scoped existing account.
4. Obtain the receiver's public host-key fingerprint through a trusted owner exchange, compare it locally, and put the verified public host entry in the configured known-hosts file. `ssh-keyscan` alone is not verification. The client uses strict host checking.
5. Agree the daemon/candidate ports and test the exact allowed forwards in both directions.

## Windows receiver

Inventory `Get-Service sshd` and the installed OpenSSH version before changing anything. If no receiver exists, install the Windows OpenSSH Server optional component using Microsoft's [installation guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse), then configure only the intended receiving account and a firewall rule scoped to the private peer address/network. Start the service and make its startup automatic after local validation.

Windows normally reads server settings from `%ProgramData%\ssh\sshd_config`. Standard and administrator accounts have different authorized-key defaults and ACL requirements; follow the installed version's [server configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration) and [key management](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement) instructions. Do not copy a Linux ownership command to Windows or expose an unrestricted administrator shell just to forward Bridge.

Validate the candidate configuration with the selected `sshd.exe -t` before a coordinated service restart. A successful client `ssh.exe` on Windows does not mean its receiving service exists.

## Linux receiver

Install the distribution's OpenSSH server only if needed. Use that distribution's service name (commonly `sshd` or `ssh`); inspect the active configuration and firewall before a targeted change. Configure it as an OS receiving service independently of the owner's Bridge systemd user unit. Authorize the other public key with owner-only `.ssh` directory/key-file permissions. Validate `sshd -t` before a coordinated reload/restart; retain a local recovery terminal.

The Bridge daemon itself never becomes a root system service. Do not enable lingering or copy Codex credentials to a service account to make pairing work.

## Forwarding-only authorization

The client requests no remote command (`-N`) and only the approved local/reverse forwards. Restrict both destination (`PermitOpen`) and remote listener (`PermitListen`) to exact loopback ports, with `GatewayPorts no`. Disable session channels (for a dedicated account, `MaxSessions 0`) and agent/X11/PTY capabilities. Both forwarding directions must be allowed; `DisableForwarding yes` would disable the required operation. Validate directives against the installed SSH version. See [OpenSSH server forwarding controls](https://man.openbsd.org/sshd_config).

For a dedicated receiver, an owner-reviewed `Match User` section can restrict its scope without changing unrelated users. Per-key `restrict,port-forwarding,permitopen="127.0.0.1:DAEMON_PORT",permitlisten="127.0.0.1:CANDIDATE_PORT"` is a useful additional restriction where supported, but it does **not** by itself prohibit command execution: enforce session denial as well. Confirm a remote shell/SFTP request is denied while both selected forwards succeed.

Example for a **dedicated forwarding account** named `bridge_receiver`, with local Bridge port 47321 and the authorized incoming candidate listener 47422:

```text
Match User bridge_receiver
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    MaxSessions 0
    AllowTcpForwarding yes
    GatewayPorts no
    PermitOpen 127.0.0.1:47321
    PermitListen 127.0.0.1:47422
    AllowAgentForwarding no
    PermitTTY no
Match all
```

Substitute the actual receiving account and approved ports on **each** endpoint; do not apply the block to a shared shell account without reviewing the impact. On Linux also deny unsupported/unneeded forwarding features such as X11, stream-local sockets, and network tunnels using that server's supported settings. Windows does not implement every Unix directive; do not paste unsupported `X11Forwarding`, `AllowStreamLocalForwarding`, or `PermitTunnel` settings into its configuration. Validate the installed server with `sshd -t` and inspect the effective matching configuration before reload.

`MaxSessions 0` denies shell, command, and subsystem session channels while permitting forwarding. No `ForceCommand` shell script or deliberately invalid command is needed. A parse test is insufficient: prove that shell/SFTP requests are denied and that the exact required forwarding and authenticated Bridge exchange work after the change.

Forwarding-only access cannot start a stopped remote daemon or copy invitations with SCP/SFTP. Use owner-local daemon commands and a separate private invitation exchange. Do not silently relax the key when a setup step fails.

## Verify and revoke

Check network reachability, SSH authentication, exact forwards, and authenticated Bridge identity separately. Test native tasks in both directions only within a selected harmless project. Record actual service startup/reboot observations separately from configuration checks.

To revoke a peer, revoke Bridge pairing, disable its persistent startup, and remove only its authorized public key/network grant. Preserve unrelated keys and routes. Completed files/results and conversation history are not erased by revocation.
