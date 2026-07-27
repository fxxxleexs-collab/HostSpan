# Domain Model

## Endpoint

Represents a reachable execution or storage location.

Supported provider types:

- `local`
- `ssh`

Local endpoints provide local filesystem and local process execution. SSH endpoints provide SSH transport, SFTP filesystem access, remote detached task execution, SSH PTY sessions, and tmux-backed durable sessions when tmux is installed on the remote host.

## Environment

Aggregates endpoints, workspaces, and execution targets. It is the resource used to launch tasks and sessions.

An environment has:

- `endpoint_ids`
- `execution_targets`
- `default_execution_target_id`
- `default_session_backend`

Current default session backends:

- local endpoint: `local_pty`
- SSH endpoint: `ssh_pty`

For durable remote interactive sessions, explicitly request `backend="ssh_tmux"`.

## ExecutionTarget

Represents a runnable target inside an environment.

Current target providers:

- `local_process`
- `ssh_process`

For remote tasks, use `persistent=True` so the task routes to `ssh_detached`.

## Workspace

Defines logical roots, physical replicas, and bindings between replicas.

Current capabilities:

- create workspace records
- add roots and replicas
- bind replicas
- compute local snapshot revisions
- local one-way mirror sync

Current limitations:

- no robust SFTP directory sync yet
- no include/exclude filtering in sync behavior yet
- no conflict detection or bidirectional merge

## Task

Represents a non-interactive command execution with:

- command spec
- environment and target
- state
- persisted stdout/stderr logs
- backend metadata
- timestamps
- exit code

Important task states:

- `PREPARING`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`
- `CANCELLING`
- `CANCELLED`
- `LOST`

Durable task backends:

- `local_detached`
- `ssh_detached`

## Session

Represents an interactive process or terminal.

Session data includes:

- command
- backend
- backend metadata
- terminal size
- term type
- interaction state
- exit code

Session backends:

- `local_pty`: local pipe-based interactive process.
- `ssh_pty`: direct AsyncSSH PTY; not durable after local runtime restart.
- `ssh_tmux`: remote tmux-backed session; durable across local runtime restart.

## TerminalFrame

Represents replayable terminal stream data.

Frame kinds:

- `output`
- `input`
- `resize`
- `marker`
- `redacted`

Session output is persisted as TerminalFrames. Session input is recorded as redacted frame data by default.

## InputRequest

Stores the fact that a client or automation flow needs input for a session or task.

Input request types:

- text
- confirmation
- secret
- keystroke
- human takeover

## WriterLease

Grants exclusive write access to a session.

The broker enforces writer leases for `session.write`. If no explicit `owner_id` is provided, the broker uses the authenticated principal ID.

## Artifact

Registers a file reachable through a workspace path and stores metadata plus hash.

## RuntimeEvent

Captures lifecycle events for endpoints, environments, workspaces, tasks, sessions, interactions, artifacts, and recovery transitions.

Runtime events are persisted and also published through the in-memory event bus. Broker `event.subscribe` can stream matching events.
