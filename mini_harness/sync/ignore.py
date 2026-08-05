from __future__ import annotations

from fnmatch import fnmatchcase

DEFAULT_SYNC_IGNORE_PATTERNS = [
    ".git/**",
    ".mini-harness/**",
    ".mypy_cache/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".venv/**",
    "**/.venv/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "node_modules/**",
    "**/node_modules/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_*",
    ".ssh/**",
    "**/.ssh/**",
]


class SyncIgnoreMatcher:
    def __init__(self, patterns: list[str] | None = None) -> None:
        self.patterns = patterns or list(DEFAULT_SYNC_IGNORE_PATTERNS)

    def should_ignore(self, relative_path: str) -> bool:
        normalized = normalize_relative_path(relative_path)
        candidates = _path_candidates(normalized)
        return any(
            fnmatchcase(candidate, pattern) for pattern in self.patterns for candidate in candidates
        )


def normalize_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("sync paths cannot contain parent traversal")
    return "/".join(parts) or "."


def _path_candidates(path: str) -> list[str]:
    candidates = [path]
    if path != ".":
        candidates.append(f"./{path}")
        parts = path.split("/")
        candidates.extend(parts[:1])
        for index in range(1, len(parts)):
            candidates.append("/".join(parts[:index]))
    return candidates
