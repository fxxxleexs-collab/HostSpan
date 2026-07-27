# Broker

The local broker is the preferred command surface for agent harnesses. It keeps one runtime context alive and exposes request/response commands plus streaming subscriptions over a local IPC address.

## Address

Default address selection:

- Windows: Named Pipe derived from the current working directory.
- Unix-like systems: Unix socket under `runtime.data_dir`.

CLI:

```bash
envrt broker address
envrt broker serve
```

## Authentication

On startup, the broker creates a random token at:

```text
<runtime.data_dir>/broker.token
```

`BrokerClient(address, settings=settings)` and `AgentRuntimeClient.from_broker(settings=settings)` read the token automatically.

Raw clients must include:

```json
{
  "auth": {
    "token": "...",
    "principal_id": "agent-a",
    "principal_type": "agent",
    "scope_id": "default"
  }
}
```

The token model is intentionally local-first. It prevents accidental or unrelated local processes from controlling the broker. It is not a full multi-user RBAC model.

## Principal

A principal identifies the caller. Current fields:

- `principal_id`
- `principal_type`
- `scope_id`

The broker passes the principal into command handling. `session.write` defaults to this principal ID when validating writer leases.

## Writer Lease

Before writing to a session, acquire a writer lease:

```python
client.sessions.acquire_lease(session_id, force=True)
client.sessions.write(session_id, "echo hello\n")
```

If another principal holds a valid lease, the write is rejected.

## Request/Response Commands

Discover the current command set:

```python
for command in client.broker.commands():
    print(command["method"], command["params_schema"])
```

Important command groups:

- `env.ensure_local`
- `env.ensure_ssh`
- `file.read_text`
- `file.write_text`
- `task.start`
- `task.logs`
- `session.create`
- `session.write`
- `session.frames`
- `workspace.sync`

Command parameters are validated by Pydantic models before dispatch.

## Streams

Broker streams keep the connection open and send framed messages:

- `start`
- `item`
- `heartbeat`
- `end`

The SDK yields `item` payloads by default.

Runtime events:

```python
for event in client.broker.events(resource_type="task", max_items=10):
    print(event["event_type"], event["resource_id"])
```

Terminal frames:

```python
for frame in client.sessions.stream_frames(session_id, after_seq=-1):
    print(frame["seq"], frame["kind"], frame["data"])
```

## Canonical Command Design

The broker command names are the current canonical method set for SDK and agent harnesses. API routes and CLI commands may have different names, but they should remain adapters over the same service/provider behavior.

Avoid putting business logic in broker commands. Broker responsibilities are:

- authentication
- principal context
- parameter validation
- command dispatch
- small adapter mapping

Business behavior belongs in services and providers.

## Known Limits

- No task log stream command yet; use `task.logs` polling or general `event.subscribe`.
- No full resource-level owner/scope authorization yet.
- No remote broker mode yet.
- No WebSocket transport yet.
