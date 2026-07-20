from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    LOCAL_EXECUTION = "local_execution"
    LOCAL_FILESYSTEM = "local_filesystem"
    INTERACTIVE_SESSION = "interactive_session"
    WORKSPACE_SYNC = "workspace_sync"
    ARTIFACTS = "artifacts"
    EVENTS = "events"
