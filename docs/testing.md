# Testing

## Standard Validation

Run:

```bash
python -m ruff check environment_runtime tests
python -m mypy environment_runtime
python -m pytest
```

The default test suite skips real SSH Docker tests unless explicitly enabled.

## Current Test Areas

Unit tests cover:

- domain validation and writer leases
- local and SSH provider behavior with fakes
- detached execution behavior
- session backend contract
- SSH PTY command and output handling
- SSH tmux command, attach, status, and recovery behavior
- TerminalFrame persistence
- broker command dispatch, token auth, writer lease enforcement, and streams
- Agent SDK facade method mapping

Integration tests cover:

- local runtime flow
- local broker shared active state
- broker event and TerminalFrame streams
- local persistent task tracking through Agent SDK
- optional real SSH persistent task recovery through Agent SDK

## Optional Real SSH Test

The optional test is:

```bash
tests/integration/test_agent_sdk_remote_task.py
```

It is skipped unless:

```powershell
$env:ENVRT_TEST_SSH_DOCKER = "1"
```

Default SSH test values:

- host: `127.0.0.1`
- port: `2222`
- user: `envrt`
- key: `manual_ssh_test/envrt_test_key`
- known hosts: `manual_ssh_test/known_hosts`

Override with:

- `ENVRT_TEST_SSH_HOST`
- `ENVRT_TEST_SSH_PORT`
- `ENVRT_TEST_SSH_USER`
- `ENVRT_TEST_SSH_KEY`
- `ENVRT_TEST_SSH_KNOWN_HOSTS`

Run:

```powershell
$env:ENVRT_TEST_SSH_DOCKER = "1"
.\.venv\Scripts\python -m pytest tests\integration\test_agent_sdk_remote_task.py -q
```

The test verifies:

- `AgentRuntimeClient.environments.ensure_ssh`
- SFTP file write/read through SDK
- remote persistent task startup
- SDK log polling
- broker/runtime shutdown
- broker/runtime restart with the same DB and data dir
- remote detached task recovery
- final task state and exit code

## Docker Notes

The manually used Docker SSH container is expected to expose SSH on host port `2222`.

If Windows denies access to the default pytest temp directory, run pytest with `TMP` and `TEMP` pointing to a workspace-local directory.

## Known Testing Gaps

- No WebSocket tests because WebSocket streaming is not implemented.
- No SSH `proxy_jump` tests because proxy jump is not implemented.
- No full remote workspace sync tests yet.
- No full RBAC/scope authorization tests yet.
