# Environment Runtime

Environment Runtime is a stateful Python runtime for local and remote development automation. It provides endpoints, environments, workspace metadata, file access, long-running tasks, interactive sessions, terminal output persistence, recovery, a local broker, and an agent-facing SDK without depending on any specific LLM or agent framework.

The current recommended integration path is:

```text
agent harness -> AgentRuntimeClient -> BrokerTransport -> local broker -> services/providers
```

The REST API and CLI are still available, but the local broker plus SDK facade is now the most complete surface for agent integration.

## Current Status

Implemented:

- Local endpoints with filesystem and process execution.
- SSH endpoints with strict known-host validation and identity-file/agent authentication.
- SFTP-backed remote filesystem access.
- Local process tasks with persisted logs.
- Local detached tasks with restart recovery.
- SSH detached persistent tasks with remote launcher upload, SFTP log tailing, remote status files, and restart recovery.
- Local interactive sessions.
- AsyncSSH PTY sessions for direct remote interactive use.
- SSH tmux-backed durable remote sessions with attach/recovery support.
- TerminalFrame persistence for replayable terminal output.
- Broker request/response commands over Windows Named Pipe or Unix socket.
- Broker stream support for runtime events and session terminal frames.
- Broker token authentication, principal metadata, and writer-lease enforcement for session writes.
- Agent-facing SDK facade using `BrokerTransport`.
- Workspace metadata, local snapshot revision, and local one-way mirror sync.
- Broker-level workspace commands and local/SFTP file commands.
- FastAPI routes and CLI commands for core runtime operations.
- Unit, integration, Docker SSH, recovery, broker, SDK, and tmux-oriented tests.

Partially implemented or intentionally minimal:

- Workspace sync is still simple. It supports local snapshot/mirror behavior, but does not yet provide robust incremental SFTP directory sync, conflict detection, ignore rules, or bidirectional reconciliation.
- SSH `proxy_jump` is represented in endpoint config but not implemented in the SSH transport.
- Non-persistent remote SSH tasks are not implemented. Use `persistent=True` for remote tasks.
- WebSocket streaming is not implemented. Broker streaming and polling are the current real-time alternatives.
- Reconnected detached remote tasks are tracked to completion, but cancellation after recovery is limited.
- `ssh_pty` sessions are tied to the live SSH channel. Use `ssh_tmux` for remote sessions that must survive runtime restart.
- The legacy HTTP SDK is intentionally small. Use `AgentRuntimeClient` for new agent integrations.

## Install

```bash
pip install -e ".[dev]"
```

Run the validation suite:

```bash
python -m ruff check environment_runtime tests
python -m mypy environment_runtime
python -m pytest
```

On Windows, the console script is the easiest CLI entrypoint:

```powershell
.\.venv\Scripts\envrt.exe --help
```

## Core Concepts

- `Endpoint`: a concrete local or SSH machine/filesystem target.
- `Environment`: a logical execution environment composed from one or more endpoints.
- `ExecutionTarget`: the executable target inside an environment.
- `Task`: a non-interactive command with persisted logs and final state.
- `Session`: an interactive process/terminal with input, output, terminal frames, and optional durable backend.
- `Workspace`: metadata describing logical roots, replicas, and bindings.
- `Broker`: a local per-project command surface used by agent harnesses and SDK transports.
- `Principal`: caller identity metadata used by broker authentication and writer leases.

## Broker Quick Start

Start a local broker:

```bash
envrt broker serve
```

In another shell:

```bash
envrt broker call broker.ping --params-json "{}"
envrt broker call broker.status --params-json "{}"
envrt broker call broker.commands --params-json "{}"
```

The broker creates a token file at:

```text
<runtime.data_dir>/broker.token
```

`BrokerClient` and `AgentRuntimeClient.from_broker(settings=...)` read this token automatically. Raw broker clients must include the token in their auth payload.

Broker streaming is available through:

- `event.subscribe`
- `session.subscribe_frames`

The stream protocol emits `start`, `item`, `heartbeat`, and `end` messages internally. The SDK yields `item` payloads by default.

## Agent SDK

Use `AgentRuntimeClient` for new integrations. It is a facade over a small transport contract, so the SDK can later support HTTP, WebSocket, or in-process transports without changing the high-level API.

```python
from environment_runtime.sdk import AgentRuntimeClient

client = AgentRuntimeClient.from_broker(principal_id="agent-a")

print(client.broker.ping())
print(client.broker.status())
print(client.broker.commands())

client.close()
```

### Ensure A Local Environment

```python
from environment_runtime.sdk import AgentRuntimeClient

client = AgentRuntimeClient.from_broker(principal_id="agent-a")

bundle = client.environments.ensure_local("local-dev", ".")
endpoint = bundle["endpoint"]
environment = bundle["environment"]
target_id = bundle["target_id"]

print(endpoint["endpoint_id"])
print(environment["environment_id"])
print(target_id)
```

The returned bundle is shaped like:

```python
{
    "endpoint": {...},
    "environment": {...},
    "target_id": "target_...",
}
```

### Read And Write Files

File operations route through the endpoint provider. Local endpoints use local filesystem access; SSH endpoints use SFTP.

```python
endpoint_id = endpoint["endpoint_id"]

client.files.write_text(endpoint_id, "notes/hello.txt", "hello runtime")
text = client.files.read_text(endpoint_id, "notes/hello.txt")
entries = client.files.list(endpoint_id, "notes")
digest = client.files.sha256(endpoint_id, "notes/hello.txt")

print(text)
print(entries)
print(digest)
```

Binary files use base64 over the broker protocol but expose bytes in the SDK:

```python
client.files.write_bytes(endpoint_id, "data.bin", b"\x00\x01")
payload = client.files.read_bytes(endpoint_id, "data.bin")
```

For local endpoints, file paths are constrained to the endpoint root.

### Run A Task

```python
task = client.tasks.start(
    environment["environment_id"],
    target_id,
    ["python", "-u", "-c", "print('hello from task')"],
)

final = client.tasks.wait(task["task_id"], timeout_seconds=30)
logs = client.tasks.logs_text(task["task_id"])

print(final["state"], final["exit_code"])
print(logs)
```

For long-running tasks:

```python
task = client.tasks.start(
    environment["environment_id"],
    target_id,
    [
        "python",
        "-u",
        "-c",
        "import time\nfor i in range(5): print(f'TICK={i}', flush=True); time.sleep(1)",
    ],
    persistent=True,
)

client.tasks.wait_for_log(task["task_id"], "TICK=2", timeout_seconds=20)
final = client.tasks.wait(task["task_id"], timeout_seconds=60)
```

Use `persistent=True` for remote SSH tasks. Remote non-persistent tasks are intentionally rejected for now.

### Interactive Sessions

Create a session:

```python
session = client.sessions.create(
    environment["environment_id"],
    target_id,
    ["python", "-i"],
    backend="local_pty",
)
```

Before writing, acquire a writer lease:

```python
client.sessions.acquire_lease(session["session_id"], force=True)
client.sessions.write(session["session_id"], "print('hello')\n")
```

Read output:

```python
tail = client.sessions.tail_until(session["session_id"], "hello", timeout_seconds=10)
print(tail["text"])
```

Stream terminal frames:

```python
for frame in client.sessions.stream_frames(session["session_id"], after_seq=-1, max_items=10):
    print(frame["kind"], frame["stream"], frame["data"])
```

Remote interactive backend guidance:

- `ssh_pty`: direct SSH PTY, useful for live interaction, not durable across runtime restart.
- `ssh_tmux`: remote tmux-backed session, recommended when the session must survive local runtime/broker restart.

The remote host must have `tmux` installed for `ssh_tmux`.

### SSH Environment

Prepare a strict known-hosts file first. Then:

```python
bundle = client.environments.ensure_ssh(
    name="remote-dev",
    hostname="127.0.0.1",
    port=2222,
    username="envrt",
    known_hosts_file="manual_ssh_test/known_hosts",
    identity_file="manual_ssh_test/envrt_test_key",
    use_ssh_agent=False,
)

endpoint_id = bundle["endpoint"]["endpoint_id"]
environment_id = bundle["environment"]["environment_id"]
target_id = bundle["target_id"]
```

Remote file access:

```python
client.files.write_text(endpoint_id, ".environment-runtime/probe.txt", "remote ok")
print(client.files.read_text(endpoint_id, ".environment-runtime/probe.txt"))
```

Remote persistent task:

```python
task = client.tasks.start(
    environment_id,
    target_id,
    ["bash", "-lc", "for i in 0 1 2 3; do echo REMOTE_TICK=$i; sleep 1; done"],
    persistent=True,
)

client.tasks.wait_for_log(task["task_id"], "REMOTE_TICK=2", timeout_seconds=20)
final = client.tasks.wait(task["task_id"], timeout_seconds=60)
print(final["state"], final["exit_code"])
```

Remote durable session:

```python
session = client.sessions.create(
    environment_id,
    target_id,
    ["bash", "-l"],
    backend="ssh_tmux",
)

client.sessions.acquire_lease(session["session_id"], force=True)
client.sessions.write(session["session_id"], "echo hello-from-tmux\n")
print(client.sessions.tail_until(session["session_id"], "hello-from-tmux")["text"])
```

## Broker Command Surface

Use this to discover the current canonical command set:

```python
commands = client.broker.commands()
for command in commands:
    print(command["method"], command["params_schema"])
```

Current groups:

- `broker.*`: status, command discovery, shutdown, event subscription.
- `endpoint.*`: local and SSH endpoint creation, listing, health checks.
- `env.*`: create/get/list and `ensure_local` / `ensure_ssh`.
- `workspace.*`: create/get/list, roots, replicas, bindings, revisions, sync.
- `file.*`: exists/stat/list/mkdir/remove/sha256/read/write for text and bytes.
- `task.*`: start/get/list/logs/cancel.
- `session.*`: create/get/list/write/resize/terminate/tail/frames/stream frames.

The broker validates command parameters with Pydantic models before dispatching to services.

## CLI Surface

The CLI remains useful for manual testing and local operation:

- `envrt endpoint add-local`
- `envrt endpoint add-ssh`
- `envrt endpoint list`
- `envrt endpoint health`
- `envrt env create`
- `envrt env list`
- `envrt env inspect`
- `envrt env reconcile`
- `envrt workspace create`
- `envrt workspace add-root`
- `envrt workspace add-replica`
- `envrt workspace bind`
- `envrt workspace revision`
- `envrt workspace sync`
- `envrt workspace status`
- `envrt task run`
- `envrt task start`
- `envrt task list`
- `envrt task inspect`
- `envrt task logs`
- `envrt task cancel`
- `envrt session create`
- `envrt session resize`
- `envrt session list`
- `envrt session inspect`
- `envrt session attach`
- `envrt session terminate`
- `envrt broker address`
- `envrt broker serve`
- `envrt broker call`
- `envrt broker shutdown`
- `envrt artifact list`
- `envrt artifact download`
- `envrt serve`

## REST API And Legacy SDK

The FastAPI app is still available:

```bash
envrt serve
```

The legacy SDK remains exported:

```python
from environment_runtime.sdk import AsyncEnvironmentRuntimeClient, EnvironmentRuntimeClient
```

For new agent harness work, prefer:

```python
from environment_runtime.sdk import AgentRuntimeClient
```

## Mini Harness Agent

This repository now includes `mini_harness`, a small SDK-consuming coding-agent harness for validating the recommended integration path:

```text
Mini Harness -> AgentRuntimeClient -> BrokerTransport -> local broker -> services/providers
```

Run a deterministic local sample:

```powershell
.\.venv\Scripts\mini-harness.exe run --embedded-broker --fake-model --project tests\mini_harness\sample_project "Find why the tests fail, fix the code, and verify all tests pass."
```

Without refreshing console scripts, the same command is available as:

```powershell
.\.venv\Scripts\python.exe -m mini_harness run --embedded-broker --fake-model --project tests\mini_harness\sample_project "Find why the tests fail, fix the code, and verify all tests pass."
```

See `docs/mini-harness.md` and `MINI_HARNESS_STATUS.md` for architecture, commands, verification status, and current limitations.

Mini Harness supports TOML configuration for OpenAI-compatible APIs and Anthropic:

```toml
[model]
provider = "anthropic" # or "openai" / "openai-compatible"
model = "claude-your-model-name"
api_key = "..."
```

Use `--config path\to\mini-harness.toml` or place `mini-harness.toml` in the project root. See
`mini-harness.example.toml` for a minimal commented starting point.

## Recovery Behavior

On runtime startup, recovery reconciles persisted live resources:

- `local_detached` tasks can be recovered from local log/status files.
- `ssh_detached` tasks can be recovered from remote SFTP log/status files.
- `ssh_tmux` sessions can be reattached if the remote tmux session still exists.
- `local_pty` and `ssh_pty` sessions cannot be reattached after runtime restart and are marked disconnected.

Normal broker/runtime shutdown detaches durable tmux sessions rather than killing them.

## Testing Remote SSH With Docker

The repository has been manually tested with a Docker SSH container using files under `manual_ssh_test/`.

Run regular tests:

```bash
python -m pytest
```

Run the optional real SSH SDK remote-task test:

```powershell
$env:ENVRT_TEST_SSH_DOCKER = "1"
.\.venv\Scripts\python -m pytest tests\integration\test_agent_sdk_remote_task.py -q
```

Optional environment variables:

- `ENVRT_TEST_SSH_HOST`, default `127.0.0.1`
- `ENVRT_TEST_SSH_PORT`, default `2222`
- `ENVRT_TEST_SSH_USER`, default `envrt`
- `ENVRT_TEST_SSH_KEY`, default `manual_ssh_test/envrt_test_key`
- `ENVRT_TEST_SSH_KNOWN_HOSTS`, default `manual_ssh_test/known_hosts`

The optional test verifies:

- SDK `ensure_ssh`
- SFTP file write/read
- remote persistent task startup
- log polling through SDK
- broker/runtime restart
- detached SSH task recovery
- final task state and exit code

## Architecture

The runtime is organized into four main layers:

- `environment_runtime/core`: resource models, IDs, events, domain errors, and shared concepts.
- `environment_runtime/services`: orchestration, validation, recovery, and state transitions.
- `environment_runtime/providers`: local/SSH transports, filesystems, execution backends, session backends, and sync adapters.
- `environment_runtime/api`, `environment_runtime/cli`, `environment_runtime/broker`, `environment_runtime/sdk`: user-facing adapters.

Important design rule:

```text
Business behavior belongs in services/providers.
Broker/API/CLI/SDK are adapters over those services.
```

## Roadmap

High-value next steps:

- Use Mini Harness to identify the most painful SDK/workspace gaps.
- Improve workspace sync with SFTP directory upload/download, include/exclude rules, incremental revisions, and conflict strategy.
- Implement SSH `proxy_jump`.
- Add task log streaming as a first-class broker stream.
- Add WebSocket streaming on top of terminal frames/events for UI integrations.
- Improve cancellation for recovered remote detached tasks.
- Add resource ownership/scope enforcement beyond the current broker token/principal/writer-lease model.
