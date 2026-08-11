from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from typing import Literal

NewlineKind = Literal["lf", "crlf", "mixed", "none"]


@dataclass(frozen=True)
class TextSnapshot:
    path: str
    text: str
    sha256: str
    size: int
    line_count: int
    newline: NewlineKind
    encoding: str = "utf-8"


@dataclass(frozen=True)
class TextDiff:
    path: str
    before_sha256: str
    after_sha256: str
    unified: str
    added_lines: int
    removed_lines: int
    changed: bool


def snapshot_text(path: str, text: str, *, encoding: str = "utf-8") -> TextSnapshot:
    data = text.encode(encoding)
    return TextSnapshot(
        path=path,
        text=text,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        line_count=_line_count(text),
        newline=_detect_newline(text),
        encoding=encoding,
    )


def make_unified_diff(
    path: str,
    before: TextSnapshot | str,
    after: TextSnapshot | str,
    *,
    context_lines: int = 3,
) -> TextDiff:
    before_snapshot = before if isinstance(before, TextSnapshot) else snapshot_text(path, before)
    after_snapshot = after if isinstance(after, TextSnapshot) else snapshot_text(path, after)
    before_lines = before_snapshot.text.splitlines()
    after_lines = after_snapshot.text.splitlines()
    unified_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context_lines,
            lineterm="",
        )
    )
    unified = "\n".join(unified_lines)
    if unified:
        unified += "\n"
    added, removed = _changed_line_counts(unified)
    return TextDiff(
        path=path,
        before_sha256=before_snapshot.sha256,
        after_sha256=after_snapshot.sha256,
        unified=unified,
        added_lines=added,
        removed_lines=removed,
        changed=before_snapshot.sha256 != after_snapshot.sha256,
    )


def summarize_diff(diff: TextDiff, *, max_chars: int = 8_000) -> str:
    if not diff.changed:
        return f"{diff.path}: no changes"
    header = (
        f"{diff.path}: +{diff.added_lines} -{diff.removed_lines} "
        f"({diff.before_sha256[:12]} -> {diff.after_sha256[:12]})"
    )
    if not diff.unified:
        return header
    body = diff.unified
    truncated = len(body) > max_chars
    if truncated:
        body = body[: max(0, max_chars - 25)].rstrip() + "\n...[diff truncated]"
    return f"{header}\n{body}"


def _line_count(text: str) -> int:
    if text == "":
        return 0
    return len(text.splitlines())


def _detect_newline(text: str) -> NewlineKind:
    crlf = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    lf = without_crlf.count("\n")
    cr = without_crlf.count("\r")
    if crlf == 0 and lf == 0 and cr == 0:
        return "none"
    kinds = sum(1 for count in (crlf, lf, cr) if count)
    if kinds > 1 or cr:
        return "mixed"
    return "crlf" if crlf else "lf"


def _changed_line_counts(unified: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in unified.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed
