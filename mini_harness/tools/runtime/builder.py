from __future__ import annotations

from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.tools.base import AgentTool


def build_runtime_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    from mini_harness.tools.adapter import (
        ActivateTerminalSessionTool,
        CancelTaskTool,
        CloseTerminalTool,
        EditFileTool,
        EnsureRemoteToolTool,
        InspectTerminalSessionTool,
        ListFilesTool,
        ListTasksTool,
        ListTerminalSessionsTool,
        ObserveTaskTool,
        ObserveTerminalTool,
        OpenTerminalTool,
        ReadFileTool,
        RequestHumanTerminalInputTool,
        RequestSSHConnectionTool,
        RunCommandTool,
        RunInSessionTool,
        SendTerminalControlTool,
        SendTerminalInputTool,
        StartTaskTool,
        SyncPushTool,
        SyncStatusTool,
        WriteFileTool,
    )

    return [
        ListFilesTool(runtime),
        ReadFileTool(runtime),
        WriteFileTool(runtime),
        EditFileTool(runtime),
        RunCommandTool(runtime),
        StartTaskTool(runtime),
        ObserveTaskTool(runtime),
        CancelTaskTool(runtime),
        ListTasksTool(runtime),
        EnsureRemoteToolTool(runtime),
        SyncStatusTool(runtime),
        SyncPushTool(runtime),
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
