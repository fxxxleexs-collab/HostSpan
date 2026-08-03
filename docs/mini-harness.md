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

## Tools

The current version exposes:

- `list_files`
- `read_file`
- `write_file`
- `run_command`
- `observe_task`
- `cancel_task`
- `ensure_remote_tool`
- `open_terminal`
- `observe_terminal`
- `send_terminal_input`
- `run_in_session`
- `close_terminal`

Tool schemas hide endpoint, environment, target, broker, process, persistence, SSH, and tmux details from the model. `WorkContext` injects stable Runtime resource IDs and maps relative paths to either the local project root or the configured SSH `remote_root`.

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
- `run_command` uses SDK `commands.run` as a clean task. It does not inherit terminal state such as root shell, `cd`, exported env vars, activated venv, nested login, or tmux shell state.
- `observe_task` uses cursor-based SDK observation.
- Interactive work uses `open_terminal`, `observe_terminal`, `send_terminal_input`, and `close_terminal`.
- If a terminal session has important state, use `run_in_session` for dependent commands. For example, after opening a root shell with `sudo -i`, install commands should run with `run_in_session`, not `run_command`.
- When a privileged or stateful terminal session is active, `run_command` returns a recoverable warning unless `force_clean=true` is explicitly set.
- `observe_terminal` supports `wait_seconds` and `idle_seconds`; for a terminal command expected to run for 10 seconds, observe with a window such as `wait_seconds=12`.
- `send_terminal_input.data` is the exact terminal input bytes. Use `""` or `"\n"` to press Enter, and include a trailing `"\n"` to submit a shell command.
- `send_terminal_input.run_directly=true` appends Enter when `data` does not already end with one, so `{"data": "id", "run_directly": true}` executes `id` immediately.
- In SSH runtime mode, `open_terminal` already starts the process on the configured remote host. Do not pass `ssh remote` as `argv`; use `["bash", "-l"]` for an interactive remote shell.
- SSH terminals default to `ssh_tmux`; if tmux startup fails and fallback is enabled, the SDK retries with `ssh_pty`.
- If `open_terminal` falls back from `ssh_tmux` to `ssh_pty`, the result includes `fallback_from`, `fallback_error`, and a recommended action.
- `ensure_remote_tool` can check for `tmux` and optionally attempt non-interactive installation with the remote package manager.

Password SSH authentication is not wired through Runtime endpoint creation yet. Use an identity file or SSH agent for this first version.

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
