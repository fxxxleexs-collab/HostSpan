from __future__ import annotations

import pytest

from mini_harness.errors import MiniHarnessError
from mini_harness.runtime.work_context import WorkContext


def _context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/project",
    )


def test_normalize_path_rejects_escape() -> None:
    with pytest.raises(MiniHarnessError):
        _context().normalize_path("../outside.py")


def test_normalize_path_rejects_absolute() -> None:
    with pytest.raises(MiniHarnessError):
        _context().normalize_path("/tmp/outside.py")


def test_normalize_path_keeps_relative_shape() -> None:
    assert _context().normalize_path("./src/bad") == "src/bad"


def test_task_cursor_and_ref() -> None:
    context = _context()
    context.active_task_id = "task_1"
    context.task_log_cursor = 42

    assert context.task_ref() == "task:task_1"
    assert context.task_log_cursor == 42
