from __future__ import annotations

from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.file import build_file_tools
from mini_harness.tools.runtime.sync import build_sync_tools
from mini_harness.tools.runtime.task import build_task_tools


def build_runtime_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    from mini_harness.tools.adapter import (
        ActivateTerminalSessionTool,
        CloseTerminalTool,
        EnsureRemoteToolTool,
        InspectTerminalSessionTool,
        ListTerminalSessionsTool,
        ObserveTerminalTool,
        OpenTerminalTool,
        RequestHumanTerminalInputTool,
        RequestSSHConnectionTool,
        RunInSessionTool,
        SendTerminalControlTool,
        SendTerminalInputTool,
    )

    return [
        *build_file_tools(runtime),
        *build_task_tools(runtime),
        EnsureRemoteToolTool(runtime),
        *build_sync_tools(runtime),
        RequestSSHConnectionTool(runtime),
        OpenTerminalTool(runtime),
        OpenTerminalTool(runtime, "open_local_terminal", fixed_target="local"),
        OpenTerminalTool(runtime, "open_remote_terminal", fixed_target="remote"),
        ListTerminalSessionsTool(runtime),
        InspectTerminalSessionTool(runtime),
        ActivateTerminalSessionTool(runtime),
        ObserveTerminalTool(runtime),
        SendTerminalInputTool(runtime),
        RequestHumanTerminalInputTool(runtime),
        SendTerminalControlTool(runtime),
        RunInSessionTool(runtime, "run_terminal_command"),
        RunInSessionTool(runtime),
        CloseTerminalTool(runtime),
    ]


__all__ = ["build_runtime_tools"]
