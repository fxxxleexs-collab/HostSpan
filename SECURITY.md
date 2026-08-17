# Security Policy

## Supported Versions

Security fixes are currently considered for the main branch only until the project reaches a stable release process.

## Reporting A Vulnerability

Please do not open public issues for vulnerabilities involving credential exposure, remote command execution, workspace escape, SSH trust bypass, or authorization bypass.

Until a dedicated security contact is published, report privately to the repository maintainers. Include:

- Affected component and version or commit.
- Reproduction steps.
- Expected and actual behavior.
- Impact assessment.
- Any relevant logs with secrets redacted.

## Current Security Model

Environment Runtime is a local-first developer runtime. It includes broker token authentication, strict SSH known-host validation, local endpoint path constraints, writer leases for terminal input, Mini Harness capability checks, and a policy-only workspace sandbox.

Current limitations are documented in [docs/security.md](docs/security.md). In particular, full multi-user RBAC, complete resource ownership enforcement, container-grade sandboxing, remote secret management, and WebSocket security policy are not yet implemented.

## Handling Secrets

- Prefer environment variables or interactive prompts for API keys and passwords.
- Do not commit `.env`, private keys, known-sensitive logs, runtime databases, or trace files.
- Review `.mini-harness/runs/` artifacts before sharing them.
