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

The first version exposes only:

- `list_files`
- `read_file`
- `write_file`
- `run_command`
- `observe_task`
- `cancel_task`

Tool schemas hide endpoint, environment, target, broker, process, and persistence details from the model. `WorkContext` injects the stable Runtime resource IDs.

## Running

Start a broker in one terminal:

```powershell
.\.venv\Scripts\envrt.exe broker serve
```

Run the deterministic sample flow:

```powershell
.\.venv\Scripts\mini-harness.exe run --fake-model --project tests\mini_harness\sample_project "检查测试失败的原因，修改代码并确保所有测试通过。"
```

If the editable install has not been refreshed yet, use the module entrypoint:

```powershell
.\.venv\Scripts\python.exe -m mini_harness run --fake-model --project tests\mini_harness\sample_project "检查测试失败的原因，修改代码并确保所有测试通过。"
```

For a self-contained local smoke run, use the embedded broker:

```powershell
.\.venv\Scripts\mini-harness.exe run --embedded-broker --fake-model --project tests\mini_harness\sample_project "检查测试失败的原因，修改代码并确保所有测试通过。"
```

For a real model:

```powershell
$env:MINI_AGENT_API_KEY = "..."
$env:MINI_AGENT_MODEL = "..."
$env:MINI_AGENT_BASE_URL = "https://api.openai.com/v1"
.\.venv\Scripts\mini-harness.exe run --embedded-broker --project tests\mini_harness\sample_project "修复失败测试并验证通过"
```

Real model runs are smoke tests only. They are non-deterministic and are not part of default CI.

## Configuration

Mini Harness reads TOML config from:

1. `--config <path>`
2. `<project>/mini-harness.toml`
3. `<project>/.mini-harness.toml`
4. `./mini-harness.toml`
5. `./.mini-harness.toml`

CLI options and environment variables override the file. Common overrides:

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
