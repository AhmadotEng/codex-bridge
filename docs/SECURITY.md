# Security and operating boundaries

Codex Bridge is a scoped collaboration service for computers whose owners have authorized pairing and local project work. It does not make an untrusted person, prompt, repository, or document safe to execute. Keep authorization decisions on the computer that owns the data.

## Credentials and transport

The daemon listens on `127.0.0.1` only. Its MCP client uses a local bearer credential from local configuration and bypasses environment HTTP proxies. Peers use separate bridge-only incoming/outgoing credentials, exchanged in pairing invitations over a verified private channel. The local administration credential is not the peer credential, and peer credentials cannot invoke local administration operations.

An SSH connection supplies encryption and peer host authentication. The bridge supervisor uses a pinned known-hosts file, `StrictHostKeyChecking=yes`, an explicitly selected local identity, batch mode, and loopback `-L` and `-R` forwards. It does not request a shell. Existing reverse-tunnel relays remain transport components; a reverse forwarding permission is not permission to run commands on the relay host.

SSH private keys remain on the computer that owns them. Bridge never automatically collects Codex account tokens, browser sessions, or private keys, and they are absent from invitations and published plugin bundles. Selected artifact contents are not automatically screened for secrets; owners must keep credentials out of transfer folders. The Codex App Server authenticates through the local user's existing Codex installation. The bridge does not implement account login, copy `auth.json`, or import another person's account.

Pairing invitations are secrets even though they are not account tokens. Keep export files in the dedicated protected exchange directory and delete exchange copies after import. Configuration, the SQLite state store, logs, and cached artifacts may contain credentials or project data. Local administrators and programs running as the owner remain within the local machine's trust boundary.

The recommended Tailscale route carries ordinary OpenSSH; it does not replace SSH host verification or local key authentication. Its network access, device-sharing policy, and node-key expiry are independent of Bridge pairing. The setup assistant does not silently install network services, change firewall rules, enable Tailscale SSH, or modify other tunnels.

## Startup and process ownership

Windows startup is opt-in and limited to the signed-in owner; Linux uses an optional systemd user service. Ordinary startup registers the waiting daemon only. Persistent peer connections are a separate explicit choice; future peers are not automatically included. Windows tasks contain no account passwords and request no elevated execution. Full-access project policy does not change their operating-system principal.

Start/stop controls use Bridge-owned process identity and lifecycle locks. A stale or unverifiable process record does not authorize terminating an arbitrary PID. A peer disconnect persists until its local owner resumes; disabling future startup preserves currently running work. Uninstalling requires disabling startup, stopping its components, and removing their registrations.

Tailscale unattended mode and automatic OpenSSH service startup can provide the network before login. That does not grant access to a Codex account or turn Bridge into a Windows system service. An expired Tailscale login or revoked SSH key can still interrupt a route independently of Bridge startup.

## Project and tool scope

Every received operation must match an enabled peer, a project selected locally for that peer, and a session belonging to that peer. Tasks, messages, artifacts, and context updates are separate allowed operation classes. A session uses the intersection of both computers' configured operations. Local project changes continue to constrain existing sessions.

Peer RPC exposes defined collaboration operations; it cannot select a different execution policy, add trusted computers, or change project configuration. Those choices belong to local owner setup. Messages and artifacts do not grant additional permissions. A full-access worker is intentionally allowed to run owner-level commands, including authorized maintenance outside the project directory; the RPC boundary is not a filesystem sandbox around that worker. The safe local-status and local-chat views remain unavailable through peer credentials.

Different sessions retain different context and conversation IDs. Sessions serialize their task turns to avoid concurrent modification of the same conversation. Project/session state separation does not create separate Windows users or an operating-system container. Selecting the same physical workspace for two projects can still create ordinary file-level interference; choose separate directories when isolation is needed.

## Codex execution policy

Workers run the locally installed Codex App Server with the local model default. The adapter checks the selected executable's generated schema against the requests, policies, responses, and notifications it needs; incompatible contracts are refused. Schema acceptance is separate from behavioral verification. It uses retained `thread/start` or `thread/resume` conversations owned by the bridge and never searches arbitrary personal conversations for a match.

Known Windows bundle profiles also check required companion-file presence and readability. This is not signature, distribution-hash, architecture, version-consistency, or execution verification. Select a complete trusted local runtime; do not mix binaries across versions or copy authentication to repair it. Unknown layouts remain explicitly unverified. Static preflight sends no model turn or project command. [Runtime repair](RUNTIME.md) preserves scopes and existing account settings.

The default task policy is `read-only`. Local owners can select one of these policies per project:

| Policy | Worker execution |
| --- | --- |
| `read-only` | Read-only Codex sandbox; network disabled |
| `workspace-write` | Codex writes restricted to the configured writable roots; network disabled |
| `full-access` | Codex `danger-full-access`; owner-account filesystem/commands/network without the project sandbox |

`full-access` must be chosen in local project configuration. Peer-supplied fields cannot upgrade a task's policy. The selected workspace remains its working/context directory, **not** a filesystem security boundary in this mode. Authorizing it means trusting the designated peer's maintenance tasks with access to owner-readable/writable files and network services. It grants neither root/administrator elevation nor a sudo password. Operating-system permissions and local account authentication still apply. Approval requests and unsupported interactive prompts remain reported failures, not silent approval or copied credentials. See [permission setup](PERMISSIONS.md).

The restricted policies rely on Codex's sandbox implementation and the host operating system. They are **not a hard read container**: a read-only policy prevents requested writes, but does not guarantee that the worker cannot read every other file visible to its operating-system account. Full-access deliberately removes the project sandbox; use a separately restricted operating-system account if a narrower account boundary is needed.

Personal MCP servers, plugins, apps, and hooks remain disabled for worker turns by default, including full-access turns; the adapter checks for unexpected MCP exposure. Full access enables native commands/network, not automatic loading of personal integrations. A scoped bridge-only integration requires a separately reviewed local allowlist. Ordinary desktop conversations keep their own integrations; worker overrides do not rewrite global settings. An unsandboxed worker can nevertheless access resources available to its owner through native commands, so integration disabling is not an account-data isolation boundary.

## Files and results

Each file transfer is one explicitly selected file. Files up to 8 MiB use direct transfer; negotiated chunked transfers support larger files with a default 256 MiB file ceiling, bounded storage, at most eight active transfers, and expiry of incomplete staging data. Export and import roots must be inside the selected workspace. The implementation rejects absolute paths, parent traversal, root escapes, symlinks, and Windows reparse-point paths. Artifact and chunk lookup are scoped to their originating peer and collaboration session.

The receiver checks chunk and final byte lengths and SHA-256 and archives the artifact reference. A different existing destination is not overwritten. Identical transfers can be retried using the same operation ID. Large transfers stage data privately before publishing the completed file. Archive/staging quotas do not include completed files retained in the owner's selected import folders. These artifact API restrictions also apply to full-access projects, but they do not restrict the native file/network abilities of an authorized full-access task.

Task prompts, progress, results, messages, and transferred logs can contain project information. Inspect them before exporting beyond the selected collaboration. Do not place account credentials or private keys in an export root. Peer text should be treated as untrusted content when included in later Codex prompts.

Structured native-command evidence uses App Server completion items from the confirmed conversation/turn and omits raw commands, working directories, environment, and output. It records bounded execution metadata, not a trust verdict about a command or its effects. Dialogue completion, process exit zero, and file delivery are distinct evidence; check expected file bytes/hash and project behavior separately.

## Retries, interruption, and revocation

Mutating requests use durable caller-chosen IDs and content digests. Reusing an ID with different intent is rejected. Request records are persisted before dispatch, and retries query or return the existing operation rather than beginning another execution. Preserve IDs across disconnects and timeouts.

There is an unavoidable uncertainty window when a process dies after dispatch but before durable completion. Started requests are marked `uncertain` on restart and are not automatically replayed. Inspect the designated Codex conversation and actual effects before issuing a new operation ID. The bridge does not promise transactional rollback or exactly-once completion of arbitrary external actions.

Cancellation targets only bridge-managed tasks. It is cooperative at the Codex turn boundary and cannot undo completed actions. Revocation disables peer authentication and access and requests cancellation of that peer's local running requests; pending outgoing delivery is stopped. Work already delivered to the other computer can require cancellation or revocation there as well.

Bridge revocation is independent of SSH authorization. Removing a bridge pairing does not remove an existing SSH authorized key, terminate unrelated tunnels, or change gameplay routes. End those separately only when the owner requests it. Uninstalling the plugin does not automatically erase project files, retained conversations, audit state, or backups.
