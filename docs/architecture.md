# Architecture

## Layers

- `core`: domain models, IDs, events, command specs, and logical path validation
- `services`: application orchestration and policy enforcement
- `providers`: concrete local adapters for filesystem, execution, sync, and sessions
- `interfaces`: FastAPI routes, CLI commands, and SDK clients

## Current Runtime Flow

1. A user creates an `Endpoint`.
2. An `Environment` references one or more endpoints and exposes execution targets.
3. A `Workspace` holds logical roots and physical replicas.
4. `WorkspaceService` computes revisions and performs one-way snapshot sync.
5. `TaskService` starts local subprocesses and persists logs plus lifecycle events.
6. `SessionService` starts interactive subprocesses and allows `InteractionService` to route input through a valid writer lease.
7. `ArtifactService` registers and exports files addressed through workspace paths.

## Dependency Direction

- API and CLI depend on services.
- Services depend on core models, repositories, and providers.
- Providers do not depend on FastAPI or CLI code.
- Core does not depend on SQLAlchemy, FastAPI, or provider-specific modules.

## Current Gaps

The codebase is intentionally structured so `ssh`, `sftp`, `tmux`, and recovery components can be added without redesigning the domain model, but those implementations are not present yet.
