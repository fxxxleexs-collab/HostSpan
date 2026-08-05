from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from mini_harness.sync.ignore import DEFAULT_SYNC_IGNORE_PATTERNS

SyncMode = Literal["push", "pull", "bidirectional"]


class SyncIgnoreConfig(BaseModel):
    patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_SYNC_IGNORE_PATTERNS))

    @field_validator("patterns")
    @classmethod
    def _valid_patterns(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip().replace("\\", "/")
            if not item:
                raise ValueError("sync ignore patterns cannot be empty")
            normalized.append(item)
        return normalized


class SyncConfig(BaseModel):
    enabled: bool = False
    mode: SyncMode = "push"
    delete_remote: bool = False
    max_file_bytes: int = Field(default=1_048_576, ge=1)
    text_only: bool = True
    local_state_dir: str = ".mini-harness/sync"
    remote_manifest_path: str = ".mini-harness/sync-manifest.json"
    ignore: SyncIgnoreConfig = Field(default_factory=SyncIgnoreConfig)

    @field_validator("local_state_dir", "remote_manifest_path")
    @classmethod
    def _valid_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        if not normalized:
            raise ValueError("sync paths cannot be empty")
        if normalized.startswith("/") or ".." in [part for part in normalized.split("/") if part]:
            raise ValueError("sync paths must be relative and stay inside the workspace")
        return normalized
