from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class SnapshotSyncProvider:
    async def compute_revision(self, root: Path, exclude: set[str] | None = None) -> str:
        digest = hashlib.sha256()
        excluded = exclude or set()
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel in excluded:
                continue
            digest.update(rel.encode("utf-8"))
            digest.update(path.read_bytes())
        return f"revision_{digest.hexdigest()}"

    async def mirror(self, source: Path, target: Path) -> None:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
