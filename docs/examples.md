# Examples

## Start A Broker

```bash
envrt broker serve
```

In another shell:

```bash
envrt broker call broker.status --params-json "{}"
```

## Agent SDK Local Flow

```python
from environment_runtime.sdk import AgentRuntimeClient

client = AgentRuntimeClient.from_broker(principal_id="agent-a")

bundle = client.environments.ensure_local("local-dev", ".")
endpoint_id = bundle["endpoint"]["endpoint_id"]
environment_id = bundle["environment"]["environment_id"]
target_id = bundle["target_id"]

client.files.write_text(endpoint_id, "hello.txt", "hello runtime")
print(client.files.read_text(endpoint_id, "hello.txt"))

task = client.tasks.start(
    environment_id,
    target_id,
    ["python", "-u", "-c", "print('TASK_OK')"],
)
print(client.tasks.wait(task["task_id"]))
print(client.tasks.logs_text(task["task_id"]))
```

## Agent SDK Long Task

```python
task = client.tasks.start(
    environment_id,
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
print(final["state"], final["exit_code"])
```

## Agent SDK Interactive Session

```python
session = client.sessions.create(
    environment_id,
    target_id,
    ["python", "-i"],
    backend="local_pty",
)

client.sessions.acquire_lease(session["session_id"], force=True)
client.sessions.write(session["session_id"], "print('SESSION_OK')\n")

tail = client.sessions.tail_until(session["session_id"], "SESSION_OK", timeout_seconds=10)
print(tail["text"])
```

## SSH Remote Task

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

task = client.tasks.start(
    bundle["environment"]["environment_id"],
    bundle["target_id"],
    ["bash", "-lc", "for i in 0 1 2 3; do echo REMOTE_TICK=$i; sleep 1; done"],
    persistent=True,
)

client.tasks.wait_for_log(task["task_id"], "REMOTE_TICK=2", timeout_seconds=20)
print(client.tasks.wait(task["task_id"], timeout_seconds=60))
```

## SSH Tmux Session

```python
session = client.sessions.create(
    bundle["environment"]["environment_id"],
    bundle["target_id"],
    ["bash", "-l"],
    backend="ssh_tmux",
)

client.sessions.acquire_lease(session["session_id"], force=True)
client.sessions.write(session["session_id"], "echo TMUX_OK\n")
print(client.sessions.tail_until(session["session_id"], "TMUX_OK")["text"])
```

## Workspace Metadata And Local Sync

```python
workspace = client.workspaces.create("workspace")
workspace = client.workspaces.add_root(workspace["workspace_id"], ".")

source = client.workspaces.add_replica(
    workspace["workspace_id"],
    endpoint_id,
    ".",
)

target = client.workspaces.add_replica(
    workspace["workspace_id"],
    endpoint_id,
    "copy",
)

workspace = client.workspaces.bind(
    workspace["workspace_id"],
    source["replicas"][0]["replica_id"],
    target["replicas"][1]["replica_id"],
)
```

Workspace sync is currently simple and local-first. Use direct `client.files.*` operations for remote file read/write until deeper workspace sync is implemented.
