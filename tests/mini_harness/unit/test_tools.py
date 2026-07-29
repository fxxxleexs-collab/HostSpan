from __future__ import annotations

import pytest

from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.adapter import build_runtime_tools
from mini_harness.tools.registry import ToolRegistry


def _context() -> WorkContext:
    return WorkContext(
        endpoint_id="endpoint_1",
        environment_id="env_1",
        target_id="target_1",
        project_root="/project",
    )


@pytest.mark.asyncio
async def test_runtime_tools_map_to_runtime_client(fake_runtime) -> None:
    registry = ToolRegistry()
    for tool in build_runtime_tools(fake_runtime):
        registry.register(tool)
    context = _context()

    listed = await registry.execute("list_files", {"path": "."}, context)
    read = await registry.execute("read_file", {"path": "calculator.py"}, context)
    write = await registry.execute(
        "write_file",
        {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"},
        context,
    )
    run = await registry.execute(
        "run_command",
        {"argv": ["python", "-m", "pytest", "-q"], "cwd": "."},
        context,
    )
    observed = await registry.execute("observe_task", {"wait_seconds": 0}, context)

    assert listed.ok
    assert "calculator.py" in str(listed.content)
    assert read.ok
    assert "return a - b" in str(read.content)
    assert write.ok
    assert run.resource_ref == "task:task_1"
    assert observed.ok
    assert observed.cursor == len(".\n1 passed\n")
    assert context.active_task_id is None
    assert [name for name, _ in fake_runtime.requests] == [
        "list_files",
        "read_text",
        "write_text",
        "start_task",
        "get_task",
        "task_logs",
    ]


@pytest.mark.asyncio
async def test_registry_handles_unknown_tool(fake_runtime) -> None:
    result = await ToolRegistry().execute("missing", {}, _context())

    assert not result.ok
    assert result.error_code == "UNKNOWN_TOOL"
