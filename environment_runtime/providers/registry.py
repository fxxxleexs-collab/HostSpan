from __future__ import annotations

from typing import Any

from environment_runtime.config import RuntimeSettings
from environment_runtime.providers.execution.local_detached import LocalDetachedExecutionProvider
from environment_runtime.providers.execution.local_process import LocalProcessExecutionProvider
from environment_runtime.providers.execution.ssh_detached import SSHDetachedExecutionProvider
from environment_runtime.providers.filesystem.local import LocalFilesystemProvider
from environment_runtime.providers.filesystem.sftp import SFTPFilesystemProvider
from environment_runtime.providers.session.local_pty import LocalSessionProvider
from environment_runtime.providers.synchronization.snapshot import SnapshotSyncProvider
from environment_runtime.providers.transport.local import LocalTransportProvider
from environment_runtime.providers.transport.ssh import SSHTransportProvider


class ProviderRegistry:
    def __init__(self, settings: RuntimeSettings | None = None) -> None:
        ssh_transport = SSHTransportProvider()
        self.transport: dict[str, Any] = {
            "local": LocalTransportProvider(),
            "ssh": ssh_transport,
        }
        self.filesystem: dict[str, Any] = {
            "local": LocalFilesystemProvider(),
            "sftp": SFTPFilesystemProvider(ssh_transport),
        }
        # Execution providers have deliberately different start() shapes
        # (local_detached takes a task_id), so this map is dynamically dispatched.
        self.execution: dict[str, Any] = {"local_process": LocalProcessExecutionProvider()}
        self.session = {"local_pty": LocalSessionProvider()}
        self.sync = {"snapshot": SnapshotSyncProvider()}
        if settings is not None:
            data_dir = settings.runtime.data_dir
            self.execution["local_detached"] = LocalDetachedExecutionProvider(
                log_dir=data_dir / "logs",
                status_dir=data_dir / "status",
                poll_interval=settings.runtime.detached_poll_interval_seconds,
            )
            self.execution["ssh_detached"] = SSHDetachedExecutionProvider(
                transport=ssh_transport,
                sftp=self.filesystem["sftp"],
                poll_interval=settings.runtime.detached_poll_interval_seconds,
            )
