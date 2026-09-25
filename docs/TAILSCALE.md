# Ordinary OpenSSH over Tailscale

Tailscale supplies the private network. Bridge uses ordinary OpenSSH, pinned host verification, locally held keys, and loopback forwards. Tailscale SSH is a separate feature and is not required. Windows and Linux can be paired with their owners' separate Codex accounts.

## 1. Permit either computer to initiate

Use existing connected Tailscale installations where available. Do not re-enroll or reset a working device merely to add Bridge. For a new device, follow the [official installation instructions](https://tailscale.com/download).

Either-direction initiation requires network policy allowing both A-to-B and B-to-A TCP access to the selected receiving SSH ports. Use approved same-tailnet access or the necessary reciprocal accepted device shares. Replies over an established connection do not prove permission to originate a new connection the other way. See [Tailscale sharing](https://tailscale.com/docs/features/sharing).

A device entry, TSMP ping, or `ShareeNode:true` is not proof that TCP/SSH is allowed or denied. Diagnose actual reachability in each direction. Preserve existing gameplay rules and do not open Bridge's HTTP ports to the network.

## 2. Prepare SSH receiving access on both computers

Follow [Windows/Linux receiving setup](RECEIVING-SSH.md). Each computer keeps its own private key, authorizes only the other's public key, and pins the other's verified SSH host key. Both run SSH receiving services with exact loopback forwarding permissions and no remote command/session access for the Bridge key.

SSH service startup is an OS setting, separate from daemon owner-login startup. The Bridge installer does not install/modify receiving services or firewall rules implicitly.

## 3. Keep the network available after restart

On Windows, the owner may enable **Run unattended** in Tailscale preferences, as described in [Tailscale's guide](https://tailscale.com/docs/how-to/run-unattended). On Linux, retain the distribution's Tailscale service setup. This keeps the private network available; it does not start Codex or prove Bridge readiness.

Each owner still signs in for their Bridge daemon and local Codex credentials. Expired device authentication needs local renewal; startup cannot renew it automatically.

## 4. Pair and select a project

Run [setup](SETUP.md) on each computer. Exchange private Bridge invitations through a selected private file-sharing channel. Forwarding-only SSH does not supply SFTP/SCP; don't loosen receiving restrictions for the exchange.

Configure both SSH receiving endpoints and the same approved candidate map, with distinct loopback listener ports for each initiator. Pick the same project ID with local workspace/export/import roots on each computer. An old one-origin route can remain in legacy mode during preparation; it cannot claim managed either-side initiation.

## 5. Wait at sign-in, connect on demand

Set `$bridge` to the Windows `scripts/bridge.ps1`, or `bridge` to Linux `scripts/bridge.sh`:

| Action | Windows | Linux |
| --- | --- | --- |
| Enable waiting startup | `& $bridge autostart-enable --component daemon` | `sh "$bridge" autostart-enable --component daemon` |
| Start daemon now | `& $bridge start --interactive` | `sh "$bridge" start --background` |
| Inspect connection | `& $bridge connection-status --peer ID` | `sh "$bridge" connection-status --peer ID` |
| Connect when requested | `& $bridge connect --peer ID --request-id UUID` | `sh "$bridge" connect --peer ID --request-id UUID` |

Use a fresh UUID for new intent and retain it when repeating the same operation. Existing healthy authenticated tunnels are reused regardless of initiator. Offline peers produce bounded failures; there is no infinite polling/dialing loop. Persistent startup is a separate opt-in for a selected peer and uses the same finite budgets. See [connection behavior](CONNECTIONS.md).

Verify actual SSH authentication, both forwards, Bridge authorization, and native task results separately. `tailscale ping` distinguishes direct and relay paths; an initial relay may later become direct. It does not authenticate Bridge. See [connection types](https://tailscale.com/docs/reference/connection-types).

## Migration and rollback

Record the old route, owning process, listener ports, and startup registration. Preserve the current working connection while installing an isolated candidate and preparing the reverse receiving authorization. Disable only unwanted old transport startup; do not disable the waiting daemon or reset the private network.

Coordinate a daemon maintenance interval after active work drains. Authenticate the new route before retiring its predecessor through the old route's owner. Never run both over competing listener ports or use a broad process kill. Roll back source/registration if needed while retaining current sessions/results; do not restore an old state database over new work. See the [full migration procedure](CONNECTIONS.md#migrate-without-losing-work).

## Stop or revoke

`disconnect --peer ID --request-id UUID` pauses that peer, preserving pairing and work. `autostart-disable --component transport --peer ID` removes future persistent demand without stopping unrelated routes. Revocation is stronger: remove Bridge pairing, the selected authorized public key, and that peer's network grant deliberately. Do not remove shared SSH/Tailscale configuration used by other work.
