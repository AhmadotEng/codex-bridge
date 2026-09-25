# Codex runtime readiness and repair

A Bridge task can arrive and produce a reply while native tools are unavailable. Treat task delivery, dialogue completion, command execution, and project correctness as separate checks.

Bridge starts the owner's selected local Codex executable through the [Codex App Server](https://learn.chatgpt.com/docs/app-server). The private `codex_path` setting selects that runtime; the desktop app may be using a different installed bundle.

Execution permissions come from the locally selected project policy. `read-only` and `workspace-write` keep their restricted, network-disabled worker modes. An explicitly configured `full-access` project uses Codex `danger-full-access` under the same local owner account. It does not repair a broken runtime, supply missing authentication, or grant OS elevation. See [project permissions](PERMISSIONS.md) before diagnosing an intended policy restriction as a runtime failure.

## Select a complete local bundle

Point `codex_path` at the executable inside a complete, trusted local Codex runtime distribution. Keep its companion files together in their original version directory. Do not copy only `codex.exe`, mix helpers from different versions, or copy another person's account files or SSH keys.

The observed Windows bundles for **0.153.4** and **0.155.0-alpha.2.6** require these three siblings beside `codex.exe`:

- `codex-code-mode-host.exe`
- `codex-command-runner.exe`
- `codex-windows-sandbox-setup.exe`

For those two observed layouts, Bridge checks that the siblings are regular, nonempty, readable files. An incomplete bundle is refused. These filenames are deployment observations, not a universal layout specification for every Codex release or platform.

This check does **not** verify executable architecture, publisher signatures, distribution hashes, version consistency, or the ability to launch a native command. An owner must select a trusted complete distribution; Bridge does not repair or download missing binaries. Where trusted release hashes and platform signature verification are available, check them locally before selecting a replacement.

Other versions still undergo generated-schema compatibility checks. Their bundle layout is reported as `unknown`; non-Windows layout checking is `not_applicable`. A schema-compatible version with an unknown layout is not automatically rejected, and has not thereby been behaviorally verified.

## Read preflight correctly

Run on the affected computer:

```powershell
$bridge = Join-Path $env:USERPROFILE 'plugins\codex-bridge\scripts\bridge.ps1'
& $bridge preflight --project-id sample-project
```

| Check | What it establishes |
| --- | --- |
| Runtime schema | The selected executable exposes the App Server contract Bridge requires |
| Runtime bundle `complete` | The required files for an observed layout passed the limited file checks |
| Runtime bundle `incomplete` | At least one expected companion is absent, empty, invalid, or unreadable; repair the local selection |
| Runtime bundle `unknown` / `not_applicable` | This layout has no applicable static profile; inspect the distribution locally |
| Local sign-in | Codex reports a saved local sign-in |
| Route, pairing, and project scope | The configured connection and selected permissions pass their reported checks |

Preflight is a read-only readiness check: it invokes Codex for version, schema, and login status, but sends no model turn and runs no project command or native-command smoke test. Its `ok` result means its required static/configuration checks passed. `native_tool_execution: "not_checked"` remains explicit even when every required check passes.

## Repair only the selected runtime

Use this procedure when a complete, suitable runtime is already installed locally. For an actual Bridge software upgrade, follow [the separate update procedure](ADVANCED.md#update-an-existing-installation).

1. Coordinate with the project owner. Finish or cancel Bridge-managed work and wait until its worker is released. Keep the existing session and conversation IDs.
2. Record the current local runtime selection and keep a protected local backup of the private Bridge configuration. Do not export that backup or restore an old state database over newer work.
3. Stop only this computer's idle Bridge daemon:

   ```powershell
   & $bridge stop
   ```

   Confirm the result reports `stopped: true` before continuing. Leave the working SSH/Tailscale transport and unrelated applications running.
4. Select the executable inside the complete local bundle:

   ```powershell
   & $bridge setup --codex 'C:\Path\To\CompleteBundle\codex.exe' --batch
   if ($LASTEXITCODE -ne 0) { throw 'Runtime selection failed; inspect the reported checks before continuing.' }
   ```

   Use the same `--config` file as the existing installation if it has a custom location, placing `--config PATH` before each command name. For an existing configuration, this command changes only `codex_path`. It preserves computer identity, pairing credentials, projects, policies, local actions, model settings, state, session/context records, and transport/startup settings. It does not re-register MCP or edit Codex authentication.
5. Restart through the current launch configuration:

   ```powershell
   & $bridge start
   & $bridge preflight --project-id sample-project
   & $bridge status
   ```

   With owner-login startup already enabled, `start` uses the existing registered task. Do not re-enable or replace startup registration just to change `codex_path`. Manual installations retain their selected launch mode.
6. Run the bounded native-tool check below in the existing collaboration conversation before claiming the repair works.

On macOS/Linux, use the installed `scripts/bridge.sh` launcher and an appropriate local Codex path. Runtime selection and retained-state rules are the same; the Windows sibling profile does not apply.

If the replacement fails, stop the idle daemon and select a previously verified complete local bundle with the same command. Preserve current state and request records. Missing authentication requires the owner's local sign-in, never a copy of another computer's credentials.

## Prove native execution in the existing session

After the owner authorizes a harmless test, reuse the designated collaboration session. A suitable task is:

> Run a native command to read the selected test README and compute its SHA-256. Do not change any files. Report the result and wait for completion in this same conversation.

Use `task_status` or `task_wait` and inspect `task.result.execution_evidence` on the completed task. Bridge derives it from App Server `commandExecution` completion items scoped to that task's confirmed conversation and turn, rather than from the model's prose.

| Field | Meaning |
| --- | --- |
| `native_command_execution: "observed"` | At least one completed/failed native command item had an integer exit code |
| `native_command_execution: "not_observed"` | No qualifying item was observed; this is not itself a failure verdict |
| `execution_observed_count` | Command items with terminal execution evidence |
| `successful_exit_count` | Observed command items with completed status and exit code zero |
| `unsuccessful_exit_count` | Other observed command exits: failed status or a nonzero exit code |
| `items` | Bounded item IDs, statuses, exit codes, and durations for this turn |
| `tracking_limit_reached` | Counts are lower bounds because the tracking limit was reached |

A dialogue-only task can validly complete with no native command. A declined or failed item without an exit code does not establish execution. A successful process exit still does not prove the requested file content or application behavior.

For a file-writing test, first authorize that operation within the selected project's policy. Use a tiny selected input and a new destination in its export folder, refuse overwriting an existing file, and compute the output SHA-256 with a native command. Fetch that exact artifact and independently compare its bytes/hash with the expected result. File delivery alone proves neither that a command created it nor that the surrounding project works.

For a full-access maintenance project, also verify an explicitly authorized temporary file outside the workspace and a non-sensitive approved network request. Record policy, command evidence, and observed results separately. Do not use account credentials, browser sessions, private SSH keys, or an OS privilege change as a smoke test. A `full-access` configuration entry by itself does not prove that the selected runtime executed with that policy.

The structured evidence omits raw commands, working directories, environment, and output. Detailed task text or owner-inspected local conversation history can still contain project information; share only what the task requires.

## Evidence and limits

A repair on the separately deployed 0.1 branch replaced only the selected runtime path, retained an existing conversation, and completed an actual native PowerShell command with exit code zero. A selected output file was independently retrieved and its hash matched. This is evidence for that deployment and task, not a claim that every new user or schema-compatible runtime works.

The public 0.3.1 checks and their isolated tests are recorded separately in [TESTING.md](TESTING.md). Neither static checks nor the deployment smoke test establishes game readiness or any other project's acceptance criteria.
