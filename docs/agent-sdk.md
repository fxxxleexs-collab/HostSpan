# Agent SDK

`AgentRuntimeClient` is the recommended SDK for new agent harness work.

It is intentionally a facade over a transport protocol:

```text
AgentRuntimeClient -> RuntimeTransport -> BrokerTransport -> local broker
```

Only `BrokerTransport` is implemented today, but the facade is designed so future HTTP, WebSocket, or in-process transports can be added without changing harness code.

## Create A Client

```python
from environment_runtime.sdk import AgentRuntimeClient, RuntimePolicy

client = AgentRuntimeClient.from_broker(
    principal_id="agent-a",
    policy=RuntimePolicy(
        remote_command_persistent=True,
        remote_terminal_backend="ssh_tmux",
        allow_ssh_pty_fallback=True,
    ),
)
```

With explicit settings:

```python
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient

settings = RuntimeSettings(
    database={"url": "sqlite+aiosqlite:///./runtime.db"},
    runtime={"data_dir": "./runtime-data"},
)

client = AgentRuntimeClient.from_broker(settings=settings, principal_id="agent-a")
```

## Namespaces

The facade exposes:

- `client.broker`
- `client.endpoints`
- `client.environments`
- `client.workspaces`
- `client.files`
- `client.tasks`
- `client.sessions`
- `client.commands`
- `client.terminals`

## Environment Helpers

Local:

```python
bundle = client.environments.ensure_local("local-dev", ".")
endpoint_id = bundle["endpoint"]["endpoint_id"]
environment_id = bundle["environment"]["environment_id"]
target_id = bundle["target_id"]
```

SSH:

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
```

## File Tools

These are suitable building blocks for agent `read` and `write` tools.

```python
client.files.write_text(endpoint_id, "notes.txt", "hello")
text = client.files.read_text(endpoint_id, "notes.txt")

client.files.write_bytes(endpoint_id, "data.bin", b"\x00\x01")
payload = client.files.read_bytes(endpoint_id, "data.bin")
```

Other helpers:

```python
client.files.exists(endpoint_id, "notes.txt")
client.files.stat(endpoint_id, "notes.txt")
client.files.list(endpoint_id, ".", recursive=False)
client.files.mkdir(endpoint_id, "build")
client.files.sha256(endpoint_id, "notes.txt")
client.files.remove(endpoint_id, "notes.txt")
```

Local endpoint paths are constrained to the endpoint root. SSH endpoint file operations use SFTP.

## Task Tools

These are suitable building blocks for agent `bash`, `set_task`, `stop_task`, and `observe` tools.

```python
task = client.tasks.start(
    environment_id,
    target_id,
    ["python", "-u", "-c", "print('hello')"],
)

final = client.tasks.wait(task["task_id"], timeout_seconds=30)
logs = client.tasks.logs_text(task["task_id"])
```

Long-running task:

```python
task = client.tasks.start(
    environment_id,
    target_id,
    ["bash", "-lc", "for i in 0 1 2 3; do echo TICK=$i; sleep 1; done"],
    persistent=True,
)

client.tasks.wait_for_log(task["task_id"], "TICK=2", timeout_seconds=20)
final = client.tasks.wait(task["task_id"], timeout_seconds=60)
```

For low-level SSH tasks, use `persistent=True`. The higher-level `client.commands.run`
helper does this automatically for SSH targets.

Incremental observation:

```python
cursor = 0
task = client.commands.run(
    environment_id,
    target_id,
    ["bash", "-lc", "for i in 0 1 2; do echo TICK=$i; sleep 1; done"],
)

while True:
    update = client.tasks.observe(task["task_id"], cursor=cursor, wait_seconds=1)
    cursor = update["cursor"]
    if update["text"]:
        print(update["text"], end="")
    if update["is_terminal"]:
        break
```

## Session Tools

Create a session:

```python
session = client.sessions.create(
    environment_id,
    target_id,
    ["bash", "-l"],
    backend="ssh_tmux",
)
```

Write safely:

```python
client.sessions.write(session["session_id"], "echo hello\n")
```

`sessions.write` renews the writer lease before writing by default. If you
already acquired a lease and want strict manual control, pass
`renew_lease=False`.

Observe:

```python
tail = client.sessions.tail_until(session["session_id"], "hello", timeout_seconds=10)
frames = client.sessions.frames(session["session_id"], after_seq=-1)
```

Stream frames:

```python
for frame in client.sessions.stream_frames(session["session_id"], after_seq=-1):
    print(frame["kind"], frame["stream"], frame["data"])
```

## Semantic Agent Helpers

`client.commands` and `client.terminals` are policy-based wrappers over the lower-level
task and session namespaces.

Run a non-interactive command:

```python
task = client.commands.run(
    environment_id,
    target_id,
    ["pytest", "-q"],
    cwd=".",
)
```

Default command policy:

- Local targets start normal tasks unless the policy says otherwise.
- SSH targets start persistent detached tasks so logs, status, and recovery are available.

Open an interactive terminal:

```python
opened = client.terminals.open(
    environment_id,
    target_id,
    ["bash", "-l"],
)

client.terminals.write(opened["session_id"], "echo hello\n")
terminal = client.terminals.observe(opened["session_id"], after_seq=0)
```

Default terminal policy:

- Local targets use `local_pty`.
- SSH targets use `ssh_tmux`.
- If `ssh_tmux` fails and `allow_ssh_pty_fallback=True`, the SDK retries with `ssh_pty`.
- `terminals.open` acquires a writer lease by default.
- `sessions.write` and `terminals.write` renew the writer lease before writing by default, using
  `RuntimePolicy.writer_lease_ttl_seconds`. Pass `renew_lease=False` only when
  you intentionally want to manage leases through `client.sessions` yourself.

## Suggested Harness Mapping

- `read`: `client.files.read_text` or `client.files.read_bytes`
- `write`: `client.files.write_text` or `client.files.write_bytes`
- `bash`: `client.commands.run`
- `set_task`: harness-side record of active `task_id`, `session_id`, environment, and target
- `stop_task`: `client.tasks.cancel`
- `observe`: `client.tasks.observe`, `client.terminals.observe`, or `client.terminals.stream`
- `open_terminal`: `client.terminals.open`
- `send_input`: `client.terminals.write`

## Current SDK Limits

- The facade returns dictionaries, not generated typed resource classes.
- The facade is synchronous. The legacy HTTP SDK has async methods, but the new agent facade currently targets local broker usage.
- No automatic broker startup helper yet.
- No high-level workspace project sync helper yet.
- No task log stream helper yet; `tasks.observe` is cursor-based polling.
