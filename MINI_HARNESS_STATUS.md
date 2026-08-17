# Mini Harness Status

## Implemented

- Added `mini_harness` as a separate top-level SDK consumer package in this repository.
- Added explicit agent states, legal transition validation, and event emission.
- Added multi-turn `chat` sessions with reused runtime context and deterministic context compaction.
- Added structured model decisions: `ToolDecision` and `FinalDecision`.
- Added `FakeModelProvider` for deterministic tests and an OpenAI-compatible provider for manual smoke runs.
- Added TOML config loading with OpenAI-compatible and Anthropic model provider support.
- Added provider HTTP diagnostics that report status code, request id, and sanitized API error bodies.
- Added the default semantic runtime tool facade:
  - `file` with actions `list`, `read`, `write`, and `edit`
  - `command` with action `run`
  - `task` with actions `start`, `observe`, `list`, and `cancel`
  - `remote` with actions `ensure_tool` and `request_ssh_connection`
  - `terminal` with actions `open`, `list`, `inspect`, `activate`, `observe`, `command`, `human_input`, `control`, and `close`
- Added `WorkContext` with endpoint/environment/target IDs, path validation, active task tracking, and incremental log cursor.
- Added terminal session state tracking and a `run_command` guard so clean tasks do not accidentally discard root/session state.
- Added explicit local/remote terminal target selection plus OS/shell/backend metadata in context and terminal results.
- Added a capability-based tool permission preflight in `ToolRegistry.execute()` with TOML allow/deny configuration, file/task/terminal/session permission requests, and structured `PERMISSION_DENIED` results.
- Added a policy-only workspace sandbox with path/cwd rules, command guard checks, TOML configuration, and a pluggable `SandboxEngine` interface for future bubblewrap/container/gVisor engines.
- Initialized `mini_harness.sync` with config, ignore matching, manifest scanning, push planning, local state storage, and a runtime-file-API push engine. Sync tools remain internal/experimental and are not exposed through the default agent facade.
- Added Rich event renderer for terminal status/tool/task/final output.
- Added trace writer under `.mini-harness/runs/<run-id>/`.
- Added `mini-harness` console script.
- Added deterministic sample project and tests under `tests/mini_harness`.

## SDK Integration

Mini Harness performs file and task operations through:

```text
Mini Harness -> AgentRuntimeClient -> BrokerTransport -> local broker -> Runtime services/providers
```

The harness adapter calls only the SDK facade for file and task operations. It does not access Runtime repositories, providers, SQLite tables, or subprocess APIs.

## Verification Scope

Current validation uses the commands below:

```powershell
.\.venv\Scripts\python.exe -m ruff check mini_harness tests/mini_harness
.\.venv\Scripts\python.exe -m ruff format --check mini_harness tests/mini_harness
.\.venv\Scripts\python.exe -m pytest tests/mini_harness/unit -q
.\.venv\Scripts\python.exe -m pytest tests/mini_harness/integration -q --basetemp .tmp\pytest-mini-harness
.\.venv\Scripts\python.exe -m ruff check environment_runtime mini_harness tests
.\.venv\Scripts\python.exe -m mypy environment_runtime mini_harness
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp\pytest-full
```

The historical test notes for this repository should be read as implementation-time smoke
evidence, not as a stable release matrix. The useful boundary is:

- Mini Harness unit tests cover deterministic agent-loop, tool, config, permission, sandbox,
  diff, and facade behavior.
- Mini Harness integration tests cover the local broker/SDK path with `FakeModelProvider` and
  a copied sample project.
- Repository-wide tests cover Runtime SDK and provider behavior that Mini Harness depends on.
- Optional Docker SSH tests are manual/opt-in and validate a real SSH endpoint, SFTP, remote
  task recovery, and tmux/PTY behavior when the local Docker test server is available.
- Real OpenAI-compatible or Anthropic runs are smoke tests only. They are non-deterministic,
  depend on external API behavior, and are not treated as CI guarantees.

Note: refreshing the editable install with `pip install -e .` was attempted, but Windows reported `envrt.exe` was locked by another process. The module entrypoint `python -m mini_harness` was added and verified.

## Capability Boundaries and Known Limitations

- Mini Harness is a CLI validation harness for Runtime-backed agent workflows, not a polished
  end-user coding-agent product yet.
- The default model-facing facade exposes `file`, `command`, `task`, `remote`, and `terminal`.
  Internal sync tools exist for development, but sync is not exposed in the default prompt/facade.
- Browser UI, WebSocket UI, MCP, RAG, long-term memory, and sub-agents are intentionally out of
  scope for the current open-source MVP.
- Only one configured SSH runtime is active for a session. Multi-remote routing and live server
  switching are not implemented.
- Password SSH is supported only through interactive secret entry. Plaintext password fields in
  TOML or CLI arguments are intentionally not supported.
- The sandbox is policy-only. It blocks or asks approval for risky paths and command patterns,
  but it is not a process/container isolation boundary yet.
- Capability approval is a CLI one-shot `y/n` decision. There is no persistent approval UI,
  role model, or full RBAC policy yet.
- `file` action `edit` uses guarded exact text replacement. Patch application is intentionally
  not implemented yet.
- `file` operations can target local or the configured remote endpoint. Experimental sync target
  handling is deliberately hidden from the default agent entrypoint until the workflow is complete.
- Long task and terminal summaries are best-effort context hints. They reduce repeated discovery
  steps but are not a source of truth.
- `observe_task` implements an in-harness cursor over the current SDK `task.logs` result because the SDK currently returns complete task logs.
- Runtime task `cwd` is currently interpreted by Runtime execution providers as a process cwd, not automatically as an endpoint-root-relative path. Mini Harness accepts only relative cwd from the model and projects it into the local `project_root` before calling the SDK.
