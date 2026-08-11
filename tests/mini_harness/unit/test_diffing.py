from __future__ import annotations

import hashlib

from mini_harness.diffing import make_unified_diff, snapshot_text, summarize_diff


def test_snapshot_text_records_hash_size_lines_and_newline() -> None:
    text = "one\r\ntwo\r\n"

    snapshot = snapshot_text("notes.txt", text)

    assert snapshot.path == "notes.txt"
    assert snapshot.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert snapshot.size == len(text.encode("utf-8"))
    assert snapshot.line_count == 2
    assert snapshot.newline == "crlf"
    assert snapshot.encoding == "utf-8"


def test_snapshot_text_detects_mixed_and_empty_newlines() -> None:
    assert snapshot_text("empty.txt", "").newline == "none"
    assert snapshot_text("mixed.txt", "a\r\nb\nc\r").newline == "mixed"


def test_make_unified_diff_counts_changed_lines() -> None:
    before = snapshot_text("app.py", "a = 1\nb = 2\n")
    after = snapshot_text("app.py", "a = 1\nb = 3\nc = 4\n")

    diff = make_unified_diff("app.py", before, after)

    assert diff.changed
    assert diff.before_sha256 == before.sha256
    assert diff.after_sha256 == after.sha256
    assert diff.added_lines == 2
    assert diff.removed_lines == 1
    assert "--- a/app.py" in diff.unified
    assert "+++ b/app.py" in diff.unified
    assert "-b = 2" in diff.unified
    assert "+b = 3" in diff.unified
    assert "+c = 4" in diff.unified


def test_summarize_diff_truncates_large_diff() -> None:
    before = "a\n" * 20
    after = "b\n" * 20
    diff = make_unified_diff("large.txt", before, after, context_lines=20)

    summary = summarize_diff(diff, max_chars=120)

    assert "large.txt:" in summary
    assert "+20 -20" in summary
    assert "[diff truncated]" in summary


def test_summarize_diff_reports_no_changes() -> None:
    diff = make_unified_diff("same.txt", "same\n", "same\n")

    assert summarize_diff(diff) == "same.txt: no changes"
