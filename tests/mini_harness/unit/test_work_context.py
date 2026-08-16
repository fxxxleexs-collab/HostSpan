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
        updated_by="terminal_command",
    )

    summary = context.runtime_activity_summary()

    assert "Recent runtime transitions: none" in summary
    assert "Managed tasks tracked for this conversation:" in summary
    assert "task:task_1" in summary
    assert "pid=12345" in summary
    assert "python app.py" in summary
    assert "Running on http://127.0.0.1:5000" in summary
    assert "Terminal sessions:" in summary
    assert "session:session_1" in summary
    assert "privilege=root" in summary
    assert "pending=true" in summary


def test_runtime_activity_summary_includes_recent_transitions() -> None:
    context = _context()
    context.record_runtime_transition(
        kind="terminal",
        action="close",
        ref="session:old",
        summary="closed session:old; active terminal is now none",
        state="TERMINATED",
        active_after="none",
    )
    context.record_runtime_transition(
        kind="terminal",
        action="open",
        ref="session:new",
        summary="opened local session:new using local_pty",
        state="ACTIVE",
        active_after="session:new",
    )

    summary = context.runtime_activity_summary()

    assert "Recent runtime transitions:" in summary
    assert "terminal.open" in summary
    assert "ref=session:new" in summary
    assert "active_after=session:new" in summary
    assert "terminal.close" in summary
    assert "ref=session:old" in summary
    assert "active_after=none" in summary


def test_managed_task_inventory_lists_active_task_pid() -> None:
    context = _context()
    context.active_task_id = "task_1"
    context.record_task_brief(
        "task_1",
        argv=["python", "-m", "http.server", "8000"],
        cwd="/project",
        state="RUNNING",
        pid=12345,
        persistent=True,
        started_by="start_task",
    )

    inventory = context.managed_task_inventory(active_only=True)

    assert context.active_task_is_tracked()
    assert inventory == [
        {
            "task_id": "task_1",
            "argv": ["python", "-m", "http.server", "8000"],
            "cwd": "/project",
            "state": "RUNNING",
            "pid": 12345,
            "persistent": True,
            "brief": None,
            "log_tail": None,
            "exit_code": None,
            "started_by": "start_task",
            "active": True,
            "touched_at": inventory[0]["touched_at"],
            "touch_index": 1,
        }
    ]


def test_task_brief_is_saved_and_summarized() -> None:
    context = _context()
    context.active_task_id = "task_1"

    context.record_task_brief(
        "task_1",
        argv=["python", "-m", "http.server", "8000"],
        cwd="/project",
        state="RUNNING",
        pid=12345,
        persistent=True,
        brief="serving local test app and waiting for requests",
        started_by="start_task",
    )

    inventory = context.managed_task_inventory(active_only=True)
    summary = context.runtime_activity_summary()

    assert inventory[0]["brief"] == "serving local test app and waiting for requests"
    assert "brief=serving local test app and waiting for requests" in summary


def test_runtime_activity_summary_does_not_redact_docker_ps_output() -> None:
    context = _context()
    docker_output = (
        "CONTAINER ID   IMAGE          COMMAND                  PORTS\n"
        "b6f4d2c8a9e0123456789abcdef01234   nginx:latest   "
        '"nginx -g daemon off;"   0.0.0.0:5000->80/tcp\n'
        "sha256:3b6e6d2c9f30123456789abcdef0123456789abcdef0123456789abcdef0123\n"
    )
    context.record_task_brief(
        "task_docker",
        argv=["docker", "ps"],
        cwd="/project",
        state="RUNNING",
        pid=123,
        persistent=True,
        log_tail=docker_output,
        started_by="start_task",
    )

    summary = context.runtime_activity_summary()

    assert "b6f4d2c8a9e0123456789abcdef01234" in summary
    assert "sha256:3b6e6d2c9f30123456789abcdef0123456789abcdef0123456789abcdef0123" in summary
    assert "0.0.0.0:5000->80/tcp" in summary
    assert "[REDACTED]" not in summary


def test_runtime_activity_summary_redacts_high_confidence_secrets() -> None:
    context = _context()
    context.record_session_interaction(
        "session_secret",
        target="remote",
        backend="ssh_pty",
        runtime_state="ACTIVE",
        brief=(
            "password=hunter2 Authorization: Bearer abc.def.ghi "
            "OPENAI_API_KEY=sk-testsecretvalue1234567890"
        ),
        pending=False,
        updated_by="observe_terminal",
    )

    summary = context.runtime_activity_summary()

    assert "password=[REDACTED]" in summary
    assert "Authorization: Bearer [REDACTED]" in summary
    assert "OPENAI_API_KEY=[REDACTED]" in summary
    assert "hunter2" not in summary
    assert "sk-testsecretvalue" not in summary
