# Remote SSH Capabilities

Remote support is built around SSH transport, SFTP filesystem access, detached tasks, direct PTY sessions, and tmux-backed durable sessions.

## SSH Endpoint

Create or ensure an SSH environment through the Agent SDK:

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
```

Security behavior:

- known-host validation is required
- identity file or SSH agent must be configured
- host-key bypass is not provided

Current limit:

- `proxy_jump` is present in config but not implemented in the transport.

## SFTP Files

Broker `file.*` commands route SSH endpoint file access through SFTP.

```python
endpoint_id = bundle["endpoint"]["endpoint_id"]

client.files.write_text(endpoint_id, ".environment-runtime/probe.txt", "ok")
text = client.files.read_text(endpoint_id, ".environment-runtime/probe.txt")
```

Supported operations:

- exists
- stat
- list
- mkdir
- remove
- sha256
- read/write text
- read/write bytes

## Remote Persistent Tasks

For SSH task execution, use `persistent=True`.

```python
task = client.tasks.start(
    bundle["environment"]["environment_id"],
    bundle["target_id"],
    ["bash", "-lc", "for i in 0 1 2 3; do echo TICK=$i; sleep 1; done"],
    persistent=True,
)
```

The `ssh_detached` backend:

- uploads a launcher
- starts a remote detached process
- writes remote logs and status files
- tails logs through SFTP
- restores exit code from remote status JSON
- can reattach after local runtime restart

Current limit:

- Non-persistent SSH task execution is rejected.
- Cancellation after recovery is limited.

## Remote Interactive Sessions

Two SSH session backends exist:

- `ssh_pty`
- `ssh_tmux`

`ssh_pty`:

- uses a direct AsyncSSH PTY channel
- supports live write and resize
- does not survive local runtime restart

`ssh_tmux`:

- starts a remote tmux session
- pipes pane output to a remote log
- tails output into TerminalFrames
- supports write and resize
- can reattach after local runtime restart

Example:

```python
session = client.sessions.create(
    bundle["environment"]["environment_id"],
    bundle["target_id"],
    ["bash", "-l"],
    backend="ssh_tmux",
)
```

The remote host must have `tmux` installed.

## When Tmux Is Missing

Without tmux, durable interactive sessions are not available. The runtime can still run and recover remote non-interactive tasks through `ssh_detached`.

Recommended fallback:

- Use `ssh_detached` for long-running non-interactive commands.
- Ask the user or agent to install tmux for durable interactive sessions.
- Keep `ssh_pty` for non-durable live interaction.

## Remote Testing

See `docs/testing.md` for the optional Docker SSH test. It verifies SDK-driven remote task execution and recovery across broker restart.
