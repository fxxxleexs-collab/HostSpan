from __future__ import annotations


class SyncError(Exception):
    """Base class for sync-specific failures."""


class SyncConflictError(SyncError):
    """Raised when remote state changed since the last pushed manifest."""


class SyncUnsupportedFileError(SyncError):
    """Raised when a file cannot be represented by the current sync transport."""
