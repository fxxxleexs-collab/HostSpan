# Changelog

All notable changes will be documented here.

This project is currently pre-1.0, so APIs and tool schemas may change between commits.

## Unreleased

- Added Environment Runtime broker, SDK facade, local and SSH runtime capabilities, task/session persistence, recovery support, and Mini Harness agent validation flows.
- Added capability-based Mini Harness tool permissions, policy-only workspace sandboxing, interactive approvals, multi-turn chat, context compaction, terminal controls, task management, and file diff/hash guards.
- Added internal experimental sync module scaffolding. Sync is not exposed through the default Mini Harness agent facade yet.
- Documented current limitations around workspace sync, WebSocket streaming, SSH proxy jump, non-persistent SSH tasks, sandbox engines, and full resource authorization.
