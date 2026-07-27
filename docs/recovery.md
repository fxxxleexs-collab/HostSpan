# Recovery

Recovery runs when `build_runtime()` creates a new runtime context. Its job is to make persisted state honest after the local process or broker exits.

## Task Recovery

Recoverable task states:

- `PREPARING`
- `RUNNING`
- `CANCELLING`

Durable task backends:

- `local_detached`
- `ssh_detached`

`local_detached` recovery:

- Reads local log/status files under `runtime.data_dir`.
- Reattaches a watcher if the process is still alive.
- Finalizes from status file when the task has completed.
- Marks the task `LOST` when it cannot be reclaimed honestly.

`ssh_detached` recovery:

- Uses endpoint metadata from `backend_ref`.
- Reads remote log and status files through SFTP.
- Reattaches a watcher when the remote PID is still alive.
- Resumes log tailing from the persisted log offset.
- Finalizes from remote status JSON when available.
- Marks the task `LOST` if the remote state cannot be determined.

Current limit:

- A reconnected detached remote task is tracked to completion, but cancellation after recovery is limited because it is not registered as a normal active task handle.

## Session Recovery

Recoverable session states:

- `CREATING`
- `ACTIVE`

Durable session backend:

- `ssh_tmux`

`ssh_tmux` recovery:

- Checks whether the remote tmux session still exists.
- Reattaches a local watcher if the tmux session is alive.
- Tails the remote pane log from the persisted TerminalFrame output offset.
- Restores finished status from remote status JSON when the session has completed.

Non-durable session backends:

- `local_pty`
- `ssh_pty`

These are marked `DISCONNECTED` after restart because their local process handle or SSH channel is gone.

## Normal Shutdown

During `shutdown_runtime()`:

- Detached tasks are detached rather than killed.
- `ssh_tmux` sessions are detached rather than killed.
- Non-durable sessions are terminated.

## State Semantics

- `SUCCEEDED`: completed with exit code 0.
- `FAILED`: completed with non-zero exit code.
- `LOST`: the runtime cannot honestly determine task outcome.
- `DISCONNECTED`: an interactive session no longer has a live controllable backend.
- `TERMINATED`: a session has ended.

## Recommended Manual Recovery Tests

Remote persistent task:

1. Start broker.
2. Use `AgentRuntimeClient` to `ensure_ssh`.
3. Start a remote task with `persistent=True`.
4. Wait for a mid-run log marker.
5. Shutdown broker.
6. Start broker again using the same database and data dir.
7. Verify `task.get` returns `RUNNING` or `SUCCEEDED`.
8. Verify `tasks.wait_for_log` can see later remote output.
9. Verify final state is `SUCCEEDED` and `exit_code == 0`.

Remote tmux session:

1. Ensure the remote host has `tmux`.
2. Create a session with `backend="ssh_tmux"`.
3. Wait for output.
4. Shutdown broker.
5. Restart broker with the same database and data dir.
6. Verify the session is `ACTIVE`.
7. Write more input through a writer lease.
8. Verify new output appears.
