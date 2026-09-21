# Security and operating boundaries

Codex Bridge is a scoped collaboration service for computers whose owners have authorized pairing and local project work. It does not make an untrusted person, prompt, repository, or document safe to execute. Keep authorization decisions on the computer that owns the data.

## Credentials and transport

The daemon listens on `127.0.0.1` only. Its MCP client uses a local bearer credential from local configuration and bypasses environment HTTP proxies. Peers use separate bridge-only incoming/outgoing credentials, exchanged in pairing invitations over a verified private channel. The local administration credential is not the peer credential, and peer credentials cannot invoke local administration operations.

An SSH connection supplies encryption and peer host authentication. The bridge supervisor uses a pinned known-hosts file, `StrictHostKeyChecking=yes`, an explicitly selected local identity, batch mode, and loopback `-L` and `-R` forwards. It does not request a shell. Existing reverse-tunnel relays remain transport components; a reverse forwarding permission is not permission to run commands on the relay host.

SSH private keys remain on the computer that owns them. Codex account tokens, browser sessions, and private keys are not included in bridge invitations, project artifacts, or plugin bundles. The Codex App Server authenticates through the local user's existing Codex installation. The bridge does not implement account login, copy `auth.json`, or import another person's account.

Pairing invitations are secrets even though they are not account tokens. Keep export files in the dedicated protected exchange directory and delete exchange copies after import. Configuration, the SQLite state store, logs, and cached artifacts may contain credentials or project data. Local administrators and programs running as the owner remain within the local machine's trust boundary.

## Project and tool scope

Every received operation must match an enabled peer, a project selected locally for that peer, and a session belonging to that peer. Tasks, messages, artifacts, and context updates are separate allowed operation classes. A session uses the intersection of both computers' configured operations. Local project changes continue to constrain existing sessions.

Peer tools cannot add trusted computers, select arbitrary workspace roots, edit local bridge configuration, install plugins, or call an unrestricted shell operation. Messages and artifacts are data, not instructions that can expand local authorization. The twelve MCP tools expose the defined collaboration operations; local administration remains in the CLI.

Different sessions retain different context and conversation IDs. Sessions serialize their task turns to avoid concurrent modification of the same conversation. Project/session state separation does not create separate Windows users or an operating-system container. Selecting the same physical workspace for two projects can still create ordinary file-level interference; choose separate directories when isolation is needed.

## Codex execution policy

Workers run the locally installed Codex App Server with the local model default. The adapter only accepts the schema-validated versions listed in its capabilities. It uses retained `thread/start` or `thread/resume` conversations owned by the bridge and never searches arbitrary personal conversations for a match.

The default task policy is `read-only`. `workspace-write` must be chosen in local project configuration and restricts the writable roots requested from Codex to the selected workspace. Network access is disabled for worker execution. Approval requests and unsupported interactive prompts are denied and reported; they are not silently approved or redirected to another account.

These policies rely on Codex's sandbox implementation and the host operating system. They are **not a hard read container**: a read-only policy prevents requested writes, but does not guarantee that the worker cannot read every other file visible to its Windows account. Use a separately restricted operating-system account or stronger isolation if that is required. Do not authorize task execution for a peer who should not be trusted with the resulting project outputs.

Personal MCP servers, plugins, apps, and hooks are disabled for worker turns by default, and the adapter checks that unexpected MCP tools are not exposed. A scoped bridge-only integration may be explicitly allowlisted by a reviewed local installation. That exception must not silently enable personal integrations or make bridge pairing equivalent to access to email, browser sessions, or unrelated applications. Ordinary local desktop conversations keep their own configured integrations; the worker overrides do not rewrite the user's global settings.

## Files and results

Each file transfer is one explicitly selected file, at most 8 MiB. Export and import roots must be inside the selected workspace. The implementation rejects absolute paths, parent traversal, root escapes, symlinks, and Windows reparse-point paths. Artifact lookup is scoped to its originating collaboration session.

The receiver checks byte length and SHA-256 and archives the artifact reference. A different existing destination is not overwritten. Identical transfers can be retried using the same operation ID. This protects the bridge's transfer boundary; it is separate from the read/write abilities of an explicitly authorized Codex task.

Task prompts, progress, results, messages, and transferred logs can contain project information. Inspect them before exporting beyond the selected collaboration. Do not place account credentials or private keys in an export root. Peer text should be treated as untrusted content when included in later Codex prompts.

## Retries, interruption, and revocation

Mutating requests use durable caller-chosen IDs and content digests. Reusing an ID with different intent is rejected. Request records are persisted before dispatch, and retries query or return the existing operation rather than beginning another execution. Preserve IDs across disconnects and timeouts.

There is an unavoidable uncertainty window when a process dies after dispatch but before durable completion. Started requests are marked `uncertain` on restart and are not automatically replayed. Inspect the designated Codex conversation and actual effects before issuing a new operation ID. The bridge does not promise transactional rollback or exactly-once completion of arbitrary external actions.

Cancellation targets only bridge-managed tasks. It is cooperative at the Codex turn boundary and cannot undo completed actions. Revocation disables peer authentication and access and requests cancellation of that peer's local running requests; pending outgoing delivery is stopped. Work already delivered to the other computer can require cancellation or revocation there as well.

Bridge revocation is independent of SSH authorization. Removing a bridge pairing does not remove an existing SSH authorized key, terminate unrelated tunnels, or change gameplay routes. End those separately only when the owner requests it. Uninstalling the plugin does not automatically erase project files, retained conversations, audit state, or backups.
