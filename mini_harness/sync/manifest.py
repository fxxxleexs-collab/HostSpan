from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from mini_harness.sync.config import SyncConfig
from mini_harness.sync.ignore import SyncIgnoreMatcher

SkipReason = Literal["ignored", "too_large", "binary", "not_file"]


class FileRecord(BaseModel):
    path: str
    sha256: str
    size: int
    mtime_ns: int


class SkippedFile(BaseModel):
    path: str
    reason: SkipReason
    detail: str | None = None


class SyncManifest(BaseModel):
    version: int = 1
    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    files: dict[str, FileRecord] = Field(default_factory=dict)

    def record(self, path: str) -> FileRecord | None:
        return self.files.get(path)


class ManifestScanResult(BaseModel):
    manifest: SyncManifest
    skipped: list[SkippedFile] = Field(default_factory=list)


def scan_local_manifest(root: str | Path, config: SyncConfig | None = None) -> ManifestScanResult:
    sync_config = config or SyncConfig()
    root_path = Path(root).resolve()
    matcher = SyncIgnoreMatcher(sync_config.ignore.patterns)
    manifest = SyncManifest()
    skipped: list[SkippedFile] = []

    for path in sorted(root_path.rglob("*")):
        if path == root_path:
            continue
        relative = path.relative_to(root_path).as_posix()
        if matcher.should_ignore(relative):
            skipped.append(SkippedFile(path=relative, reason="ignored"))
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            skipped.append(SkippedFile(path=relative, reason="not_file"))
            continue
        stat = path.stat()
        if stat.st_size > sync_config.max_file_bytes:
            skipped.append(
                SkippedFile(
                    path=relative,
                    reason="too_large",
                    detail=f"{stat.st_size} bytes exceeds {sync_config.max_file_bytes}",
                )
            )
            continue
        data = path.read_bytes()
        if sync_config.text_only and not _is_text_bytes(data):
            skipped.append(SkippedFile(path=relative, reason="binary"))
            continue
        digest = hashlib.sha256(data).hexdigest()
        manifest.files[relative] = FileRecord(
            path=relative,
            sha256=digest,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
    return ManifestScanResult(manifest=manifest, skipped=skipped)


def _is_text_bytes(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
