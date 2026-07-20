# Environment Runtime

Environment Runtime is a stateful Python runtime for development and automation workflows. It manages endpoints, environments, logical workspaces, persistent task records, interactive sessions, routed input requests, artifacts, and a REST/CLI surface without depending on any LLM or agent framework.

## What It Does

This repository currently provides:

- Local endpoints with health checks
- Stateful `Environment`, `Workspace`, `Task`, `Session`, `InputRequest`, `WriterLease`, and `Artifact` resources
- SQLite persistence through async SQLAlchemy
- Local filesystem revisions and one-way snapshot sync
- Local subprocess task execution with persisted logs
- Local interactive sessions with routed input and writer leases
- FastAPI routes, a small SDK, and an `envrt` CLI
- Unit and local integration tests

## What It Does Not Yet Do

The task brief asked for a broader runtime than could be implemented faithfully in a single pass. The following areas are scaffolded in the architecture but not implemented yet:

- SSH transport and host-key validation
- SFTP remote filesystem provider
- tmux-backed persistent remote sessions
- restart reconciliation of live remote resources
- WebSocket streaming
- remote port forwarding
- Docker-based contract and recovery suites

Those gaps are tracked in [IMPLEMENTATION_STATUS.md](./IMPLEMENTATION_STATUS.md).

## Install

```bash
pip install -e ".[dev]"
```

## Quick Start

Add a local endpoint:

```bash
envrt endpoint add-local demo .
```

Create an environment using that endpoint:

```bash
envrt env create local-env --endpoint-ids <endpoint_id>
```

Start the API:

```bash
envrt serve
```

Run tests:

```bash
python -m pytest
python -m ruff check environment_runtime tests
python -m mypy environment_runtime
```

## CLI Surface

The current CLI includes:

- `envrt endpoint add-local`
- `envrt endpoint list`
- `envrt endpoint health`
- `envrt env create`
- `envrt env list`
- `envrt env inspect`
- `envrt env reconcile`
- `envrt workspace create`
- `envrt workspace add-root`
- `envrt workspace add-replica`
- `envrt workspace bind`
- `envrt workspace revision`
- `envrt workspace sync`
- `envrt workspace status`
- `envrt task run`
- `envrt task start`
- `envrt task list`
- `envrt task inspect`
- `envrt task logs`
- `envrt task cancel`
- `envrt session create`
- `envrt session list`
- `envrt session inspect`
- `envrt session attach`
- `envrt session terminate`
- `envrt artifact list`
- `envrt artifact download`
- `envrt serve`

## Python SDK Example

```python
import asyncio

from environment_runtime.sdk import AsyncEnvironmentRuntimeClient


async def main() -> None:
    client = AsyncEnvironmentRuntimeClient("http://127.0.0.1:8000")
    endpoint = await client.add_local_endpoint("demo", ".")
    workspace = await client.create_workspace("demo-workspace")
    environment = await client.create_environment("demo-env", [endpoint["endpoint_id"]], [workspace["workspace_id"]])
    print(environment)
    await client.close()


asyncio.run(main())
```

## Architecture

The runtime is organized into four layers:

- `environment_runtime/core`: resource models, IDs, events, logical paths, domain constraints
- `environment_runtime/services`: orchestration and state transitions
- `environment_runtime/providers`: local execution, filesystem, session, and sync adapters
- `environment_runtime/api`, `environment_runtime/cli`, `environment_runtime/sdk`: user-facing interfaces

More detail lives in [docs/architecture.md](./docs/architecture.md).
