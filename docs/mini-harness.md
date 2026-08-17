# Mini Harness Agent

Mini Harness Agent is a small CLI agent used to validate that the Environment Runtime SDK can serve as an execution layer for agentic coding workflows. It is intentionally not a full coding-agent product.

## Architecture

```text
CLI
  -> AgentController
  -> AgentLoop
       -> ModelProvider
       -> ToolRegistry
       -> AgentContext
       -> AgentEventSink
            -> RuntimeToolAdapter
            -> AgentRuntimeClient
            -> BrokerTransport
            -> Environment Runtime
```

The package lives at `mini_harness/` so it remains independent from Runtime services/providers while still being tested in the same repository as the SDK it validates.

## Interaction Modes

- `run`: execute one user task and exit.
- `chat`: keep one runtime session open for multiple user turns, preserving the active task/session state and compacted conversation context.

Long chat histories are compacted deterministically inside `AgentContext`: older user/final turns and older tool results are summarized, while recent tool results remain available within the configured context budget.

## Tools

The current version exposes:

- `file` with actions `list`, `read`, `write`, and `edit`
- `command` with action `run`
- `task` with actions `start`, `observe`, `list`, and `cancel`
- `remote` with actions `ensure_tool` and `request_ssh_connection`
- `terminal` with actions `open`, `list`, `inspect`, `activate`, `observe`, `command`, `human_input`, `control`, and `close`

Tool schemas hide endpoint, environment, target, broker, process, persistence, SSH, and tmux details from the model. `WorkContext` injects stable Runtime resource IDs and maps relative paths to either the local project root or the configured SSH `remote_root`.

`command` action `run` and `task` action `start` accept `target="local" | "remote"`. Use `local` for machine-local commands such as packaging or Windows PowerShell work, and `remote` for the configured SSH host.

## Capability Boundaries

Mini Harness is currently a Runtime SDK validation harness with a CLI agent loop. The default
model-facing surface is intentionally small: file operations, short commands, long tasks,
remote setup helpers, and interactive terminals.

Current boundaries:

- The default facade does not expose sync tools. The `mini_harness.sync` package is present for
  internal development and tests only.
- Local and remote operations are explicit targets. A session can use the local project and one
  configured SSH runtime, but multi-remote routing and live server switching are not implemented.
- `file` action `edit` is exact text replacement with hash guarding and diff preview. General
  patch application is not implemented.
- `command` action `run` is for clean one-shot commands and returns available output directly.
  `task` action `start` is for long-running non-interactive work. `terminal` is for stateful or
  human-interactive shell state.
- The sandbox is policy-only. It can block or ask approval for risky paths and command patterns,
  but it is not process isolation, container isolation, or gVisor/bubblewrap enforcement yet.
- CLI approvals are one-shot user decisions. Mini Harness does not yet provide a persistent
  approval UI, per-resource ACLs, or full RBAC.
- Long task and terminal summaries are best-effort context aids, not durable audit records.
- Browser UI, WebSocket UI, MCP, RAG, long-term memory, and sub-agents are outside the current
  open-source MVP.

## Tool Permissions

Tool execution goes through a capability preflight in `ToolRegistry.execute()` before the runtime operation is attempted. The first implementation defaults to an allow-all policy so existing local and SSH smoke flows keep working, but the authorization seam is now mandatory for registered runtime tools.

Configure capability allow/deny patterns in TOML:

```toml
[permissions]
allow = ["*"]
deny = [
  "file.write:*",
  "terminal.open:remote",
  "terminal.send_input:remote",
]
approve_sandbox_denials = true
approve_terminal_open = true
approve_root_escalation = true
```

Patterns can match exact capability keys such as `file.read:local`, target-wide keys such as `terminal.open:*`, or all capabilities with `*`. Deny rules win over allow rules.

Covered permission request families:

- File tools request `file.list`, `file.read`, or `file.write`.
- Task tools request `task.run`, `task.observe`, or `task.cancel`.
- Terminal tools request `terminal.open`, `terminal.observe`, `terminal.send_input`, or `terminal.close` with a local/remote target.
- Session discovery tools request `terminal.list`, `terminal.inspect`, or `terminal.activate`. Use these to discover Runtime-managed sessions; do not rely on `tmux ls` from a separate shell because tmux visibility depends on user/socket context.
- Shell commands which look like they create or overwrite files, such as `>`, `>>`, `tee`, `touch`, `mkdir`, `cp`, or `mv`, also request `file.write` for the target.
- Internal experimental sync tools request `sync.status` or `sync.push` with the remote target, but they are not exposed through the default agent facade yet.

In CLI `run` and `chat` sessions, a policy denial prompts the user for a one-shot `y/n` approval before the Runtime SDK call is attempted. If `approve_sandbox_denials` is enabled, recoverable sandbox denials such as absolute paths or blocked command patterns can also be approved once and retried with a sandbox override. If `approve_root_escalation` is enabled, root shell escalation such as `sudo -i` gets a dedicated high-risk warning before approval; disable it to block those approvals completely. If `approve_terminal_open` is enabled, opening a local or remote interactive terminal always asks for confirmation because later input can run arbitrary shell commands and may inherit session state such as cwd, env vars, login state, or root privileges.

If the user rejects the operation, or no approval handler is installed, Mini Harness returns a structured denial result and does not call the underlying Runtime SDK.

## Workspace Sandbox

Mini Harness now has a local policy-only sandbox layer before adding process-level engines such as bubblewrap or containers. The sandbox is intentionally split into:

- `WorkspacePolicy` for relative path, cwd, local root, remote root, allow patterns, and deny patterns.
- Command guard checks for clearly destructive commands, system paths, root shell escalation, package installation, and disabled network tools.
- `SandboxEngine` as a pluggable interface. The current engine is `policy-only`; future engines can wrap task and terminal argv/cwd for bubblewrap, containers, or gVisor without changing tool code.

Example configuration:

```toml
[sandbox]
profile = "workspace"   # off | workspace | strict
engine = "policy-only"  # currently only policy-only is implemented

[sandbox.remote]
root = "/srv/app"
network = "inherit"     # inherit | disabled
allow_root_shell = false
allow_system_paths = false
allow_package_install = false

[sandbox.paths]
allow = ["**"]
deny = [".env", "**/*.pem", "**/id_*", "**/.ssh/**"]
```

`profile = "off"` disables sandbox policy checks while keeping capability authorization. `profile = "strict"` keeps the same workspace boundary and additionally denies network tools by default.

## Internal Sync Module

The `mini_harness.sync` package is initialized as an independent module for workspace mirror work. It currently includes:

- `config.py`: `SyncConfig` and ignore configuration.
- `ignore.py`: conservative default ignore rules and pattern matching.
- `manifest.py`: local text-file scanner with sha256 manifests and skipped-file reporting.
- `planner.py`: push-plan calculation from local and last-pushed manifests.
- `state.py`: local JSON state storage under `.mini-harness/sync/`.
- `engine.py`: push/status engine using the existing runtime file API.

The module is currently internal/experimental and is not exposed through the default agent facade. Its internal tools are:

- `sync_status`: scans the local workspace and returns a manifest diff summary without writing remote files.
- `sync_push`: applies the local-to-remote push plan, writes the remote manifest, and updates local sync state.

Enable the first push-mode implementation with:

```toml
[sync]
enabled = true
mode = "push"
delete_remote = false
```

This block is for internal/manual experiments until the sync workflow is promoted back into the
default agent facade.

## Running

Start a broker in one terminal:

```powershell
.\.venv\Scripts\envrt.exe broker serve
```

Run the deterministic sample flow:

```powershell
.\.venv\Scripts\mini-harness.exe run --fake-model --project tests\mini_harness\sample_project "Fix the failing tests and verify they pass."
```

If the editable install has not been refreshed yet, use the module entrypoint:

```powershell
.\.venv\Scripts\python.exe -m mini_harness run --fake-model --project tests\mini_harness\sample_project "Fix the failing tests and verify they pass."
```

For a self-contained local smoke run, use the embedded broker:

```powershell
.\.venv\Scripts\mini-harness.exe run --embedded-broker --fake-model --project tests\mini_harness\sample_project "Fix the failing tests and verify they pass."
```

Start a multi-turn chat session:

```powershell
.\.venv\Scripts\mini-harness.exe chat --embedded-broker --project .
```

Use `/compact` to summarize older turns and tool results manually. Mini Harness also compacts automatically when the chat exceeds `auto_compact_turns` user turns or `auto_compact_tool_turns` retained tool results. Use `/exit` or `/quit` to end the session.

Terminal session state is reported from Runtime's session registry. `ssh_tmux` sessions can be reattached when their remote tmux session is still alive. `ssh_pty` sessions are tied to the active Runtime process and cannot be reattached after a Runtime restart; historical terminal output may still be readable, but `DISCONNECTED`, `TERMINATED`, and `LOST` sessions cannot accept new input.

For a real model:

```powershell
$env:MINI_AGENT_API_KEY = "..."
$env:MINI_AGENT_MODEL = "..."
$env:MINI_AGENT_BASE_URL = "https://api.openai.com/v1"
.\.venv\Scripts\mini-harness.exe run --embedded-broker --project tests\mini_harness\sample_project "Fix the failing tests and verify they pass."
```

Real model runs are smoke tests only. They are non-deterministic and are not part of default CI.

## Configuration

Mini Harness reads TOML config from:

1. `--config <path>`
2. `<project>/mini-harness.toml`
3. `<project>/.mini-harness.toml`
4. `./mini-harness.toml`
5. `./.mini-harness.toml`

See `mini-harness.example.toml` in the repository root for a minimal commented starting point.

CLI options and environment variables override the file. Common model overrides:

```text
MINI_AGENT_PROVIDER=openai | openai-compatible | anthropic
MINI_AGENT_MODEL=...
MINI_AGENT_BASE_URL=...
MINI_AGENT_API_KEY=...
ANTHROPIC_API_KEY=...
```

OpenAI or OpenAI-compatible example:

```toml
[agent]
max_iterations = 30
max_consecutive_tool_errors = 3
max_context_chars = 120000
recent_tool_turns = 12
auto_compact_turns = 8
auto_compact_tool_turns = 24

[model]
provider = "openai"
model = "gpt-4.1-mini"
api_key = "..."
base_url = "https://api.openai.com/v1"
timeout_seconds = 60
max_retries = 2
```

Anthropic example:

```toml
[agent]
max_iterations = 30

[model]
provider = "anthropic"
model = "claude-your-model-name"
api_key = "..."
base_url = "https://api.anthropic.com"
anthropic_version = "2023-06-01"
max_tokens = 4096
timeout_seconds = 60
max_retries = 2
```

For secrets, prefer environment variables instead of writing `api_key` in the file.

## SSH Runtime

Mini Harness can configure a remote SSH Runtime target from TOML:

```toml
[runtime]
mode = "ssh"
name = "remote-dev"

[runtime.ssh]
hostname = "127.0.0.1"
username = "envrt"
port = 2222
known_hosts_file = "manual_ssh_test/known_hosts"
identity_file = "manual_ssh_test/envrt_test_key"
use_ssh_agent = false
remote_root = ".environment-runtime/mini-harness-project"
prefer_tmux = true
allow_ssh_pty_fallback = true
```

Equivalent CLI overrides:

```powershell
.\.venv\Scripts\mini-harness.exe run `
  --embedded-broker `
  --runtime-mode ssh `
  --ssh-host 127.0.0.1 `
  --ssh-user envrt `
  --ssh-port 2222 `
  --ssh-key manual_ssh_test\envrt_test_key `
  --ssh-known-hosts manual_ssh_test\known_hosts `
  --remote-root .environment-runtime/mini-harness-project `
  "Inspect the remote project and run tests."
```

Environment variable overrides:

```text
MINI_AGENT_RUNTIME_MODE=ssh
MINI_AGENT_SSH_HOST=...
MINI_AGENT_SSH_USER=...
MINI_AGENT_SSH_PORT=22
MINI_AGENT_SSH_KEY=...
MINI_AGENT_SSH_KNOWN_HOSTS=...
MINI_AGENT_REMOTE_ROOT=...
```

Remote behavior:

- File tools use SFTP and map relative paths under `remote_root`.
- `command` action `run` uses SDK `commands.run` as a clean task, then waits up to `timeout_seconds` and returns available output, state, and exit code directly. If the command is still running after that window, use `task` action `observe` with the returned `task_id`.
- `command` action `run` and `task` action `start` can select `target="local"` or `target="remote"` when both targets are available.
- `task` action `observe` uses cursor-based SDK observation.
- Interactive work uses `terminal` actions `open`, `observe`, `command`, and `close`.
- `terminal` action `open` uses `target="current"` by default. In local mode that target is local; in SSH mode that target is remote.
- In SSH runtime mode, Mini Harness also prepares a local target so the model can use `terminal` action `open` with `target="local"` for local packaging, local PowerShell work, or other machine-local interaction.
- Terminal tool results and the work context expose the terminal target, OS, shell, and backend so the model can choose matching command syntax.
- If a terminal session has important state, use `terminal` action `command` for dependent commands. For example, after opening a root shell with `sudo -i`, install commands should run with `terminal` action `command`, not `command` action `run`.
- When a privileged or stateful terminal session is active, `command` action `run` returns a recoverable warning unless `force_clean=true` is explicitly set.
- `terminal` action `observe` supports `wait_seconds` and `idle_seconds`; for a terminal command expected to run for 10 seconds, observe with a window such as `wait_seconds=12`.
- `terminal` action `command` `data` is the exact terminal input text. Use `""` or `"\n"` to press Enter. By default, Mini Harness appends Enter when `data` does not already end with one, so `{"action": "command", "data": "id"}` executes `id` immediately. Set `input_only=true` only when typing without submitting.
- In SSH runtime mode, `terminal` action `open` with `target="remote"` already starts the process on the configured remote host. Do not pass `ssh remote` as `argv`; leave `argv` unset or use `["bash", "-l"]` for an interactive remote shell.
- SSH terminals default to `ssh_tmux`; if tmux startup fails and fallback is enabled, the SDK retries with `ssh_pty`.
- If terminal open falls back from `ssh_tmux` to `ssh_pty`, the result includes `fallback_from`, `fallback_error`, and a recommended action.
- `remote` action `ensure_tool` can check for `tmux` and optionally attempt non-interactive installation with the remote package manager.

Password SSH authentication is supported through the interactive secret prompt only. Plaintext SSH passwords are intentionally not accepted in TOML config or CLI arguments.

## API Troubleshooting

When a provider returns a non-2xx status, Mini Harness reports the HTTP status, provider request id when present, and the sanitized API error body. Examples:

```text
OpenAI-compatible API returned HTTP 400 Bad Request, request_id=req_...:
type=invalid_request_error; code=unsupported_model; message=model does not support tools
```

```text
Anthropic API returned HTTP 401 Unauthorized, request_id=req_...:
type=authentication_error; message=invalid x-api-key
```

Use `--verbose` to include tool arguments in terminal output. API keys are not printed.

## Trace

Each run writes:

```text
.mini-harness/runs/<run-id>/
  events.jsonl
  summary.json
  messages.json
```

Events and summaries are sanitized for common secret-like keys.

## Verification

Mini Harness tests:

```powershell
.\.venv\Scripts\python.exe -m ruff check mini_harness tests/mini_harness
.\.venv\Scripts\python.exe -m pytest tests/mini_harness/unit -q
.\.venv\Scripts\python.exe -m pytest tests/mini_harness/integration -q --basetemp .tmp\pytest-mini-harness
```

The integration test uses `FakeModelProvider`, a real local broker, `AgentRuntimeClient`, and a copied sample project. It verifies that the file is modified via SDK calls and pytest is run as a Runtime task.

Older status notes may mention historical pass counts from implementation-time runs. Treat those
as smoke-test evidence, not as release badges. The current tests are most useful for validating
deterministic Mini Harness behavior, the local SDK/broker path, and selected Runtime providers.
Real model calls and Docker SSH flows remain manual or opt-in checks.
