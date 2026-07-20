# Security

## Implemented

- Logical workspace paths reject absolute paths and parent traversal.
- Writer leases prevent concurrent uncontrolled writes to a session.
- Secret inputs are stored as request metadata without logging the submitted secret value.
- Command execution prefers argv-based invocation.

## Not Yet Implemented

- SSH host-key validation
- remote secret transport
- terminal audit partitioning for tmux and SSH
- port forwarding controls

## Current Limitations

The local MVP assumes trusted local process execution. Remote trust boundaries are not in place until SSH/SFTP providers are added.
