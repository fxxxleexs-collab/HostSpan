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

## Test Results

Verified during implementation with:

```powershell
.\.venv\Scripts\python.exe -m ruff check mini_harness tests/mini_harness
.\.venv\Scripts\python.exe -m ruff format --check mini_harness tests/mini_harness
.\.venv\Scripts\python.exe -m pytest tests/mini_harness/unit -q
.\.venv\Scripts\python.exe -m pytest tests/mini_harness/integration -q --basetemp .tmp\pytest-mini-harness
.\.venv\Scripts\python.exe -m ruff check environment_runtime mini_harness tests
.\.venv\Scripts\python.exe -m mypy environment_runtime mini_harness
.\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp\pytest-full
```

Results:

- Mini Harness ruff: passed
- Mini Harness ruff format check: passed
- Mini Harness unit tests: 35 passed
- Mini Harness runtime integration tests: 1 passed
- Full repository ruff: passed
- Full repository mypy: passed over 113 source files
- Full repository pytest: 84 passed, 1 skipped
- CLI smoke with `python -m mini_harness run --embedded-broker --fake-model`: passed
- CLI smoke with an Anthropic `mini-harness.toml` and `--fake-model`: passed

Note: refreshing the editable install with `pip install -e .` was attempted, but Windows reported `envrt.exe` was locked by another process. The module entrypoint `python -m mini_harness` was added and verified.

## Known Limitations

- Browser UI, workspace bidirectional sync, MCP, RAG, long-term memory, and sub-agents are intentionally out of scope.
- `file` action `edit` uses exact text replacement. Patch application is intentionally not implemented.
- `observe_task` implements an in-harness cursor over the current SDK `task.logs` result because the SDK currently returns complete task logs.
- Runtime task `cwd` is currently interpreted by Runtime execution providers as a process cwd, not automatically as an endpoint-root-relative path. Mini Harness accepts only relative cwd from the model and projects it into the local `project_root` before calling the SDK.
