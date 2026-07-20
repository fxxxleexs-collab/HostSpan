# Domain Model

## Endpoint

Represents a reachable execution or storage location. The MVP supports `local` endpoints.

## Environment

Aggregates endpoints, workspaces, and execution targets. It is the unit used to launch tasks and sessions.

## Workspace

Defines logical roots plus one or more physical replicas. The current implementation supports revision hashing and one-way local snapshot sync.

## Task

Represents a discrete command execution with persistent state, exit code, timestamps, and stored logs.

## Session

Represents a long-lived interactive subprocess. It can receive routed user input when a valid writer lease exists.

## InputRequest

Stores the runtime-level fact that a client wants to route input to a session. The runtime does not try to infer prompt semantics.

## WriterLease

Grants exclusive write access to a session. The current implementation enforces ownership and lease expiry.

## Artifact

Registers a file reachable through a workspace path and stores its metadata plus hash.

## RuntimeEvent

Captures lifecycle events for resources such as endpoints, environments, tasks, sessions, workspaces, and interactions.
