# Codex Bridge

Let two people's Codex installations exchange project tasks, replies, and selected files over SSH.

**Use separate Codex accounts.** Each computer keeps its own sign-in, permissions, and project chat. Pair once, then add projects without reinstalling.

This is an independent community project, not an official OpenAI product.

## Start here

On **both computers**, install Python 3.11+, sign into Codex locally, and download this repository. You also need an existing authorized SSH connection with a verified host key and both local and reverse forwarding allowed.

Open PowerShell in the downloaded source folder:

```powershell
.\scripts\setup.ps1
```

The assistant detects Python and Codex, checks the App Server schema, registers the MCP tools, and guides you through:

1. Naming this computer.
2. Exchanging private Bridge invitations.
3. Selecting the existing SSH route on the computer that starts it.
4. Choosing a local project folder and the paired computer.

Use the same **project ID** on both computers, with each person's own folder. The default task policy is read-only. Configuration and credentials are generated separately on each PC; never copy one computer's configuration over the other.

**[Full setup guide, including macOS/Linux](docs/SETUP.md)** · [Computer A example](examples/computer-a.example.json) · [Computer B example](examples/computer-b.example.json)

## Connect and check

On both Windows computers:

```powershell
$bridge = Join-Path $env:USERPROFILE 'plugins\codex-bridge\scripts\bridge.ps1'
& $bridge start --interactive
```

On the computer that starts SSH:

```powershell
& $bridge transport-start
```

Then run on both:

```powershell
& $bridge preflight --project-id sample-project
& $bridge status
```

Preflight separates runtime, local sign-in, SSH, forwarding, pairing, and project-scope checks. Status hides credentials, workspace paths, prompts, and logs.

Windows interactive launch requires the owner to remain signed in. No automatic startup is installed. On macOS/Linux, use the installed `scripts/bridge.sh` and `start --background`.

## Collaborate

Open a fresh Codex chat on either computer and ask:

> Use Codex Bridge. Check the peer, create or reuse the sample-project session, and ask the other computer to summarize its README. Wait for the result and keep follow-ups in the same session.

A session links **two local conversations**. It does not mirror all chats between accounts. Each local chat is created when that computer first receives a task.

To find your local project chat, ask Codex to use `session_chat`, or run:

```powershell
& $bridge show-chat --session-id YOUR_SESSION_ID --open
```

Bridge gives new chats a project title and releases its worker after each turn. Status distinguishes working, released, waiting for the desktop owner, and failed. It never forcibly takes over a chat.

## Another project or computer

For another project, run `project-select` on both computers, choose their folders, and create one new session. Existing projects and local action settings are preserved.

For another computer, use `pair-setup` and `transport-config` with its own peer ID and forwarding ports. Each peer has separate trust, project permissions, and SSH supervisor controls.

## Available tools

| Tools | Purpose |
| --- | --- |
| `bridge_status`, `peer_status` | Safe status and peer capabilities |
| `session_create`, `session_list`, `session_get`, `session_chat` | Project sessions and local chat links |
| `session_context_update`, `message_send` | Context, questions, notes, and replies |
| `task_send`, `task_status`, `task_wait`, `task_cancel` | Work, progress, results, cancellation |
| `artifact_send`, `artifact_fetch` | Selected files with SHA-256 verification |
| `artifact_transfer_status`, `artifact_transfer_cancel` | Progress and cancellation for large files |

Files up to 8 MiB use the original direct transfer. Larger files use verified, resumable chunks when both computers support them, with bounded storage and a default 256 MiB file limit. A queued transfer is not yet delivered: inspect its completed result and hash.

## Limits and details

Compatibility is checked against the selected Codex executable's generated App Server schema. An incompatible runtime is refused. Schema compatibility is separate from real account, sandbox, and platform verification; see [test evidence and remaining checks](docs/TESTING.md).

- [Setup, new projects, chat ownership, and troubleshooting](docs/SETUP.md)
- [Implementation of the onboarding plan](docs/ONBOARDING-PLAN.md)
- [Optional plugin installation, updates, revocation, and uninstall](docs/ADVANCED.md)
- [Security boundaries](docs/SECURITY.md)

Run isolated tests without Codex credentials or a remote computer:

```powershell
python -m unittest discover -s tests -v
```

Licensed under [MIT](LICENSE).
