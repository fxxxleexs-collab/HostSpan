from __future__ import annotations

import stat as stat_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from environment_runtime.core.errors import ValidationError
from environment_runtime.core.models import Endpoint
from environment_runtime.providers.filesystem.sftp import SFTPFilesystemProvider


class FakeRemoteFile:
    def __init__(self, path: Path, mode: str) -> None:
        self.path = path
        self.mode = mode
        self.handle = None

    async def __aenter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open(self.mode)
        return self

    async def __aexit__(self, *_args) -> None:
        assert self.handle is not None
        self.handle.close()

    async def read(self, size: int = -1):
        assert self.handle is not None
        return self.handle.read(size)

    async def write(self, data: bytes) -> None:
        assert self.handle is not None
        self.handle.write(data)


class FakeSFTPClient:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def _local(self, remote_path: str) -> Path:
        return self.root / remote_path.lstrip("/")

    async def mkdir(self, remote_path: str) -> None:
        self._local(remote_path).mkdir()

    def open(self, remote_path: str, mode: str) -> FakeRemoteFile:
        return FakeRemoteFile(self._local(remote_path), mode)

    async def stat(self, remote_path: str):
        path = self._local(remote_path)
        info = path.stat()
        return SimpleNamespace(
            size=info.st_size,
            permissions=info.st_mode,
            mtime=info.st_mtime,
        )

    async def scandir(self, remote_path: str):
        entries = []
        for path in self._local(remote_path).iterdir():
            info = path.stat()
            entries.append(
                SimpleNamespace(
                    filename=path.name,
                    attrs=SimpleNamespace(
                        size=info.st_size,
                        permissions=info.st_mode,
                        mtime=info.st_mtime,
                    ),
                )
            )
        return entries

    async def rename(self, source: str, target: str) -> None:
        self._local(source).rename(self._local(target))

    async def remove(self, remote_path: str) -> None:
        self._local(remote_path).unlink()

    async def rmdir(self, remote_path: str) -> None:
        self._local(remote_path).rmdir()


class FakeSSHConnection:
    def __init__(self, root: Path) -> None:
        self.root = root

    def start_sftp_client(self) -> FakeSFTPClient:
        return FakeSFTPClient(self.root)


class FakeSSHTransport:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def connect(self, _endpoint: Endpoint) -> FakeSSHConnection:
        return FakeSSHConnection(self.root)


@pytest.fixture
def ssh_endpoint(tmp_path: Path) -> Endpoint:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")
    return Endpoint(
        name="ssh-demo",
        provider_type="ssh",
        config={
            "hostname": "example.test",
            "username": "envrt",
            "known_hosts_file": str(known_hosts),
        },
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sftp_write_read_hash_walk_and_rename(tmp_path, ssh_endpoint) -> None:
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    provider = SFTPFilesystemProvider(FakeSSHTransport(remote_root))  # type: ignore[arg-type]

    await provider.write_bytes(ssh_endpoint, "/workspace/src/file.txt", b"hello")
    await provider.write_bytes(ssh_endpoint, "/workspace/src/nested/other.bin", b"\x00\x01")

    assert await provider.exists(ssh_endpoint, "/workspace/src/file.txt")
    assert await provider.read_bytes(ssh_endpoint, "/workspace/src/file.txt") == b"hello"
    assert await provider.sha256(ssh_endpoint, "/workspace/src/file.txt")

    files = await provider.walk_files(ssh_endpoint, "/workspace")
    assert files == [
        "/workspace/src/file.txt",
        "/workspace/src/nested/other.bin",
    ]

    await provider.rename(ssh_endpoint, "/workspace/src/file.txt", "/workspace/src/renamed.txt")
    assert not await provider.exists(ssh_endpoint, "/workspace/src/file.txt")
    assert await provider.read_bytes(ssh_endpoint, "/workspace/src/renamed.txt") == b"hello"

    metadata = await provider.stat(ssh_endpoint, "/workspace/src")
    assert metadata["is_dir"] is True
    assert stat_module.S_ISDIR(metadata["permissions"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sftp_rejects_parent_traversal(tmp_path, ssh_endpoint) -> None:
    provider = SFTPFilesystemProvider(FakeSSHTransport(tmp_path))  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await provider.read_bytes(ssh_endpoint, "/workspace/../secret.txt")
