# Architecture

Environment Runtime is organized around a stable domain/service core with multiple adapters on top.

```text
Agent harness / SDK / Broker / CLI / FastAPI
        |
        v
services: orchestration, state transitions, recovery, security policy
        |
        v
providers: local, SSH, SFTP, detached execution, PTY, tmux, sync
        |
        v
core models + persistence + events
```

The current preferred agent integration path is:

```text
AgentRuntimeClient -> BrokerTransport -> LocalBrokerServer -> RuntimeCommandHandler -> services/providers
```

## Layers

- `environment_runtime/core`: Pydantic resource models, IDs, domain events, capabilities, command specs, and domain errors.
- `environment_runtime/persistence`: async SQLAlchemy repositories for resources, events, task logs, and terminal frames.
- `environment_runtime/events`: in-memory event bus with persisted event-store integration.
- `environment_runtime/providers`: concrete adapters for local and remote transports, filesystems, execution, sessions, and sync.
- `environment_runtime/services`: application orchestration and lifecycle transitions.
- `environment_runtime/broker`: local process broker over Windows Named Pipe or Unix socket.
- `environment_runtime/sdk`: facade and transport abstractions for agent-facing integrations.
- `environment_runtime/api` and `environment_runtime/cli`: REST and command-line adapters.

## Provider Families

Transport providers:

- `local`
- `ssh`

Filesystem providers:

- `local`
- `sftp`

Execution providers:

- `local_process`
- `local_detached`
- `ssh_detached`

Session providers:

- `local_pty`
- `ssh_pty`
- `ssh_tmux`

Sync providers:

- `snapshot`

## Runtime Flow

1. An endpoint describes where work can happen: local machine or SSH target.
2. An environment references endpoints and exposes execution targets.
3. A task starts through `TaskService` and a matching execution provider.
4. A session starts through `SessionService` and a matching session provider.
5. File operations route through broker `file.*` commands to local or SFTP providers.
6. Task logs, terminal frames, events, and resource state are persisted.
7. Recovery runs on startup and reconciles durable resources.

## Broker As Canonical Agent Surface

The broker exposes canonical command names such as:

- `env.ensure_local`
- `env.ensure_ssh`
- `file.read_text`
- `task.start`
- `session.create`
- `session.subscribe_frames`

Commands are validated with Pydantic models before dispatch. The SDK facade calls these canonical commands through a `RuntimeTransport` protocol.

## Dependency Direction

- Core models do not depend on SQLAlchemy, FastAPI, CLI, broker, or provider-specific implementations.
- Providers do not depend on CLI or REST adapters.
- Services depend on core models, repositories, and providers.
- Broker/API/CLI/SDK are adapters over services and providers.
- Business behavior should stay in services/providers, not in the SDK or CLI.

## Durable Remote Design

Remote non-interactive tasks use `ssh_detached`:

- Upload a launcher over SFTP.
- Start a detached remote process through SSH.
- Tail remote logs over SFTP.
- Read remote status JSON for final exit code.
- Reattach on local runtime restart.

Remote interactive durable sessions use `ssh_tmux`:

- Start a remote tmux session.
- Pipe pane output to a remote log file.
- Tail output into persisted TerminalFrames.
- Reattach to an existing tmux session after local runtime restart.

Plain `ssh_pty` is intentionally not durable because it is bound to the live SSH channel.

## Known Architectural Limits

- Workspace sync is still basic and local-first.
- WebSocket is not implemented; broker streams are the current push-style mechanism.
- SSH `proxy_jump` is represented in config but not implemented.
- Non-persistent SSH tasks are not implemented.
- Multi-user isolation currently focuses on broker token authentication and writer leases, not full RBAC.
