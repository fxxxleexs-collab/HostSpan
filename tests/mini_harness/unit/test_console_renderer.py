from __future__ import annotations

from mini_harness.agent.events import AgentEventType
from mini_harness.agent.state import AgentState
from mini_harness.ui.console import RichEventRenderer


def test_renderer_escapes_markup_like_command_text() -> None:
    renderer = RichEventRenderer(no_color=True)
    command = [
        "/bin/sh",
        "-c",
        'uvicorn main:asgi_app --host 0.0.0.0 --forwarded-allow-ips "*"',
    ]

    renderer.emit(
        AgentEventType.TOOL_STARTED,
        AgentState.EXECUTING_TOOL,
        "Started run_command",
        {"tool_name": "run_command", "arguments": {"argv": command}},
    )
    renderer.emit(
        AgentEventType.TOOL_COMPLETED,
        AgentState.PROCESSING_RESULT,
        f"started command task:task_1: {command}",
        {"metadata": {"argv": command, "task_id": "task_1"}},
    )

    assert len(renderer.events) == 2
