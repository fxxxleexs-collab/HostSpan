# Security

The current security model is a local-first MVP intended to prevent accidental cross-client interference and uncontrolled session writes. It is not yet a complete multi-user RBAC system.

## Implemented

Broker authentication:

- Broker startup creates `<runtime.data_dir>/broker.token`.
- `BrokerClient` and `AgentRuntimeClient.from_broker(settings=...)` read the token automatically.
- Requests without a valid token are rejected.

Principal metadata:

- Broker requests include `principal_id`, `principal_type`, and `scope_id`.
- The principal is passed into command handling.
- `session.write` defaults to the authenticated `principal_id` when validating writer leases.

Writer leases:

- A valid writer lease is required for broker `session.write`.
- Lease owner and expiry are enforced.
- `force=True` can take over a session lease.

Terminal input handling:

- Session input is persisted as redacted TerminalFrames.
- Output is stored as replayable TerminalFrames.

SSH safety:

- SSH endpoints require strict known-host validation.
- Identity files and SSH agent use are explicit endpoint config.
- Host-key bypass is not provided.

Local file safety:

- Broker `file.*` commands constrain local endpoint paths to the endpoint root.
- This prevents `../` traversal outside the local endpoint root.

## Current Limits

- No full resource ownership model yet.
- `scope_id` is metadata today, not a full authorization boundary.
- No per-resource ACL/RBAC enforcement yet.
- No Windows Named Pipe ACL customization beyond local token auth.
- No remote secret management beyond SSH key and known-host configuration.
- No port-forwarding security policy because port forwarding is not implemented.

## Recommended Next Steps

1. Add owner/scope fields to resources that need access control.
2. Enforce same-scope resource reads and writes in broker command dispatch.
3. Separate read-only observers from write-capable principals.
4. Persist principal metadata on sensitive actions.
5. Add clearer lease renewal and lease release commands.
6. Add platform-specific broker socket/pipe permission hardening.

## Practical Guidance

For local agent harness development:

- Use one broker per project scope.
- Give each agent a stable `principal_id`.
- Require leases for all session writes.
- Prefer `AgentRuntimeClient` over raw broker requests.
- Treat endpoint definitions and SSH keys as sensitive local configuration.
