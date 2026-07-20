# Implementation Status

## Completed

- Python package scaffold with editable install support
- Core resource models for endpoints, environments, workspaces, tasks, sessions, interactions, artifacts, and events
- SQLite persistence with async SQLAlchemy
- In-process event bus and persisted event store
- Local filesystem provider
- Local subprocess execution provider with log capture
- Local interactive session provider with stdin routing
- Writer lease service and input request flow
- Workspace revisions and one-way snapshot sync
- FastAPI application and route set
- CLI entrypoint `envrt`
- Minimal HTTP SDK
- Unit and local integration tests
- Lint and typecheck configuration

## Verification Status

Last verified in this workspace with:

```bash
python -m ruff check environment_runtime tests
python -m mypy environment_runtime
python -m pytest
```

Result:

- `ruff`: passed
- `mypy`: passed
- `pytest`: 6 tests passed

## Not Yet Implemented

- SSH transport provider
- host key verification workflow
- SFTP filesystem provider
- rsync-backed synchronization provider
- tmux session provider
- WebSocket event and terminal streaming
- SSH port forwarding
- restart reconciliation of active external resources
- Docker-based remote integration, recovery, and security suites
- Alembic migration workflow
- GitHub Actions artifact capture for remote integration logs

## Intentional Deviations

- The repository currently delivers a local-first MVP rather than the full remote runtime described in the brief.
- Remote resource abstractions are represented in the domain and service architecture, but only the local provider path is implemented.
- Session attach is implemented as writer-lease takeover rather than a full terminal passthrough UI.
- Events are persisted and queryable, but live WebSocket subscriptions are not implemented yet.

## Next Recommended Steps

1. Implement `ssh` transport and `sftp` filesystem providers behind the existing registry.
2. Add a tmux-backed session provider and update session/task services to support durable remote backends.
3. Add reconciliation for running tasks and sessions after process restart.
4. Add Docker-driven contract tests for remote providers.
5. Add WebSocket streams for events and terminal output.
