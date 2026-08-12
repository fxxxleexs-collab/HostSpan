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


def test_runtime_activity_summary_includes_active_tasks_and_sessions() -> None:
    context = _context()
    context.active_task_id = "task_1"
    context.record_task_brief(
        "task_1",
        argv=["python", "app.py"],
        cwd="/project",
        state="RUNNING",
        pid=12345,
        persistent=True,
        log_tail=" * Running on http://127.0.0.1:5000\n",
        started_by="start_task",
    )
    context.active_session_id = "session_1"
    context.record_session_interaction(
        "session_1",
        target="remote",
        backend="ssh_tmux",
        runtime_state="ACTIVE",
        brief="root shell opened for package install",
        last_command="sudo -i",
        privilege="root",
        pending=True,
        updated_by="send_terminal_input",
    )

    summary = context.runtime_activity_summary()

    assert "Managed tasks:" in summary
    assert "task:task_1" in summary
    assert "pid=12345" in summary
    assert "python app.py" in summary
    assert "Running on http://127.0.0.1:5000" in summary
    assert "Terminal sessions:" in summary
    assert "session:session_1" in summary
    assert "privilege=root" in summary
    assert "pending=true" in summary
