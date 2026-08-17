# Contributing

Thanks for helping improve Environment Runtime and Mini Harness.

## Development Setup

```bash
python -m venv .venv
pip install -e ".[dev]"
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Validation

Before opening a pull request, run:

```bash
python -m ruff check environment_runtime mini_harness tests
python -m pytest tests/unit tests/integration tests/mini_harness
```

Optional real SSH tests require a local Docker SSH fixture and are skipped by default.

## Contribution Guidelines

- Keep changes scoped and avoid unrelated refactors.
- Prefer broker and `AgentRuntimeClient` surfaces for new agent-facing behavior.
- Document limitations clearly when a feature is partial or experimental.
- Do not commit local secrets, `.env` files, runtime databases, trace logs, or generated build artifacts.
- Add or update tests for behavior changes.

## Project Status

This project is still early. Some surfaces, especially workspace sync, sandbox engines beyond policy-only, WebSocket streaming, and legacy REST/HTTP SDK coverage, are intentionally incomplete.
