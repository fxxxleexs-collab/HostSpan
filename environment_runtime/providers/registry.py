from __future__ import annotations

from environment_runtime.providers.execution.local_process import LocalProcessExecutionProvider
from environment_runtime.providers.filesystem.local import LocalFilesystemProvider
from environment_runtime.providers.session.local_pty import LocalSessionProvider
from environment_runtime.providers.synchronization.snapshot import SnapshotSyncProvider
from environment_runtime.providers.transport.local import LocalTransportProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self.transport = {"local": LocalTransportProvider()}
        self.filesystem = {"local": LocalFilesystemProvider()}
        self.execution = {"local_process": LocalProcessExecutionProvider()}
        self.session = {"local_pty": LocalSessionProvider()}
        self.sync = {"snapshot": SnapshotSyncProvider()}
