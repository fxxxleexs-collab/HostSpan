from __future__ import annotations

from mini_harness.tools.adapter import build_runtime_tools as build_adapter_tools
from mini_harness.tools.runtime.builder import build_runtime_tools as build_namespaced_tools


def test_runtime_tool_builder_matches_adapter_compat_entrypoint(fake_runtime) -> None:
    adapter_names = [tool.definition.name for tool in build_adapter_tools(fake_runtime)]
    namespaced_names = [tool.definition.name for tool in build_namespaced_tools(fake_runtime)]

    assert namespaced_names == adapter_names
    assert "run_command" in namespaced_names
    assert "run_terminal_command" in namespaced_names
    assert "request_human_terminal_input" in namespaced_names
