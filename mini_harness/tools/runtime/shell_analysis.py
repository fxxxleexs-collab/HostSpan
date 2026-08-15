from __future__ import annotations

import re

from mini_harness.permissions import PermissionRequest
from mini_harness.runtime.work_context import ResolvedTerminalTarget


def _command_write_permission_requests(
    *,
    tool_name: str,
    target: ResolvedTerminalTarget,
    command: str,
    resource: str | None,
) -> list[PermissionRequest]:
    write_targets = _likely_written_paths(command)
    if not write_targets:
        return []
    return [
        PermissionRequest.for_target(
            tool_name=tool_name,
            capability="file.write",
            target=target,
            operation="shell_write",
            resource=", ".join(write_targets[:10]) or resource,
            argv=(command,),
            metadata={
                "detected_shell_write": True,
                "detected_paths": write_targets[:10],
                "path_count": len(write_targets),
            },
        )
    ]


def _likely_written_paths(command: str) -> list[str]:
    normalized = _normalize_terminal_text(command)
    if not normalized:
        return []
    paths: list[str] = []
    paths.extend(_redirection_write_paths(normalized))
    paths.extend(_simple_command_write_paths(normalized))
    return _unique_paths(paths)


def _normalize_terminal_text(value: str) -> str:
    without_ansi = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return without_ansi.replace("\r\n", "\n").replace("\r", "\n").strip()


def _redirection_write_paths(command: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"(?<![<])(?:^|[\s;|&])(?:\d*)>>?\s*(?P<path>[^\s;&|]+)", command):
        path = _clean_shell_path(match.group("path"))
        if path:
            paths.append(path)
    return paths


def _simple_command_write_paths(command: str) -> list[str]:
    paths: list[str] = []
    statements = re.split(r"[;\n]", command)
    for statement in statements:
        tokens = _shell_words(statement)
        if not tokens:
            continue
        command_name = tokens[0]
        if command_name == "sudo" and len(tokens) > 1:
            tokens = tokens[1:]
            command_name = tokens[0]
        if command_name in {"tee", "touch", "mkdir"}:
            paths.extend(_non_option_tokens(tokens[1:]))
        elif command_name in {"cp", "mv"}:
            candidates = _non_option_tokens(tokens[1:])
            if candidates:
                paths.append(candidates[-1])
    return paths


def _shell_words(statement: str) -> list[str]:
    return [
        _clean_shell_path(token)
        for token in re.findall(r"""(?:"[^"]*"|'[^']*'|[^\s]+)""", statement)
        if _clean_shell_path(token)
    ]


def _non_option_tokens(tokens: list[str]) -> list[str]:
    return [
        token
        for token in tokens
        if not token.startswith("-") and token not in {"|", ">", ">>", "2>", "2>>"}
    ]


def _clean_shell_path(value: str) -> str:
    token = value.strip().strip("'\"")
    if not token:
        return ""
    if token.startswith(("&", "$", "`", "<", "(")):
        return ""
    if token in {"-", "/dev/null"}:
        return ""
    return token


def _unique_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


__all__ = [
    "_command_write_permission_requests",
    "_likely_written_paths",
    "_normalize_terminal_text",
]
