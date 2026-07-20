# Recovery

## Current Behavior

- Resource state persists in SQLite.
- Task logs and runtime events persist in SQLite.
- If the runtime process stops, active subprocess handles are lost.
- `env reconcile` can currently mark an environment as degraded when unfinished task records still exist.

## Not Yet Implemented

- rediscovery of live subprocesses
- tmux pane recovery
- SSH reconnect with output backfill
- persisted offsets for live stream resumption

## Planned Direction

The service and provider split is designed so future reconciliation can inspect provider backends and update persisted resource state accordingly.
