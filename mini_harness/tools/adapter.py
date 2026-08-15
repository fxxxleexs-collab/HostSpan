from __future__ import annotations

from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.file import EditFileTool as EditFileTool
from mini_harness.tools.runtime.file import ListFilesTool as ListFilesTool
from mini_harness.tools.runtime.file import ReadFileTool as ReadFileTool
from mini_harness.tools.runtime.file import WriteFileTool as WriteFileTool
from mini_harness.tools.runtime.remote import EnsureRemoteToolTool as EnsureRemoteToolTool
from mini_harness.tools.runtime.remote import RequestSSHConnectionTool as RequestSSHConnectionTool
from mini_harness.tools.runtime.sync import SyncPushTool as SyncPushTool
from mini_harness.tools.runtime.sync import SyncStatusTool as SyncStatusTool
from mini_harness.tools.runtime.task import CancelTaskTool as CancelTaskTool
from mini_harness.tools.runtime.task import ListTasksTool as ListTasksTool
from mini_harness.tools.runtime.task import ObserveTaskTool as ObserveTaskTool
from mini_harness.tools.runtime.task import RunCommandTool as RunCommandTool
from mini_harness.tools.runtime.task import StartTaskTool as StartTaskTool
from mini_harness.tools.runtime.terminal import (
    ActivateTerminalSessionTool as ActivateTerminalSessionTool,
)
from mini_harness.tools.runtime.terminal import CloseTerminalTool as CloseTerminalTool
from mini_harness.tools.runtime.terminal import (
    InspectTerminalSessionTool as InspectTerminalSessionTool,
)
from mini_harness.tools.runtime.terminal import (
    ListTerminalSessionsTool as ListTerminalSessionsTool,
)
from mini_harness.tools.runtime.terminal import ObserveTerminalTool as ObserveTerminalTool
from mini_harness.tools.runtime.terminal import OpenTerminalTool as OpenTerminalTool
from mini_harness.tools.runtime.terminal import (
    RequestHumanTerminalInputTool as RequestHumanTerminalInputTool,
)
from mini_harness.tools.runtime.terminal import (
    SendTerminalControlTool as SendTerminalControlTool,
)
from mini_harness.tools.runtime.terminal import TerminalCommandTool as TerminalCommandTool


def build_runtime_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    from mini_harness.tools.runtime.builder import build_runtime_tools as build

    return build(runtime)


__all__ = [
    "ActivateTerminalSessionTool",
    "CancelTaskTool",
    "CloseTerminalTool",
    "EditFileTool",
    "EnsureRemoteToolTool",
    "InspectTerminalSessionTool",
    "ListFilesTool",
    "ListTasksTool",
    "ListTerminalSessionsTool",
    "ObserveTaskTool",
    "ObserveTerminalTool",
    "OpenTerminalTool",
    "ReadFileTool",
    "RequestHumanTerminalInputTool",
    "RequestSSHConnectionTool",
    "RunCommandTool",
    "SendTerminalControlTool",
    "TerminalCommandTool",
    "StartTaskTool",
    "SyncPushTool",
    "SyncStatusTool",
    "WriteFileTool",
    "build_runtime_tools",
]
