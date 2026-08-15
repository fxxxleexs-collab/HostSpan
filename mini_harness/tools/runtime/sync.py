from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel

from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.permissions import PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import WorkContext
from mini_harness.sync.engine import SyncEngine, SyncPushResult
from mini_harness.sync.errors import SyncConflictError
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.common import RuntimeTool
from mini_harness.tools.schemas import SyncPushInput, SyncStatusInput, ToolResult


class SyncStatusTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "sync_status",
            "Report local-to-remote sync manifest status and planned changes.",
            SyncStatusInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = SyncStatusInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="sync.status",
                target="remote",
                operation="status",
                resource=context.sync_remote_root(),
                metadata={"workspace_id": data.workspace_id},
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed
            if isinstance(parsed, SyncStatusInput)
            else SyncStatusInput.model_validate(parsed)
        )
        disabled = _sync_disabled_result(context)
        if disabled is not None:
            return disabled
        _require_push_sync_context(context)
        engine = _sync_engine(self.runtime, context, data.workspace_id)
        result = await asyncio.to_thread(engine.status)
        return _sync_tool_result(
            action="status",
            result=result,
            context=context,
            max_paths=data.max_paths,
        )


class SyncPushTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "sync_push",
            "Push local workspace changes to the configured remote mirror.",
            SyncPushInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = SyncPushInput.model_validate(arguments)
        return [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="sync.push",
                target="remote",
                operation="push",
                resource=context.sync_remote_root(),
                metadata={"workspace_id": data.workspace_id},
            )
        ]

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, SyncPushInput) else SyncPushInput.model_validate(parsed)
        disabled = _sync_disabled_result(context)
        if disabled is not None:
            return disabled
        _require_push_sync_context(context)
        engine = _sync_engine(self.runtime, context, data.workspace_id)
        try:
            result = await asyncio.to_thread(engine.push)
        except SyncConflictError as exc:
            status = await asyncio.to_thread(engine.status)
            return _sync_tool_result(
                action="push",
                result=status,
                context=context,
                max_paths=data.max_paths,
                ok=False,
                summary=f"sync push blocked by manifest conflicts: {exc}",
                error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
                recoverable=True,
            )
        return _sync_tool_result(
            action="push",
            result=result,
            context=context,
            max_paths=data.max_paths,
        )


def build_sync_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    return [
        SyncStatusTool(runtime),
        SyncPushTool(runtime),
    ]


def _sync_engine(
    runtime: HarnessRuntimeClient,
    context: WorkContext,
    workspace_id: str,
) -> SyncEngine:
    config = context.sync_config
    return SyncEngine(
        runtime=runtime,
        endpoint_id=context.endpoint_id,
        local_root=context.project_root,
        remote_root=context.sync_remote_root(),
        workspace_id=workspace_id,
        config=config,
    )


def _sync_disabled_result(context: WorkContext) -> ToolResult | None:
    config = context.sync_config
    if config is not None and config.enabled:
        return None
    return ToolResult(
        ok=False,
        summary="sync is disabled; enable [sync].enabled=true before using sync tools",
        error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
        recoverable=True,
        metadata={
            "sync_enabled": False,
            "recommended_config": {"sync.enabled": True, "sync.mode": "push"},
        },
    )


def _require_push_sync_context(context: WorkContext) -> None:
    config = context.sync_config
    if context.runtime_mode != "ssh":
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            "sync tools require ssh runtime mode because they manage a remote mirror",
            recoverable=True,
        )
    if config is not None and config.mode != "push":
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            f"sync_push currently supports mode=push, got mode={config.mode}",
            recoverable=True,
        )


def _sync_tool_result(
    *,
    action: Literal["status", "push"],
    result: SyncPushResult,
    context: WorkContext,
    max_paths: int,
    ok: bool | None = None,
    summary: str | None = None,
    error_code: str | None = None,
    recoverable: bool = False,
) -> ToolResult:
    diff = result.plan.diff_summary(max_paths)
    resolved_ok = result.ok if ok is None else ok
    state = _sync_state(action, result, resolved_ok)
    truncated_flags = diff.get("truncated", {})
    truncated = (
        any(bool(value) for value in truncated_flags.values())
        if isinstance(truncated_flags, dict)
        else False
    )
    return ToolResult(
        ok=resolved_ok,
        summary=summary or _sync_summary(action, result),
        content=_render_sync_diff(result, max_paths),
        resource_ref=f"sync:{result.workspace_id}",
        state=state,
        truncated=truncated,
        error_code=error_code,
        recoverable=recoverable or state in {"DIRTY", "CONFLICT"},
        metadata={
            "workspace_id": result.workspace_id,
            "sync_enabled": True,
            "runtime_mode": context.runtime_mode,
            "local_root": context.project_root,
            "remote_root": context.sync_remote_root(),
            "manifest_file_count": len(result.manifest.files),
            "local_state_path": result.local_state_path,
            "remote_manifest_path": result.remote_manifest_path,
            "uploaded": result.uploaded[:max_paths],
            "deleted": result.deleted[:max_paths],
            "diff": diff,
        },
    )


def _sync_state(action: Literal["status", "push"], result: SyncPushResult, ok: bool) -> str:
    if result.plan.conflicts:
        return "CONFLICT"
    if action == "push" and ok:
        return "CLEAN"
    if result.plan.has_changes:
        return "DIRTY"
    return "CLEAN"


def _sync_summary(action: Literal["status", "push"], result: SyncPushResult) -> str:
    plan = result.plan
    if plan.conflicts:
        return f"sync {action}: {len(plan.conflicts)} manifest conflict(s)"
    if action == "push":
        return f"sync push: uploaded {len(result.uploaded)} file(s), deleted {len(result.deleted)}"
    if plan.has_changes:
        return (
            f"sync status: {len(plan.uploads)} upload(s), "
            f"{len(plan.deletes)} delete(s), {len(plan.skipped)} skipped"
        )
    return f"sync status: clean, {len(plan.unchanged)} unchanged file(s)"


def _render_sync_diff(result: SyncPushResult, max_paths: int) -> str:
    plan = result.plan
    lines = [
        f"workspace_id: {result.workspace_id}",
        f"files: {len(result.manifest.files)}",
        (
            "diff: "
            f"uploads={len(plan.uploads)}, deletes={len(plan.deletes)}, "
            f"unchanged={len(plan.unchanged)}, skipped={len(plan.skipped)}, "
            f"conflicts={len(plan.conflicts)}"
        ),
    ]
    _append_sync_paths(lines, "uploads", [action.path for action in plan.uploads], max_paths)
    _append_sync_paths(lines, "deletes", [action.path for action in plan.deletes], max_paths)
    _append_sync_paths(
        lines,
        "conflicts",
        [f"{item.path} ({item.reason})" for item in plan.conflicts],
        max_paths,
    )
    _append_sync_paths(
        lines,
        "skipped",
        [
            f"{item.path} ({item.reason}{': ' + item.detail if item.detail else ''})"
            for item in plan.skipped
        ],
        max_paths,
    )
    if result.uploaded:
        _append_sync_paths(lines, "uploaded_now", result.uploaded, max_paths)
    if result.local_state_path:
        lines.append(f"local_state_path: {result.local_state_path}")
    if result.remote_manifest_path:
        lines.append(f"remote_manifest_path: {result.remote_manifest_path}")
    return "\n".join(lines)


def _append_sync_paths(lines: list[str], label: str, paths: list[str], max_paths: int) -> None:
    if not paths:
        return
    lines.append(f"{label}:")
    for path in paths[:max_paths]:
        lines.append(f"- {path}")
    if len(paths) > max_paths:
        lines.append(f"- ... {len(paths) - max_paths} more")


__all__ = [
    "SyncPushTool",
    "SyncStatusTool",
    "build_sync_tools",
]
