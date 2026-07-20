install:
	.venv\Scripts\python -m pip install -e ".[dev]"

lint:
	.venv\Scripts\python -m ruff check environment_runtime tests

typecheck:
	.venv\Scripts\python -m mypy environment_runtime

test-unit:
	.venv\Scripts\python -m pytest -m unit

test-local:
	.venv\Scripts\python -m pytest -m integration

test-all:
	.venv\Scripts\python -m pytest

serve:
	.venv\Scripts\envrt serve
