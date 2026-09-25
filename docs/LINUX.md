# Linux setup and the one-file launcher

The Linux tar.gz includes `scripts/setup.sh` and the same Bridge source as the Windows ZIP. No PowerShell is required. The optional `.pyz` is one application file, not a bundled interpreter or Codex account. Use local 64-bit Python 3.11+ and a complete native Codex installation. These entry points are included in the 0.3.2 release candidate; v0.3.1 lacks them.

## Before running it

Use the Linux account that will own the collaboration. Do not run setup with `sudo`, copy another person's Codex login, or reuse their private Bridge configuration.

```sh
python3 --version
codex --version
codex login status
```

Use matching Linux **x86_64** or **aarch64/ARM64** binaries, including the complete distribution's helper files. Windows `.exe` files cannot supply the Linux runtime. Codex packaging layouts vary; keep the selected distribution intact rather than copying its main executable alone. See the [runtime guide](RUNTIME.md) and [official package layout](https://github.com/openai/codex/blob/8dd0a0816064a1cbc57caeb1cd957be8ebcce45b/scripts/codex_package/layout.py).

Native helpers also need compatible dynamic loaders and libraries. In the isolated Alpine check, the official 0.153.4 musl package's main runtime initialized successfully, but its bundled zsh requested a glibc loader absent on that guest. That check does not establish shell/tool or sandbox execution on Alpine. Test an actual authorized native command under the owner's selected runtime before declaring a deployment ready; do not replace individual helpers or silently force a different shell.

Codex sign-in stays local. If sign-in is missing, use Codex's local login workflow; headless device-code login is described in the [official authentication guide](https://learn.chatgpt.com/docs/auth). A keyring-backed login may require its owner's unlocked session and environment. Do not move account storage to a system account to make a service work.

Tailscale supplies reachability. Bridge uses **ordinary OpenSSH over that connection**, with pinned host verification and an existing authorized key. The computer initiating the route needs the OpenSSH client; the receiving computer needs its owner-configured SSH server. Tailscale SSH is a separate feature and is not required. This launcher does not install network services, change firewalls, or modify other routes.

## Choose one entry point

From a source checkout:

```sh
sh scripts/setup.sh
```

Select a particular interpreter if necessary:

```sh
CODEX_BRIDGE_PYTHON=/path/to/python3.11 sh scripts/setup.sh
```

For a supplied one-file package, keep its `.sha256` beside it and verify it against the hash supplied through your trusted download/handoff:

```sh
sha256sum --check codex-bridge.pyz.sha256
python3 ./codex-bridge.pyz doctor
python3 ./codex-bridge.pyz setup
```

`doctor` prints dependency hints and architecture information. It does not authenticate a peer or run a Codex command. Setup selects the local runtime, creates or preserves this computer's configuration, and guides pairing/project selection. MCP registration is enabled by default; `setup --skip-registration` leaves registration to the owner. `setup --batch --peer-id friend-linux --codex /absolute/path/to/codex` performs unattended local configuration without asking pairing questions.

Both entry points default to `~/plugins/codex-bridge` and `~/.codex-bridge/config.json`. A one-file package refuses to overwrite an unrelated, modified, or different-version installation. Select a separate empty installation directory when testing a preview. It can safely reuse its own unchanged installation.

For a custom one-file location, put global options **before** the command:

```sh
python3 ./codex-bridge.pyz --directory "$HOME/preview/codex-bridge" --config "$HOME/.codex-bridge-preview/config.json" setup
```

Use that `--directory` for subsequent commands. The saved local configuration path is reused unless you explicitly supply another. Source setup also accepts `--directory` and `--config`; use `sh scripts/setup.sh --help` for its options. Do not point a preview at a running installation.

## Start, inspect, and stop

With the one-file package:

```sh
python3 ./codex-bridge.pyz start --background
python3 ./codex-bridge.pyz status
python3 ./codex-bridge.pyz preflight --project-id sample-project
```

The installed shell entry exposes the same commands:

```sh
bridge="$HOME/plugins/codex-bridge/scripts/bridge.sh"
"$bridge" status
"$bridge" preflight --project-id sample-project
```

On either computer when collaboration is requested:

```sh
python3 ./codex-bridge.pyz connect --peer other-computer --request-id "$(python3 -c 'import uuid; print(uuid.uuid4())')"
python3 ./codex-bridge.pyz connection-status --peer other-computer
```

The configured forward is loopback-only in both directions: local peer port to the receiving Bridge, and remote return port to the initiating Bridge. Use distinct local ports for distinct peers. Neither side exposes its Bridge HTTP listener to the LAN or Tailscale interface. A listening SSH socket is insufficient proof; run `preflight` on both computers and exchange a scoped task.

Stop only the selected components:

```sh
python3 ./codex-bridge.pyz disconnect --peer other-computer --request-id "$(python3 -c 'import uuid; print(uuid.uuid4())')"
python3 ./codex-bridge.pyz stop
```

`stop` preserves configuration, conversation IDs, context, deduplication records, and project files. Restarting with `start --background` reuses that state. Normal transport shutdown uses recorded process identity and process groups. Abruptly killing a Linux supervisor is not guaranteed to remove its SSH child; inspect transport status and retained process records before restarting. Do not use broad `pkill ssh` commands.

## Startup and conversations

After manual execution works, opt into waiting startup with `sh "$bridge" autostart-enable --component daemon`. On supported Linux distributions it registers a systemd **user** service. It does not start SSH or create a persistent connection. `autostart-status` inspects registration; `autostart-disable --component daemon` disables future starts while preserving current work. A missing user service manager is reported; manual background startup remains available.

The user unit uses the installed Python/code/configuration and the owner's environment. It does not run as root, enable lingering, or install a system-wide Bridge service. Test owner login/logout, credential/keyring access, and reboot on the actual distribution. Headless and graphical sessions may expose different credentials. A registered unit is not a successful reboot test. Optional `autostart-enable --component transport --peer ID` is a separate per-peer persistent choice with bounded attempts, not an infinite retry loop.

Each computer has its own local Codex conversation. Follow-ups resume its designated conversation through App Server; accounts and unrelated chats are not mirrored. Linux desktop availability is documented by [OpenAI](https://learn.chatgpt.com/docs/linux/linux-app), but Bridge's `codex://` opening, sidebar refresh, and graphical session behavior require testing on the selected desktop. CLI conversation continuity does not prove desktop integration.

For a new Linux computer, create a distinct peer identity and local conversation. Preserve existing peers and history. Legacy protocol-1 routes remain separate, with transfers at or below 8 MiB for old peers. The new either-origin connection manager requires compatible releases on both ends and both receiving authorizations. Do not start it over ports owned by an old supervisor; use the [migration procedure](CONNECTIONS.md#migrate-without-losing-work).

## Build a one-file package

From the source repository, with Python 3.11+:

```sh
python3 scripts/build_launcher.py --output /path/to/output/codex-bridge.pyz
```

The builder refuses to overwrite an existing output and emits the `.pyz` plus its SHA-256 file. It packages only the installer allowlist and a bootstrap, excluding local configuration, account files, SSH keys, state, tests, and logs. The archive's internal checks detect damaged payload files; they do not establish publisher authenticity. Verify the external package hash through your trusted source.

Read [test evidence and remaining checks](TESTING.md) before treating a platform as verified. Passing static runtime checks does not prove native tool execution, peer pairing, sandbox behavior, or a project's own acceptance criteria.
