# Testing

## Current Test Layers

- `tests/unit`: path validation, workspace revision behavior, writer lease logic
- `tests/integration`: local runtime flows for task execution, routed input, and artifacts

## Commands

```bash
python -m pytest
python -m ruff check environment_runtime tests
python -m mypy environment_runtime
```

## Planned Test Expansion

- provider contract suites for filesystem, execution, session, and sync adapters
- Docker-based SSH and tmux integration
- recovery and reconciliation tests
- API contract tests and WebSocket tests
