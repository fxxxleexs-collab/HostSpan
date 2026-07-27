# API And Adapter Surfaces

Environment Runtime currently exposes three practical adapter surfaces:

- Broker: the most complete agent-facing command surface.
- FastAPI: HTTP routes for manual and service-style use.
- CLI: convenient manual testing and operational commands.

For new agent harness work, prefer `AgentRuntimeClient` over hand-written broker protocol calls.

## Broker Commands

Discover commands at runtime:

```python
commands = client.broker.commands()
```

Canonical command groups:

- `broker.*`: `ping`, `status`, `commands`, `shutdown`.
- `endpoint.*`: `add_local`, `add_ssh`, `list`, `health`.
- `env.*`: `create`, `ensure_local`, `ensure_ssh`, `get`, `list`.
- `workspace.*`: `create`, `get`, `list`, `add_root`, `add_replica`, `bind`, `revision`, `sync`.
- `file.*`: `exists`, `stat`, `list`, `mkdir`, `remove`, `sha256`, `read_text`, `write_text`, `read_bytes`, `write_bytes`.
- `task.*`: `start`, `get`, `list`, `logs`, `cancel`.
- `session.*`: `create`, `get`, `list`, `acquire_lease`, `write`, `resize`, `terminate`, `frames`, `tail`.

Broker stream commands:

- `event.subscribe`
- `session.subscribe_frames`

Broker command parameters are validated with Pydantic schemas in `environment_runtime/broker/schemas.py`.

## FastAPI Routes

Implemented HTTP routes:

- `POST /endpoints`
- `POST /endpoints/ssh`
- `GET /endpoints`
- `GET /endpoints/{endpoint_id}/health`
- `POST /environments`
- `GET /environments`
- `GET /environments/{environment_id}`
- `POST /environments/{environment_id}/reconcile`
- `POST /workspaces`
- `GET /workspaces`
- `POST /workspaces/{workspace_id}/roots`
- `POST /workspaces/{workspace_id}/replicas`
- `POST /workspaces/{workspace_id}/bindings`
- `POST /workspaces/{workspace_id}/revisions/{replica_id}`
- `POST /workspaces/{workspace_id}/sync/{binding_id}`
- `POST /tasks`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/logs`
- `POST /tasks/{task_id}/cancel`
- `POST /sessions`
- `GET /sessions`
- `GET /sessions/{session_id}`
- `GET /sessions/{session_id}/terminal/frames`
- `GET /sessions/{session_id}/terminal/tail`
- `POST /sessions/{session_id}/write`
- `POST /sessions/{session_id}/resize`
- `POST /sessions/{session_id}/terminate`
- `POST /interactions/requests`
- `POST /interactions/requests/{request_id}/submit`
- `POST /interactions/leases`
- `POST /artifacts`
- `GET /artifacts`
- `POST /artifacts/{artifact_id}/download`

Not implemented in HTTP yet:

- WebSocket event streaming.
- WebSocket terminal streaming.
- Remote port forwarding endpoints.
- Broker-equivalent `file.*` routes.

## CLI

The CLI includes endpoint, environment, workspace, task, session, artifact, broker, and API server commands. Use it for manual testing and local operation.

Examples:

```bash
envrt broker serve
envrt broker call broker.status --params-json "{}"
envrt endpoint add-local demo .
envrt env create local-env --endpoint-ids <endpoint_id>
envrt task start <environment_id> <target_id> -- python -u script.py
```

On Windows:

```powershell
.\.venv\Scripts\envrt.exe broker serve
```
