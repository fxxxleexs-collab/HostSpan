from __future__ import annotations

import builtins
import hashlib
import inspect
import stat as stat_module
from pathlib import PurePosixPath
from typing import Any

import asyncssh

from environment_runtime.core.errors import ValidationError
from environment_runtime.core.models import Endpoint
from environment_runtime.providers.transport.ssh import SSHTransportProvider


class SFTPFilesystemProvider:
    """SFTP filesystem adapter backed by an SSH transport connection."""

    def __init__(self, transport: SSHTransportProvider) -> None:
        self._transport = transport

    async def ensure_dir(self, endpoint: Endpoint, path: str) -> None:
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            await self._ensure_dir_client(sftp, remote_path)

    async def write_bytes(self, endpoint: Endpoint, path: str, data: bytes) -> None:
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            await self._ensure_dir_client(sftp, _parent(remote_path))
            async with sftp.open(remote_path, "wb") as handle:
                await handle.write(data)

    async def read_bytes(self, endpoint: Endpoint, path: str) -> bytes:
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp, sftp.open(remote_path, "rb") as handle:
            data = await handle.read()
        return _as_bytes(data)

    async def exists(self, endpoint: Endpoint, path: str) -> bool:
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            return await self._exists_client(sftp, remote_path)

    async def stat(self, endpoint: Endpoint, path: str) -> dict[str, Any]:
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            attrs = await sftp.stat(remote_path)
        return _attrs_to_dict(attrs)

    async def list(self, endpoint: Endpoint, path: str) -> builtins.list[str]:
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            entries = await self._scandir_client(sftp, remote_path)
        return sorted(_entry_name(entry) for entry in entries if _entry_name(entry) not in {".", ".."})

    async def rename(self, endpoint: Endpoint, source: str, target: str) -> None:
        remote_source = _normalize_remote_path(source)
        remote_target = _normalize_remote_path(target)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            await self._ensure_dir_client(sftp, _parent(remote_target))
            await sftp.rename(remote_source, remote_target)

    async def remove(self, endpoint: Endpoint, path: str) -> None:
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            attrs = await sftp.stat(remote_path)
            if _is_dir(attrs):
                await sftp.rmdir(remote_path)
            else:
                await sftp.remove(remote_path)

    async def walk_files(self, endpoint: Endpoint, root: str) -> builtins.list[str]:
        remote_root = _normalize_remote_path(root)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp:
            return sorted(await self._walk_files_client(sftp, remote_root))

    async def sha256(self, endpoint: Endpoint, path: str) -> str:
        digest = hashlib.sha256()
        remote_path = _normalize_remote_path(path)
        connection = await self._transport.connect(endpoint)
        async with connection.start_sftp_client() as sftp, sftp.open(remote_path, "rb") as handle:
            while chunk := await handle.read(1024 * 1024):
                digest.update(_as_bytes(chunk))
        return digest.hexdigest()

    async def _ensure_dir_client(self, sftp: Any, path: str) -> None:
        if path in {"", "/"}:
            return
        current = "/" if path.startswith("/") else ""
        parts = [part for part in PurePosixPath(path).parts if part != "/"]
        for part in parts:
            current = _join_remote(current, part)
            if not await self._exists_client(sftp, current):
                await sftp.mkdir(current)

    async def _exists_client(self, sftp: Any, path: str) -> bool:
        try:
            await sftp.stat(path)
            return True
        except (OSError, asyncssh.SFTPError):
            return False

    async def _scandir_client(self, sftp: Any, root: str) -> builtins.list[Any]:
        entries = sftp.scandir(root)
        if inspect.isawaitable(entries):
            return builtins.list(await entries)
        return [entry async for entry in entries]

    async def _walk_files_client(self, sftp: Any, root: str) -> builtins.list[str]:
        files: list[str] = []
        entries = await self._scandir_client(sftp, root)
        for entry in entries:
            name = _entry_name(entry)
            if name in {".", ".."}:
                continue
            child = _join_remote(root, name)
            attrs = getattr(entry, "attrs", None)
            if attrs is None:
                attrs = await sftp.stat(child)
            if _is_dir(attrs):
                files.extend(await self._walk_files_client(sftp, child))
            else:
                files.append(child)
        return files


def _normalize_remote_path(path: str) -> str:
    value = str(path).replace("\\", "/").strip()
    if not value:
        raise ValidationError("remote path cannot be empty")
    normalized = PurePosixPath(value).as_posix()
    if ".." in PurePosixPath(normalized).parts:
        raise ValidationError("remote path cannot contain parent traversal")
    return normalized


def _parent(path: str) -> str:
    return PurePosixPath(path).parent.as_posix()


def _join_remote(root: str, name: str) -> str:
    if root in {"", "."}:
        return PurePosixPath(name).as_posix()
    return (PurePosixPath(root) / name).as_posix()


def _entry_name(entry: Any) -> str:
    return str(getattr(entry, "filename", entry))


def _mode(attrs: Any) -> int | None:
    value = getattr(attrs, "permissions", None)
    if value is None:
        value = getattr(attrs, "st_mode", None)
    return int(value) if value is not None else None


def _is_dir(attrs: Any) -> bool:
    mode = _mode(attrs)
    return stat_module.S_ISDIR(mode) if mode is not None else False


def _attrs_to_dict(attrs: Any) -> dict[str, Any]:
    mode = _mode(attrs)
    return {
        "size": getattr(attrs, "size", getattr(attrs, "st_size", None)),
        "permissions": mode,
        "mtime": getattr(attrs, "mtime", getattr(attrs, "st_mtime", None)),
        "is_dir": _is_dir(attrs),
    }


def _as_bytes(data: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    return bytes(data)
