# Examples

## Local Task

1. Create a local endpoint.
2. Create an environment that references it.
3. Start a task with `envrt task run`.
4. Inspect logs with `envrt task logs`.

## Workspace Revision and Sync

1. Create a workspace.
2. Add a logical root and local replica.
3. Create a revision.
4. Add a second replica and bind it.
5. Run workspace sync.

## Session Input

1. Create a session running a script that calls `input()`.
2. Acquire a writer lease.
3. Create an `InputRequest`.
4. Submit the input payload.

## Artifact Export

1. Create a workspace-backed file.
2. Register it as an artifact.
3. Download it to a chosen destination.
