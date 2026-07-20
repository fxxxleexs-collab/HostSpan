from __future__ import annotations

import hashlib
from pathlib import Path


class LocalFilesystemProvider:
    async def ensure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    async def write_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    async def exists(self, path: Path) -> bool:
        return path.exists()

    async def walk_files(self, root: Path) -> list[Path]:
        return sorted([path for path in root.rglob("*") if path.is_file()])

    async def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
