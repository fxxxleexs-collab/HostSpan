# Agent SDK

`AgentRuntimeClient` is the recommended SDK for new agent harness work.

It is intentionally a facade over a transport protocol:

```text
AgentRuntimeClient -> RuntimeTransport -> BrokerTransport -> local broker
```

Only `BrokerTransport` is implemented today, but the facade is designed so future HTTP, WebSocket, or in-process transports can be added without changing harness code.

## Create A Client

```python
from environment_runtime.sdk import AgentRuntimeClient

client = AgentRuntimeClient.from_broker(principal_id="agent-a")
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

For SSH tasks, use `persistent=True`.

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
client.sessions.acquire_lease(session["session_id"], force=True)
client.sessions.write(session["session_id"], "echo hello\n")
```

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

## Suggested Harness Mapping

- `read`: `client.files.read_text` or `client.files.read_bytes`
- `write`: `client.files.write_text` or `client.files.write_bytes`
- `bash`: `client.tasks.start`, usually `persistent=True` for remote targets
- `set_task`: harness-side record of active `task_id`, `session_id`, environment, and target
- `stop_task`: `client.tasks.cancel`
- `observe`: `client.tasks.logs_text`, `client.tasks.wait_for_log`, `client.sessions.tail`, or `client.sessions.stream_frames`

## Current SDK Limits

- The facade returns dictionaries, not generated typed resource classes.
- The facade is synchronous. The legacy HTTP SDK has async methods, but the new agent facade currently targets local broker usage.
- No automatic broker startup helper yet.
- No high-level workspace project sync helper yet.
- No task log stream helper yet.
