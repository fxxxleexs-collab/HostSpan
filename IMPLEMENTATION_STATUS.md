# Implementation Status

## Completed

- Python package scaffold with editable install support.
- Core resource models for endpoints, environments, workspaces, tasks, sessions, interactions, artifacts, terminal frames, writer leases, and events.
- SQLite persistence with async SQLAlchemy.
- Persisted resource records, events, task logs, and TerminalFrames.
- In-memory event bus with persisted event publishing.
- Local filesystem provider.
- SFTP filesystem provider.
- SSH transport provider with strict known-host validation.
- Local subprocess execution provider.
- Local detached execution provider with recovery.
- SSH detached persistent execution provider with launcher upload, log tailing, status recovery, and restart reattach.
- Local interactive session provider.
- AsyncSSH PTY session provider.
- SSH tmux-backed durable session provider with restart reattach.
- Session backend contract.
- TerminalFrame persistence and replay/tail APIs.
- Workspace metadata, local revisions, and one-way local snapshot sync.
- FastAPI route set for endpoints, environments, workspaces, tasks, sessions, interactions, and artifacts.
- CLI entrypoint `envrt`.
- Local broker over Windows Named Pipe or Unix socket.
- Broker request/response command surface.
- Broker stream support for runtime events and session TerminalFrames.
- Broker token authentication and principal metadata.
- Writer-lease enforcement for broker session writes.
- Pydantic validation for broker command parameters.
- Agent-facing SDK facade with `BrokerTransport`.
- Legacy HTTP SDK retained for compatibility.
- Unit and integration tests for local flows, broker, SDK, recovery, SSH providers, SFTP, tmux, and terminal frames.
- Optional Docker SSH SDK remote-task recovery test.

## Verification Status

Last verified in this workspace with:

```bash
python -m ruff check environment_runtime tests
python -m mypy environment_runtime
python -m pytest
```

Recent result:

- `ruff`: passed
- `mypy`: passed
- `pytest`: passed with the optional real SSH Docker test skipped by default

The optional real SSH SDK test can be enabled with:

```powershell
$env:ENVRT_TEST_SSH_DOCKER = "1"
.\.venv\Scripts\python -m pytest tests\integration\test_agent_sdk_remote_task.py -q
```

## Partially Implemented

- Workspace sync is local-first and simple. It does not yet support robust SFTP directory sync, ignore rules, bidirectional sync, or conflict detection.
- Broker security includes token auth, principal metadata, and writer leases, but not full per-resource RBAC.
- Recovered detached remote tasks are tracked to completion, but cancellation after recovery is limited.
- REST API is useful but does not expose every broker command, especially `file.*`.
- Legacy HTTP SDK is intentionally minimal. Use `AgentRuntimeClient` for new integration work.

## Not Yet Implemented

- SSH `proxy_jump`.
- Non-persistent SSH task execution.
- WebSocket event and terminal streaming.
- Task log streaming as a first-class broker stream.
- SSH port forwarding.
- Full remote workspace synchronization.
- Full resource owner/scope authorization.
- Alembic migration workflow.

## Next Recommended Steps

1. Build the first real agent harness over `AgentRuntimeClient`: `read`, `write`, `bash`, `set_task`, `stop_task`, and `observe`.
2. Use the harness to identify the most painful workspace gaps.
3. Add SFTP directory upload/download and include/exclude handling.
4. Add task log streaming to the broker.
5. Implement SSH `proxy_jump`.
6. Add stronger resource ownership/scope enforcement.
7. Add WebSocket adapters when UI integration needs them.
