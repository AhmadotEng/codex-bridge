# On-demand connections

Bridge waiting startup and SSH dialing are separate. `autostart-enable --component daemon` starts the waiting daemon at owner sign-in. It does not launch SSH, even with queued work or undelivered results. Status checks do not grant dialing permission.

## Owner commands

Use the installed `scripts/bridge.ps1` on Windows or `sh scripts/bridge.sh` on Linux. Both accept:

| Command | Behavior |
| --- | --- |
| `status` | Inspect the local daemon |
| `start --interactive` (Windows), `start --background` (Linux) | Start only the local daemon |
| `connection-status --peer ID` | Read connection stages without dialing |
| `connect --peer ID --request-id UUID` | Reuse or establish an authorized connection |
| `connect --peer ID --retry --request-id UUID` | Explicitly resume/retry an exhausted or stopped connection |
| `disconnect --peer ID --request-id UUID` | When work is drained, persist stop intent and close only this pair's owned route; otherwise report busy |
| `autostart-enable --component daemon` | Register future waiting startup |
| `autostart-enable --component transport --peer ID` | Explicit persistent policy for this one peer |
| `autostart-disable --component transport --peer ID` | Remove future automatic demand; leave current work intact |

The MCP equivalents are `local_status`, `connection_status`, `connection_ensure`, `connection_retry`, and `connection_disconnect`. A user-requested connection operation may bootstrap this computer's daemon. Remote RPC callers cannot bootstrap/administer a computer. If a wrapper is missing, use `python -I /absolute/install/scripts/run_bridge.py --config /absolute/private/config.json COMMAND` with the local interpreter (`python3` on Linux).

Retain each mutation's UUID and identical arguments after a timeout. A new request ID means new intent. Status, task waiting, pending delivery, and sign-in do not replenish a connection budget. An unavailable remote daemon requires an owner-local startup command, never a shell through the forwarding key.

## Pairing and route configuration

Keep addresses, account names, SSH key paths, pinned host files, and loopback ports in each owner's configuration. Exchange Bridge invitations privately in both directions. Prepare [receiving access](RECEIVING-SSH.md) on both computers. Neither side copies the other's private SSH key.

Each pair reserves two candidates, identified by initiator. Each candidate has one listener on each computer and points to the opposite local daemon. For example, with daemon port 47321 on both computers:

| Candidate | A's peer listener | B's peer listener |
| --- | --- | --- |
| Initiated by A | `127.0.0.1:47422` | `127.0.0.1:47422` |
| Initiated by B | `127.0.0.1:47423` | `127.0.0.1:47423` |

These are examples, not automatically chosen ports. Both owners must approve the exact map; reserve different ports for other pairs and existing legacy routes. SSH `permitopen` permits the receiving Bridge destination; `permitlisten` permits only the receiving end's candidate listener. An unrelated occupied port is reported, not killed or bypassed. Do not manually rewrite the peer URL during live dispatch.

After `transport-config` has recorded each owner's outbound SSH identity/receiver settings, save the same non-secret `lanes.json` on both computers (replace the IDs with the actual immutable paired IDs):

```json
{
  "computer-a": {"computer-a": 47422, "computer-b": 47422},
  "computer-b": {"computer-a": 47423, "computer-b": 47423}
}
```

On A run `connection-config --peer computer-b --lanes-file /path/to/lanes.json`; on B run `connection-config --peer computer-a --lanes-file /path/to/lanes.json`. Use the appropriate installed Windows/Linux launcher. This validates/saves local connection settings only; it does not authorize SSH, open a firewall, change network policy, or dial. Both endpoints must configure the same map before `connect`. The enclosing `connections` entry is keyed by the other peer, while the lane map is keyed first by initiator and then by endpoint.

Legacy fixed forwarding remains separate. An older peer without `on_demand_tunnel_v1` cannot participate in managed dual initiation. Keep its existing connection intact until both peers are upgraded and a new map is verified. The prerelease's new selection/lifetime behavior remains experimental until its exact Windows/Linux candidates complete the real-pair acceptance tests listed in [TESTING.md](TESTING.md).

## One committed tunnel

An existing healthy, mutually authenticated connection wins. Otherwise either endpoint can propose a candidate. Separate approved ports avoid simultaneous bind conflicts. The lower immutable computer ID coordinates selection; normalized UTF-8 IDs and canonical attempt UUIDs determine ties within a short selection interval.

SSH authentication, both forwards, paired Bridge challenge/response, and a durable matching selection decision are required before dispatch. Proposed, prepared, committed, draining, and closed states distinguish progress. Generation checks reject delayed operations on superseded connections. Only the losing candidate's owner closes its own verified SSH process after the winner is confirmed.

Lost acknowledgments are reconciled against durable state. A timer, stale PID, existing listener, or persisted connection record cannot prove health. If agreement is uncertain, new dispatch pauses rather than electing competing routes. Existing accepted execution may finish and save its result.

## Bounded attempts and lifetime

| Situation | Default limit |
| --- | --- |
| Waiting-only startup | Zero SSH launches |
| Explicit initial demand | Two launches within 45 seconds, including verification |
| Per SSH launch | Up to 10 seconds to connect, bounded by the remaining deadline |
| Active-work recovery | Three launches within 120 seconds |
| Idle close | Eligible after 15 minutes without meaningful activity |

Terminal host-key, authentication, forwarding-policy, pairing, and unrelated-port failures stop immediately. Exhaustion reports the failing stage and requires explicit retry. A restart suspends an interrupted attempt; it does not create fresh allowances. Persistent startup remains a separately saved demand and uses the same limits.

An explicit disconnect also rejects incoming candidate commitment until the local owner resumes. Dialing exhaustion alone may still permit a valid incoming candidate; it is different from deliberately pausing a peer.

Disconnect refuses while either endpoint reports active tasks, transfers, or pending results. Finish or cancel that work and collect its result, then retry the disconnect with the same ID. A refused busy request does not claim that the connection stopped.

An artifact cancellation with `remote_abort_pending` still needs the peer's acknowledgment, even when its local status is `aborted`. Retry the cancellation with its original request ID when the route is available. A completed remote commit remains completed; cancellation does not undo a delivered file. Pending acknowledgments are preserved across connection failures.

If shutdown cannot confirm that an owned SSH child exited, status reports `ssh_cleanup_unconfirmed` and lists its attempt in `cleanup_pending`. Bridge retains the exact process handle and does not start another candidate on that lane. Retry the local disconnect with the same request ID after the problem is resolved; a failed cleanup is not recorded as a successful disconnect. If the daemon has since restarted and lost that handle, stop and inspect the recorded ownership locally. A saved PID alone is never permission to terminate a process or declare its forwarding ports released.

After access is revoked, the local owner can still retry cleanup of Bridge's retained SSH children. That cleanup sends no remote request and preserves pending task, result and transfer records for reconciliation; it does not restore access. Revocation already forbids further communication, so unresolved remote acknowledgments do not prevent this local cleanup.

Idle closure requires both endpoints to agree that no project has active or blocked tasks, transfers, pending messages/cancellation acknowledgments, or undelivered results. Status polling and heartbeats are not activity. Work reserved during drain cancels closure; accepted work, sessions, request IDs, results, and transfer progress survive reconnection. An unknown execution outcome is `uncertain`, not permission to replay side effects.

## Read the stages

Status separates local daemon, network/SSH reachability, host authentication, forwarding, remote Bridge authorization, and remote Codex readiness. Each stage has evidence time and a `not_checked`, `checking`, `pass`, `fail`, or `stale` state. Historical observations are not current probes.

- Before any connection: **Not connected; remote Bridge not checked.**
- SSH works but the Bridge port is unavailable: **SSH connected; remote Bridge unavailable at the configured port.**
- HTTP pairing refusal: **SSH connected; remote Bridge authorization refused.**
- Bridge available but Codex signed out: fix the remote owner's local Codex sign-in; reconnecting SSH will not fix it.

Schema/runtime readiness remains separate from native execution. Inspect a completed authorized task's execution evidence before declaring a deployment ready.

## Migrate without losing work

1. Record and privately back up source/configuration, startup registrations, process ownership, and current work. Keep the rollback code; do not export credentials to another owner or GitHub.
2. Disable only the unwanted legacy peer's transport startup, then stop its exact owned supervisor with that installed version's commands. Leave daemon startup, unrelated connections, keys, scopes, and state intact.
3. Install matching candidates into isolated directories/configurations. Prepare both receivers and unused candidate ports while the working route stays available.
4. Drain workers and coordinate daemon replacement. A restart has a maintenance interval; do not describe it as zero downtime. Keep the old transport until a recovery path is established.
5. Authenticate the managed route on both endpoints; retire the old supervisor through its original owner before reusing any of its ports.
6. Verify both initiating directions, retained work, bounded offline behavior, waiting startup, and native tasks. Report reboot/sign-in observations separately from service registration.

Rollback source/registration only where needed; never restore an old task database over newer results. Never silently re-enable unwanted laptop dialing. See [release evidence](TESTING.md) for completed checks and current limitations.
