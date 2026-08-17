from __future__ import annotations

from mini_harness.tools.adapter import build_runtime_tools as build_adapter_tools
from mini_harness.tools.runtime.builder import build_internal_runtime_tools
from mini_harness.tools.runtime.builder import build_runtime_tools as build_namespaced_tools


def test_runtime_tool_builder_matches_adapter_compat_entrypoint(fake_runtime) -> None:
    adapter_names = [tool.definition.name for tool in build_adapter_tools(fake_runtime)]
    namespaced_names = [tool.definition.name for tool in build_namespaced_tools(fake_runtime)]

    assert namespaced_names == adapter_names
    assert namespaced_names == ["file", "command", "task", "remote", "terminal"]
    assert "run_command" not in namespaced_names
    assert "terminal_command" not in namespaced_names
    assert "run_terminal_command" not in namespaced_names
    assert "run_in_session" not in namespaced_names
    assert "open_local_terminal" not in namespaced_names
    assert "open_remote_terminal" not in namespaced_names
    assert "request_human_terminal_input" not in namespaced_names


def test_command_and_task_facades_do_not_expose_current_target(fake_runtime) -> None:
    tools = {tool.definition.name: tool for tool in build_namespaced_tools(fake_runtime)}

    command_target = tools["command"].definition.input_schema["properties"]["target"]
    task_target = tools["task"].definition.input_schema["properties"]["target"]

    assert command_target["default"] == "local"
    assert task_target["default"] == "local"
    assert command_target["enum"] == ["local", "remote"]
    assert task_target["enum"] == ["local", "remote"]


def test_internal_runtime_tool_builder_keeps_low_level_tools(fake_runtime) -> None:
    internal_names = [tool.definition.name for tool in build_internal_runtime_tools(fake_runtime)]

    assert "run_command" in internal_names
    assert "terminal_command" in internal_names
    assert "request_human_terminal_input" in internal_names
    assert "run_in_session" not in internal_names
