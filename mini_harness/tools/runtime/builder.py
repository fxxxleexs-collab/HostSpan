from __future__ import annotations

from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.file import build_file_tools
from mini_harness.tools.runtime.remote import build_remote_tools
from mini_harness.tools.runtime.sync import build_sync_tools
from mini_harness.tools.runtime.task import build_task_tools
from mini_harness.tools.runtime.terminal import build_terminal_tools


def build_runtime_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    remote_tools = build_remote_tools(runtime)
    ensure_remote_tool, request_ssh_connection_tool = remote_tools
    return [
        *build_file_tools(runtime),
        *build_task_tools(runtime),
        ensure_remote_tool,
        *build_sync_tools(runtime),
        request_ssh_connection_tool,
        *build_terminal_tools(runtime),
    ]


__all__ = ["build_runtime_tools"]
