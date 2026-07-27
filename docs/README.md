# Documentation Index

Start here when working on Environment Runtime internals.

## Overview

- [Architecture](./architecture.md): layers, provider families, broker as canonical agent surface, durable remote design.
- [Domain Model](./domain-model.md): endpoints, environments, tasks, sessions, workspaces, TerminalFrames, events.
- [API And Adapter Surfaces](./api.md): broker command groups, FastAPI routes, CLI surface.

## Agent And Broker

- [Agent SDK](./agent-sdk.md): `AgentRuntimeClient`, namespaces, harness mapping.
- [Broker](./broker.md): token auth, principals, writer leases, command discovery, streams.

## Runtime Capabilities

- [Remote SSH Capabilities](./remote.md): SSH endpoints, SFTP, `ssh_detached`, `ssh_pty`, `ssh_tmux`.
- [Recovery](./recovery.md): detached task recovery, tmux session recovery, state semantics.
- [Security](./security.md): implemented security model and current limits.

## Usage And Validation

- [Examples](./examples.md): common SDK flows.
- [Testing](./testing.md): standard test commands and optional Docker SSH test.

## Status

- [Implementation Status](../IMPLEMENTATION_STATUS.md): completed work, partial areas, roadmap.
