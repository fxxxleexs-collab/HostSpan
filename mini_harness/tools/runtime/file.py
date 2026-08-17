from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from mini_harness.approvals import ToolApprovalRequest
from mini_harness.config import AgentConfig
from mini_harness.diffing import (
    TextDiff,
    TextSnapshot,
    make_unified_diff,
    snapshot_text,
    summarize_diff,
)
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.file_ops import (
    LocalDiskWorkspaceFileOps,
    RuntimeWorkspaceFileOps,
    WorkspaceFileOps,
)
from mini_harness.permissions import PermissionDecision, PermissionRequest
from mini_harness.runtime.client import HarnessRuntimeClient
from mini_harness.runtime.work_context import WorkContext
from mini_harness.sync.engine import SyncEngine, SyncPushResult
from mini_harness.sync.errors import SyncConflictError
from mini_harness.tools.base import AgentTool
from mini_harness.tools.runtime.common import RuntimeTool
from mini_harness.tools.schemas import (
    EditFileInput,
    ListFilesInput,
    ReadFileInput,
    ToolResult,
    WriteFileInput,
)


@dataclass(frozen=True)
class PreparedTextChange:
    path: str
    content: str
    before_snapshot: TextSnapshot
    after_snapshot: TextSnapshot
    diff: TextDiff
    diff_summary: str
    expected_sha256: str | None
    expected_source: str | None
    existed_before: bool
    ignored_expected_sha256: str | None = None

    @property
    def hash_guarded(self) -> bool:
        return self.expected_sha256 is not None

    @property
    def unguarded_write(self) -> bool:
        return self.expected_sha256 is None


class ListFilesTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "list_files", "List project files through the runtime SDK.", ListFilesInput
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ListFilesInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        target = _file_target(data.target, context)
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="file.list",
                target="local" if target == "sync" else target,
                operation="list",
                resource=path,
            )
        ]
        if target == "sync":
            requests.append(
                PermissionRequest.for_target(
                    tool_name=self.definition.name,
                    capability="sync.status",
                    target="remote",
                    operation="status",
                    resource=context.sync_remote_root(),
                    metadata={"path": path},
                )
            )
        return requests

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed if isinstance(parsed, ListFilesInput) else ListFilesInput.model_validate(parsed)
        )
        path = context.normalize_path(data.path)
        target = _file_target(data.target, context)
        if target == "sync":
            unavailable = _sync_target_unavailable_result(context)
            if unavailable is not None:
                return unavailable
            entries = await asyncio.to_thread(_list_local_disk_files, context, path, data.recursive)
            sync_metadata = await asyncio.to_thread(_sync_status_metadata, self.runtime, context)
        else:
            binding = context.terminal_target(target)
            entries = await asyncio.to_thread(
                self.runtime.list_files,
                binding.endpoint_id,
                context.runtime_path_for(path, binding.location),
                data.recursive,
            )
            sync_metadata = None
        visible = sorted(
            _display_path_for_target(context, entry, target)
            for entry in entries
            if not context.should_ignore_entry(_display_path_for_target(context, entry, target))
        )
        truncated = len(visible) > data.max_entries
        visible = visible[: data.max_entries]
        metadata: dict[str, Any] = {
            "entry_count": len(visible),
            "path": path,
            "target": target,
        }
        if sync_metadata is not None:
            metadata["sync"] = sync_metadata
        return ToolResult(
            ok=True,
            summary=_list_files_summary(len(visible), path, target),
            content="\n".join(visible),
            truncated=truncated,
            metadata=metadata,
        )


class ReadFileTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient, max_chars: int = 40_000) -> None:
        super().__init__(
            runtime, "read_file", "Read a text file through the runtime SDK.", ReadFileInput
        )
        self.max_chars = max_chars

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = ReadFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        target = _file_target(data.target, context)
        requests = [
            PermissionRequest.for_target(
                tool_name=self.definition.name,
                capability="file.read",
                target="local" if target == "sync" else target,
                operation="read",
                resource=path,
            )
        ]
        if target == "sync":
            requests.append(
                PermissionRequest.for_target(
                    tool_name=self.definition.name,
                    capability="sync.status",
                    target="remote",
                    operation="status",
                    resource=context.sync_remote_root(),
                    metadata={"path": path},
                )
            )
        return requests

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, ReadFileInput) else ReadFileInput.model_validate(parsed)
        path = context.normalize_path(data.path)
        if data.end_line is not None and data.max_lines is not None:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                "end_line and max_lines cannot be used together",
                recoverable=True,
            )
        if data.start_line and data.end_line and data.end_line < data.start_line:
            raise MiniHarnessError(
                ErrorCode.TOOL_ARGUMENT_INVALID,
                "end_line must be greater than or equal to start_line",
                recoverable=True,
            )
        target = _file_target(data.target, context)
        if target == "sync":
            unavailable = _sync_target_unavailable_result(context)
            if unavailable is not None:
                return unavailable
            file_ops = LocalDiskWorkspaceFileOps(context)
            sync_metadata = await asyncio.to_thread(
                _sync_status_metadata,
                self.runtime,
                context,
                path,
            )
        else:
            file_ops = RuntimeWorkspaceFileOps(self.runtime, context, target=target)
            sync_metadata = None
        read = await asyncio.to_thread(file_ops.read_text, path)
        snapshot = snapshot_text(read.location.path, read.text)
        snapshot_summary = context.record_file_snapshot(snapshot)
        lines = read.text.splitlines()
        start_index = (data.start_line - 1) if data.start_line else 0
        if data.max_lines is not None:
            end_index = min(len(lines), start_index + data.max_lines)
        else:
            end_index = data.end_line if data.end_line else len(lines)
        selected = lines[start_index:end_index]
        selected_start_line = start_index + 1 if selected else None
        selected_end_line = end_index if selected else None
        has_more = end_index < len(lines)
        next_start_line = end_index + 1 if has_more else None
        rendered = "\n".join(
            f"{line_no} | {line}" for line_no, line in enumerate(selected, start=start_index + 1)
        )
        truncated = len(rendered) > self.max_chars
        if truncated:
            rendered = rendered[: self.max_chars] + "\n[truncated]"
        result = ToolResult(
            ok=True,
            summary=_read_file_summary(path, len(selected), selected_start_line, selected_end_line),
            content=rendered,
            resource_ref=f"file:{path}",
            truncated=truncated,
            metadata={
                "path": path,
                "sha256": snapshot.sha256,
                "size": snapshot.size,
                "line_count": snapshot.line_count,
                "selected_line_count": len(selected),
                "start_line": selected_start_line,
                "end_line": selected_end_line,
                "requested_start_line": data.start_line,
                "requested_end_line": data.end_line,
                "requested_max_lines": data.max_lines,
                "has_more": has_more,
                "next_start_line": next_start_line,
                "newline": snapshot.newline,
                "encoding": snapshot.encoding,
                "snapshot": snapshot_summary.as_dict(),
                "file_location": read.location.as_dict(),
                "target": target,
            },
        )
        if sync_metadata is not None:
            result.metadata["sync"] = sync_metadata
        return result


class WriteFileTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime, "write_file", "Overwrite a text file through the runtime SDK.", WriteFileInput
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = WriteFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        target = _write_target(data.target, context)
        return _file_change_permission_requests(
            tool_name=self.definition.name,
            context=context,
            path=path,
            target=target,
            operation="write",
        )

    async def approval_preview(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
        permission_requests: list[PermissionRequest],
        config: AgentConfig,
    ) -> ToolApprovalRequest | ToolResult | None:
        data = WriteFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        target = _write_target(data.target, context)
        prepared = await asyncio.to_thread(
            _prepare_text_change,
            file_ops=_write_prepare_file_ops(self.runtime, context, target),
            context=context,
            path=path,
            new_content=data.content,
            expected_sha256=data.expected_sha256,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        if prepared.unguarded_write and not config.allow_unguarded_write:
            return _unguarded_write_denied_result(self.definition.name, prepared)
        return _file_change_approval_request(
            tool_name=self.definition.name,
            arguments=arguments,
            permission_requests=permission_requests,
            prepared=prepared,
            prefer_edit=prepared.existed_before,
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = (
            parsed if isinstance(parsed, WriteFileInput) else WriteFileInput.model_validate(parsed)
        )
        path = context.normalize_path(data.path)
        target = _write_target(data.target, context)
        prepared = await asyncio.to_thread(
            _prepare_text_change,
            file_ops=_write_prepare_file_ops(self.runtime, context, target),
            context=context,
            path=path,
            new_content=data.content,
            expected_sha256=data.expected_sha256,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        if target == "sync":
            return await asyncio.to_thread(
                _execute_sync_write,
                runtime=self.runtime,
                context=context,
                path=path,
                content=data.content,
                prepared=prepared,
                operation="write",
            )
        file_ops = RuntimeWorkspaceFileOps(self.runtime, context, target=target)
        write = await asyncio.to_thread(file_ops.write_text, path, data.content)
        written_summary = context.record_file_snapshot(prepared.after_snapshot)
        context.record_runtime_transition(
            kind="file",
            action="write",
            ref=f"file:{path}",
            summary=f"{target} write completed for {path}",
            state="OK",
            active_after=None,
        )
        return ToolResult(
            ok=True,
            summary=_write_file_summary(
                path,
                write.size,
                prepared.diff.changed,
                prepared.diff.added_lines,
                prepared.diff.removed_lines,
            ),
            content=prepared.diff_summary,
            resource_ref=f"file:{path}",
            metadata={
                "path": path,
                "target": target,
                "size": write.size,
                **_prepared_change_metadata(prepared),
                "parent_directory": write.parent_directory.path,
                "parent_directory_ensured": write.parent_directory.ensured,
                "parent_directory_result": write.parent_directory.as_dict(),
                "file_location": write.location.as_dict(),
                "snapshot": written_summary.as_dict(),
            },
        )


class EditFileTool(RuntimeTool):
    def __init__(self, runtime: HarnessRuntimeClient) -> None:
        super().__init__(
            runtime,
            "edit_file",
            "Edit a text file by replacing exact old_text with new_text.",
            EditFileInput,
        )

    def permission_requests(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
    ) -> list[PermissionRequest]:
        data = EditFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        target = _write_target(data.target, context)
        return _file_change_permission_requests(
            tool_name=self.definition.name,
            context=context,
            path=path,
            target=target,
            operation="edit",
        )

    async def approval_preview(
        self,
        arguments: dict[str, Any],
        context: WorkContext,
        permission_requests: list[PermissionRequest],
        config: AgentConfig,
    ) -> ToolApprovalRequest | ToolResult | None:
        data = EditFileInput.model_validate(arguments)
        path = context.normalize_path(data.path)
        target = _write_target(data.target, context)
        prepared = await asyncio.to_thread(
            _prepare_edit_change,
            file_ops=_write_prepare_file_ops(self.runtime, context, target),
            context=context,
            path=path,
            old_text=data.old_text,
            new_text=data.new_text,
            expected_sha256=data.expected_sha256,
            replace_all=data.replace_all,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        if prepared.unguarded_write and not config.allow_unguarded_write:
            return _unguarded_write_denied_result(self.definition.name, prepared)
        return _file_change_approval_request(
            tool_name=self.definition.name,
            arguments=arguments,
            permission_requests=permission_requests,
            prepared=prepared,
            prefer_edit=False,
        )

    async def _execute(self, parsed: BaseModel, context: WorkContext) -> ToolResult:
        data = parsed if isinstance(parsed, EditFileInput) else EditFileInput.model_validate(parsed)
        path = context.normalize_path(data.path)
        target = _write_target(data.target, context)
        prepared = await asyncio.to_thread(
            _prepare_edit_change,
            file_ops=_write_prepare_file_ops(self.runtime, context, target),
            context=context,
            path=path,
            old_text=data.old_text,
            new_text=data.new_text,
            expected_sha256=data.expected_sha256,
            replace_all=data.replace_all,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        if target == "sync":
            return await asyncio.to_thread(
                _execute_sync_write,
                runtime=self.runtime,
                context=context,
                path=path,
                content=prepared.content,
                prepared=prepared,
                operation="edit",
            )
        file_ops = RuntimeWorkspaceFileOps(self.runtime, context, target=target)
        write = await asyncio.to_thread(file_ops.write_text, path, prepared.content)
        written_summary = context.record_file_snapshot(prepared.after_snapshot)
        context.record_runtime_transition(
            kind="file",
            action="edit",
            ref=f"file:{path}",
            summary=f"{target} edit completed for {path}",
            state="OK",
            active_after=None,
        )
        return ToolResult(
            ok=True,
            summary=_write_file_summary(
                path,
                write.size,
                prepared.diff.changed,
                prepared.diff.added_lines,
                prepared.diff.removed_lines,
            ),
            content=prepared.diff_summary,
            resource_ref=f"file:{path}",
            metadata={
                "path": path,
                "target": target,
                "size": write.size,
                **_prepared_change_metadata(prepared),
                "replace_all": data.replace_all,
                "parent_directory": write.parent_directory.path,
                "parent_directory_ensured": write.parent_directory.ensured,
                "parent_directory_result": write.parent_directory.as_dict(),
                "file_location": write.location.as_dict(),
                "snapshot": written_summary.as_dict(),
            },
        )


def build_file_tools(runtime: HarnessRuntimeClient) -> list[AgentTool]:
    return [
        ListFilesTool(runtime),
        ReadFileTool(runtime),
        WriteFileTool(runtime),
        EditFileTool(runtime),
    ]


FileTarget = Literal["local", "remote", "sync"]
WriteTarget = FileTarget
FileChangeOperation = Literal["write", "edit"]


def _file_target(raw_target: str, context: WorkContext) -> FileTarget:
    if raw_target == "sync":
        return "sync"
    if raw_target == "current":
        return context.default_terminal_target()
    if raw_target in {"local", "remote"}:
        return raw_target  # type: ignore[return-value]
    raise MiniHarnessError(
        ErrorCode.TOOL_ARGUMENT_INVALID,
        f"unsupported file target: {raw_target}",
        recoverable=True,
    )


def _write_target(raw_target: str, context: WorkContext) -> WriteTarget:
    return _file_target(raw_target, context)


def _display_path_for_target(context: WorkContext, runtime_path: str, target: FileTarget) -> str:
    if target == "remote":
        return context.display_path(runtime_path)
    return runtime_path.replace("\\", "/")


def _list_files_summary(count: int, path: str, target: FileTarget) -> str:
    if target == "sync":
        return f"{count} local sync workspace entries listed from {path}"
    return f"{count} {target} entries listed from {path}"


def _list_local_disk_files(context: WorkContext, path: str, recursive: bool) -> list[str]:
    root = Path(context.project_root).resolve()
    base = root if path == "." else (root / path).resolve()
    if not base.is_relative_to(root):
        raise MiniHarnessError(
            ErrorCode.PATH_OUTSIDE_PROJECT,
            "paths must stay inside the project root",
            recoverable=True,
        )
    if recursive:
        if base.is_file():
            return [base.name]
        return sorted(item.relative_to(base).as_posix() for item in base.rglob("*") if item.is_file())
    return sorted(item.name for item in base.iterdir())


def _sync_status_metadata(
    runtime: HarnessRuntimeClient,
    context: WorkContext,
    path: str | None = None,
) -> dict[str, Any]:
    result = _sync_engine(runtime, context).status()
    state = _sync_semantic_state(result)
    diff = result.plan.diff_summary(50)
    metadata: dict[str, Any] = {
        "ok": result.ok,
        "state": state,
        "semantic_state": state,
        "workspace_id": result.workspace_id,
        "local_root": context.project_root,
        "remote_root": context.sync_remote_root(),
        "manifest_file_count": len(result.manifest.files),
        "diff": diff,
        "retryable": state == "LOCAL_AHEAD",
        "recommended_action": _sync_read_recommended_action(state),
    }
    if path is not None:
        metadata["path_status"] = _sync_path_status(result, path)
    return metadata


def _sync_semantic_state(result: SyncPushResult) -> str:
    if result.plan.conflicts:
        return "CONFLICT"
    if result.plan.has_changes:
        return "LOCAL_AHEAD"
    return "IN_SYNC"


def _sync_path_status(result: SyncPushResult, path: str) -> dict[str, object]:
    normalized = path.replace("\\", "/").strip("/")
    if any(item.path == normalized for item in result.plan.conflicts):
        return {"state": "CONFLICT", "path": normalized}
    skipped = next((item for item in result.plan.skipped if item.path == normalized), None)
    if skipped is not None:
        return {
            "state": "SKIPPED",
            "path": normalized,
            "reason": skipped.reason,
            "detail": skipped.detail,
        }
    if any(item.path == normalized for item in result.plan.uploads):
        return {"state": "LOCAL_AHEAD", "path": normalized}
    if normalized in result.plan.unchanged:
        return {"state": "IN_SYNC", "path": normalized}
    if normalized in result.manifest.files:
        return {"state": "TRACKED", "path": normalized}
    return {"state": "UNTRACKED", "path": normalized}


def _sync_read_recommended_action(state: str) -> str | None:
    if state == "LOCAL_AHEAD":
        return 'sync action="push" before remote commands that need these local files'
    if state == "CONFLICT":
        return 'sync action="status" and inspect conflicts before relying on the mirror'
    return None


def _write_prepare_file_ops(
    runtime: HarnessRuntimeClient,
    context: WorkContext,
    target: WriteTarget,
) -> WorkspaceFileOps:
    if target == "sync":
        _require_sync_write_context(context)
        return LocalDiskWorkspaceFileOps(context)
    return RuntimeWorkspaceFileOps(runtime, context, target=target)


def _file_change_permission_requests(
    *,
    tool_name: str,
    context: WorkContext,
    path: str,
    target: WriteTarget,
    operation: FileChangeOperation,
) -> list[PermissionRequest]:
    if target == "sync":
        return [
            PermissionRequest.for_target(
                tool_name=tool_name,
                capability="file.write",
                target="local",
                operation=operation,
                resource=path,
            ),
            PermissionRequest.for_target(
                tool_name=tool_name,
                capability="file.write",
                target="remote",
                operation=f"sync_{operation}",
                resource=f"{context.sync_remote_root().rstrip('/')}/{path}",
            ),
            PermissionRequest.for_target(
                tool_name=tool_name,
                capability="sync.push",
                target="remote",
                operation="push_file",
                resource=context.sync_remote_root(),
                metadata={"path": path, "file_operation": operation},
            ),
        ]
    return [
        PermissionRequest.for_target(
            tool_name=tool_name,
            capability="file.write",
            target=target,
            operation=operation,
            resource=path,
        )
    ]


def _execute_sync_write(
    *,
    runtime: HarnessRuntimeClient,
    context: WorkContext,
    path: str,
    content: str,
    prepared: PreparedTextChange,
    operation: FileChangeOperation,
) -> ToolResult:
    unavailable = _sync_write_unavailable_result(context)
    if unavailable is not None:
        return unavailable
    local_ops = LocalDiskWorkspaceFileOps(context)
    local_write = local_ops.write_text(path, content)
    written_summary = context.record_file_snapshot(prepared.after_snapshot)
    try:
        sync_result = _sync_engine(runtime, context).push_file(path)
    except SyncConflictError as exc:
        context.record_runtime_transition(
            kind="file",
            action=f"sync_{operation}",
            ref=f"file:{path}",
            summary=f"local write completed but remote sync hit conflicts for {path}: {exc}",
            state="CONFLICT",
            active_after="local_ahead",
        )
        return _sync_write_result(
            path=path,
            prepared=prepared,
            local_write=local_write,
            written_snapshot=written_summary.as_dict(),
            remote_endpoint_id=context.endpoint_id,
            remote_root=context.sync_remote_root(),
            sync_result=None,
            ok=False,
            summary=f"wrote local file {path}, but remote sync is blocked by conflicts",
            state="CONFLICT",
            operation=operation,
            failure_phase="conflict_check",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=True,
            remote_error=str(exc),
        )
    except Exception as exc:
        context.record_runtime_transition(
            kind="file",
            action=f"sync_{operation}",
            ref=f"file:{path}",
            summary=f"local write completed but remote sync failed for {path}: {exc}",
            state="LOCAL_AHEAD",
            active_after="local_ahead",
        )
        return _sync_write_result(
            path=path,
            prepared=prepared,
            local_write=local_write,
            written_snapshot=written_summary.as_dict(),
            remote_endpoint_id=context.endpoint_id,
            remote_root=context.sync_remote_root(),
            sync_result=None,
            ok=False,
            summary=f"wrote local file {path}, but remote sync failed: {exc}",
            state="LOCAL_AHEAD",
            operation=operation,
            failure_phase="remote_push",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=True,
            remote_error=str(exc),
        )
    if not sync_result.ok:
        context.record_runtime_transition(
            kind="file",
            action=f"sync_{operation}",
            ref=f"file:{path}",
            summary=(
                f"local write completed but {path} was not pushed "
                f"({sync_result.skipped_reason or 'sync rejected it'})"
            ),
            state="LOCAL_AHEAD",
            active_after="local_ahead",
        )
        return _sync_write_result(
            path=path,
            prepared=prepared,
            local_write=local_write,
            written_snapshot=written_summary.as_dict(),
            remote_endpoint_id=context.endpoint_id,
            remote_root=context.sync_remote_root(),
            sync_result=sync_result,
            ok=False,
            summary=(
                f"wrote local file {path}, but remote sync did not upload it "
                f"({sync_result.skipped_reason or 'not accepted by manifest'})"
            ),
            state="LOCAL_AHEAD",
            operation=operation,
            failure_phase="manifest_scan",
            error_code=ErrorCode.RUNTIME_OPERATION_FAILED.value,
            recoverable=_sync_failure_retryable(sync_result.skipped_reason),
            remote_error=sync_result.skipped_reason,
        )
    state = "IN_SYNC"
    context.record_runtime_transition(
        kind="file",
        action=f"sync_{operation}",
        ref=f"file:{path}",
        summary=f"wrote local file {path} and synced remote mirror",
        state=state,
        active_after="in_sync",
    )
    return _sync_write_result(
        path=path,
        prepared=prepared,
        local_write=local_write,
        written_snapshot=written_summary.as_dict(),
        remote_endpoint_id=context.endpoint_id,
        remote_root=context.sync_remote_root(),
        sync_result=sync_result,
        ok=True,
        summary=_sync_write_summary(path, local_write.size, prepared, sync_result),
        state=state,
        operation=operation,
    )


def _sync_write_result(
    *,
    path: str,
    prepared: PreparedTextChange,
    local_write: Any,
    written_snapshot: dict[str, Any],
    remote_endpoint_id: str,
    remote_root: str,
    sync_result: SyncPushResult | None,
    ok: bool,
    summary: str,
    state: str,
    operation: FileChangeOperation,
    failure_phase: str | None = None,
    error_code: str | None = None,
    recoverable: bool = False,
    remote_error: str | None = None,
) -> ToolResult:
    uploaded = sync_result.uploaded if sync_result is not None else []
    remote_manifest_path = sync_result.remote_manifest_path if sync_result is not None else None
    local_state_path = sync_result.local_state_path if sync_result is not None else None
    sync_diff = sync_result.plan.diff_summary(50) if sync_result is not None else None
    return ToolResult(
        ok=ok,
        summary=summary,
        content=prepared.diff_summary,
        resource_ref=f"file:{path}",
        state=state,
        error_code=error_code,
        recoverable=recoverable,
        metadata={
            "path": path,
            "target": "sync",
            "operation": operation,
            "atomic_file_operation": True,
            "size": local_write.size,
            **_prepared_change_metadata(prepared),
            "local": {
                "ok": True,
                "path": local_write.location.path,
                "runtime_path": local_write.location.runtime_path,
                "endpoint_id": local_write.location.endpoint_id,
                "size": local_write.size,
                "sha256": prepared.after_snapshot.sha256,
                "parent_directory": local_write.parent_directory.as_dict(),
            },
            "remote": {
                "ok": bool(sync_result and sync_result.ok),
                "endpoint_id": remote_endpoint_id,
                "remote_root": remote_root,
                "remote_manifest_path": remote_manifest_path,
                "uploaded": uploaded,
                "error": remote_error,
            },
            "sync": {
                "ok": bool(sync_result and sync_result.ok),
                "state": state,
                "semantic_state": state,
                "workspace_id": sync_result.workspace_id if sync_result is not None else "default",
                "local_state_path": local_state_path,
                "remote_manifest_path": remote_manifest_path,
                "diff": sync_diff,
                "skipped_reason": sync_result.skipped_reason if sync_result is not None else None,
                "failure": _sync_failure_metadata(
                    phase=failure_phase,
                    message=remote_error,
                    skipped_reason=sync_result.skipped_reason if sync_result is not None else None,
                    retryable=recoverable,
                ),
                "retryable": recoverable,
                "recommended_action": _sync_failure_recommended_action(
                    state=state,
                    phase=failure_phase,
                    retryable=recoverable,
                )
                if not ok
                else None,
            },
            "snapshot": written_snapshot,
        },
    )


def _sync_write_summary(
    path: str,
    size: int,
    prepared: PreparedTextChange,
    sync_result: SyncPushResult,
) -> str:
    upload_part = "uploaded remote mirror" if path in sync_result.uploaded else "remote mirror already current"
    if not prepared.diff.changed:
        return f"wrote {size} bytes to local {path}; no content changes; {upload_part}"
    return (
        f"wrote {size} bytes to local {path}; diff +{prepared.diff.added_lines} "
        f"-{prepared.diff.removed_lines}; {upload_part}"
    )


def _sync_failure_retryable(skipped_reason: str | None) -> bool:
    return skipped_reason not in {"ignored", "too_large", "binary", "not_file", "not_in_manifest"}


def _sync_failure_metadata(
    *,
    phase: str | None,
    message: str | None,
    skipped_reason: str | None,
    retryable: bool,
) -> dict[str, object] | None:
    if phase is None and message is None and skipped_reason is None:
        return None
    return {
        "phase": phase or "unknown",
        "message": message,
        "skipped_reason": skipped_reason,
        "retryable": retryable,
    }


def _sync_failure_recommended_action(
    *,
    state: str,
    phase: str | None,
    retryable: bool,
) -> str:
    if state == "CONFLICT" or phase == "conflict_check":
        return 'sync action="status"'
    if retryable:
        return 'sync action="push"'
    return "adjust sync ignore/config or use file write target=\"remote\" intentionally"


def _sync_engine(runtime: HarnessRuntimeClient, context: WorkContext) -> SyncEngine:
    return SyncEngine(
        runtime=runtime,
        endpoint_id=context.endpoint_id,
        local_root=context.project_root,
        remote_root=context.sync_remote_root(),
        workspace_id="default",
        config=context.sync_config,
    )


def _require_sync_write_context(context: WorkContext) -> None:
    unavailable = _sync_target_unavailable_result(context)
    if unavailable is not None:
        raise MiniHarnessError(
            ErrorCode.TOOL_ARGUMENT_INVALID,
            unavailable.summary,
            recoverable=True,
            metadata=unavailable.metadata,
        )


def _sync_write_unavailable_result(context: WorkContext) -> ToolResult | None:
    return _sync_target_unavailable_result(context)


def _sync_target_unavailable_result(context: WorkContext) -> ToolResult | None:
    config = context.sync_config
    if config is None or not config.enabled:
        return ToolResult(
            ok=False,
            summary="target=sync requires [sync].enabled=true",
            error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
            recoverable=True,
            metadata={
                "target": "sync",
                "sync_enabled": False,
                "recommended_config": {"sync.enabled": True, "sync.mode": "push"},
            },
        )
    if context.runtime_mode != "ssh" or context.remote_target() is None:
        return ToolResult(
            ok=False,
            summary="target=sync requires an active SSH remote runtime",
            error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
            recoverable=True,
            metadata={"target": "sync", "runtime_mode": context.runtime_mode},
        )
    if config.mode != "push":
        return ToolResult(
            ok=False,
            summary=f"target=sync currently supports sync.mode=push, got {config.mode}",
            error_code=ErrorCode.TOOL_ARGUMENT_INVALID.value,
            recoverable=True,
            metadata={"target": "sync", "sync_mode": config.mode},
        )
    return None


def _read_file_summary(
    path: str,
    selected_count: int,
    start_line: int | None,
    end_line: int | None,
) -> str:
    if selected_count == 0:
        return f"0 lines read from {path}"
    if start_line == end_line:
        return f"1 line read from {path} at line {start_line}"
    return f"{selected_count} lines read from {path} lines {start_line}-{end_line}"


def _write_file_summary(
    path: str,
    size: int,
    changed: bool,
    added_lines: int,
    removed_lines: int,
) -> str:
    if not changed:
        return f"wrote {size} bytes to {path}; no content changes"
    return f"wrote {size} bytes to {path}; diff +{added_lines} -{removed_lines}"


def _prepare_text_change(
    *,
    file_ops: WorkspaceFileOps,
    context: WorkContext,
    path: str,
    new_content: str,
    expected_sha256: str | None,
) -> PreparedTextChange | ToolResult:
    current_text, existed_before = _read_text_for_write(file_ops, path)
    return _prepare_text_change_from_current(
        context=context,
        path=path,
        current_text=current_text,
        new_content=new_content,
        expected_sha256=expected_sha256,
        existed_before=existed_before,
    )


def _prepare_edit_change(
    *,
    file_ops: WorkspaceFileOps,
    context: WorkContext,
    path: str,
    old_text: str,
    new_text: str,
    expected_sha256: str | None,
    replace_all: bool,
) -> PreparedTextChange | ToolResult:
    current_text, existed_before = _read_text_for_write(file_ops, path)
    if not existed_before:
        return ToolResult(
            ok=False,
            summary=f"cannot edit missing file: {path}",
            resource_ref=f"file:{path}",
            error_code="EDIT_CONTEXT_NOT_FOUND",
            recoverable=True,
            metadata={"path": path, "recommended_action": _tool_action_label("read_file")},
        )
    count = current_text.count(old_text)
    if count == 0:
        return ToolResult(
            ok=False,
            summary=f"edit context was not found in {path}; reread the file before editing",
            resource_ref=f"file:{path}",
            error_code="EDIT_CONTEXT_NOT_FOUND",
            recoverable=True,
            metadata={
                "path": path,
                "match_count": count,
                "recommended_action": _tool_action_label("read_file"),
            },
        )
    if count > 1 and not replace_all:
        return ToolResult(
            ok=False,
            summary=(
                f"edit context matched {count} locations in {path}; provide more context "
                "or set replace_all=true"
            ),
            resource_ref=f"file:{path}",
            error_code="EDIT_CONTEXT_AMBIGUOUS",
            recoverable=True,
            metadata={"path": path, "match_count": count},
        )
    replacement_count = count if replace_all else 1
    new_content = current_text.replace(old_text, new_text, replacement_count)
    return _prepare_text_change_from_current(
        context=context,
        path=path,
        current_text=current_text,
        new_content=new_content,
        expected_sha256=expected_sha256,
        existed_before=True,
    )


def _prepare_text_change_from_current(
    *,
    context: WorkContext,
    path: str,
    current_text: str,
    new_content: str,
    expected_sha256: str | None,
    existed_before: bool,
) -> PreparedTextChange | ToolResult:
    before_snapshot = snapshot_text(path, current_text)
    cached_snapshot = context.file_snapshot(path)
    ignored_expected_sha256: str | None = None
    expected_source = "argument" if expected_sha256 else None
    if expected_sha256 is None and cached_snapshot is not None:
        expected_sha256 = cached_snapshot.sha256
        expected_source = "recent_read_snapshot"
    elif (
        expected_sha256 is not None
        and before_snapshot.sha256 != expected_sha256
        and cached_snapshot is not None
        and before_snapshot.sha256 == cached_snapshot.sha256
    ):
        ignored_expected_sha256 = expected_sha256
        expected_sha256 = cached_snapshot.sha256
        expected_source = "recent_read_snapshot"
    if expected_sha256 is not None and before_snapshot.sha256 != expected_sha256:
        return ToolResult(
            ok=False,
            summary=f"file changed since it was read: {path}; reread the file before writing",
            resource_ref=f"file:{path}",
            error_code=ErrorCode.FILE_CHANGED.value,
            recoverable=True,
            metadata={
                "path": path,
                "expected_sha256": expected_sha256,
                "actual_sha256": before_snapshot.sha256,
                "expected_source": expected_source,
                "existed_before": existed_before,
                "recommended_action": _tool_action_label("read_file"),
            },
        )
    after_snapshot = snapshot_text(path, new_content)
    diff = make_unified_diff(path, before_snapshot, after_snapshot)
    return PreparedTextChange(
        path=path,
        content=new_content,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        diff=diff,
        diff_summary=summarize_diff(diff),
        expected_sha256=expected_sha256,
        expected_source=expected_source,
        existed_before=existed_before,
        ignored_expected_sha256=ignored_expected_sha256,
    )


def _prepared_change_metadata(prepared: PreparedTextChange) -> dict[str, Any]:
    return {
        "expected_sha256": prepared.expected_sha256,
        "expected_source": prepared.expected_source,
        "ignored_expected_sha256": prepared.ignored_expected_sha256,
        "hash_guarded": prepared.hash_guarded,
        "unguarded_write": prepared.unguarded_write,
        "existed_before": prepared.existed_before,
        "before_sha256": prepared.before_snapshot.sha256,
        "after_sha256": prepared.after_snapshot.sha256,
        "sha256": prepared.after_snapshot.sha256,
        "line_count": prepared.after_snapshot.line_count,
        "newline": prepared.after_snapshot.newline,
        "encoding": prepared.after_snapshot.encoding,
        "diff": {
            "changed": prepared.diff.changed,
            "added_lines": prepared.diff.added_lines,
            "removed_lines": prepared.diff.removed_lines,
            "unified": prepared.diff.unified,
            "summary": prepared.diff_summary,
        },
    }


def _unguarded_write_denied_result(tool_name: str, prepared: PreparedTextChange) -> ToolResult:
    read_action = _tool_action_label("read_file")
    return ToolResult(
        ok=False,
        summary=(
            f"unguarded write is disabled for {prepared.path}; use {read_action} first "
            "or provide expected_sha256"
        ),
        resource_ref=f"file:{prepared.path}",
        error_code=ErrorCode.PERMISSION_DENIED.value,
        recoverable=True,
        metadata={
            "tool_name": tool_name,
            "path": prepared.path,
            "unguarded_write": True,
            "recommended_action": _tool_action_label("read_file"),
        },
    )


def _file_change_approval_request(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    permission_requests: list[PermissionRequest],
    prepared: PreparedTextChange,
    prefer_edit: bool,
) -> ToolApprovalRequest:
    display_tool = _tool_action_label(tool_name)
    risks: list[str] = []
    if prepared.unguarded_write:
        risks.append("This write has no expected_sha256 guard.")
    if prefer_edit:
        risks.append(
            "This overwrites an existing file; prefer file action=\"edit\" for small changes."
        )
    if prepared.diff.added_lines + prepared.diff.removed_lines > 200:
        risks.append("This is a large diff; review carefully before approving.")
    if not risks:
        risks.append("This operation will modify file contents.")
    return ToolApprovalRequest(
        tool_name=tool_name,
        arguments=arguments,
        decision=PermissionDecision.deny(
            f"{display_tool} will modify {prepared.path}",
            missing_capabilities=tuple(request.capability_key for request in permission_requests),
            metadata={
                "warning": _file_change_warning(prepared, prefer_edit=prefer_edit),
                "risks": risks,
            },
        ),
        permission_requests=permission_requests,
        preview_kind="diff",
        preview_title=f"Diff preview for {prepared.path}",
        preview_body=prepared.diff_summary,
    )


def _file_change_warning(prepared: PreparedTextChange, *, prefer_edit: bool) -> str:
    if prepared.unguarded_write:
        return "This file write is not protected by an expected hash."
    if prefer_edit:
        return "This overwrites an existing file; file action=\"edit\" is safer for targeted changes."
    return "Review the diff before allowing this file change."


def _tool_action_label(tool_name: str) -> str:
    return {
        "read_file": 'file action="read"',
        "write_file": 'file action="write"',
        "edit_file": 'file action="edit"',
        "list_files": 'file action="list"',
    }.get(tool_name, tool_name)


def _read_text_for_write(
    file_ops: WorkspaceFileOps,
    path: str,
) -> tuple[str, bool]:
    try:
        return file_ops.read_text(path).text, True
    except Exception as exc:
        if _looks_like_missing_file(exc):
            return "", False
        raise


def _looks_like_missing_file(exc: Exception) -> bool:
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "no such file",
            "not found",
            "cannot find the path",
            "does not exist",
        )
    )


__all__ = [
    "EditFileTool",
    "ListFilesTool",
    "PreparedTextChange",
    "ReadFileTool",
    "WriteFileTool",
    "build_file_tools",
]
