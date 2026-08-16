from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from mini_harness.agent.events import AgentEvent, AgentEventType
from mini_harness.agent.state import AgentState


class RichEventRenderer:
    def __init__(self, no_color: bool = False, verbose: bool = False) -> None:
        self.console = Console(no_color=no_color)
        self.verbose = verbose
        self.events: list[AgentEvent] = []

    def emit(
        self,
        event_type: AgentEventType,
        state: AgentState,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            sequence=len(self.events) + 1,
            timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
            event_type=event_type,
            state=state,
            summary=summary,
            payload=payload or {},
        )
        self.events.append(event)
        self.render(event)
        return event

    def startup(
        self,
        model: str,
        environment: str,
        project: str,
        max_iterations: int,
        transport: str,
    ) -> None:
        self.console.print(
            Panel(
                "\n".join(
                    [
                        "Mini Harness Agent",
                        f"Model: {model}",
                        f"Environment: {environment}",
                        f"Project: {project}",
                        f"Max iterations: {max_iterations}",
                        f"Runtime transport: {transport}",
                    ]
                ),
                title="Start",
            )
        )

    def render(self, event: AgentEvent) -> None:
        if event.event_type == AgentEventType.STATE_CHANGED:
            if self.verbose:
                self.console.print(f"[dim][STATE][/dim] {_escape(event.summary)}")
            return
        if event.event_type == AgentEventType.MODEL_REQUEST_STARTED:
            message_count = event.payload.get("message_count")
            if message_count is None and isinstance(event.payload.get("messages"), list):
                message_count = len(event.payload["messages"])
            self.console.print(
                f"[dim][MODEL][/dim] request started "
                f"({message_count if message_count is not None else '?'} messages)"
            )
            return
        if event.event_type == AgentEventType.CONTEXT_TRUNCATED:
            self.console.print(f"[yellow][CONTEXT][/yellow] {_escape(event.summary)}")
            return
        if event.event_type == AgentEventType.MODEL_REQUEST_COMPLETED:
            self._render_model_decision(event)
            return
        if event.event_type == AgentEventType.TOOL_SELECTED:
            self.console.print(f"[cyan][THINK][/cyan] {_escape(event.summary)}")
            return
        if event.event_type == AgentEventType.TOOL_STARTED:
            self._render_tool_started(event)
            return
        if event.event_type == AgentEventType.TOOL_COMPLETED:
            self._render_tool_result(event, ok=not _is_failed_task_observation(event.payload))
            return
        if event.event_type == AgentEventType.TOOL_FAILED:
            self._render_tool_result(event, ok=False)
            return
        if event.event_type == AgentEventType.TASK_STARTED:
            self._render_task_started(event)
            return
        if event.event_type == AgentEventType.TASK_OUTPUT:
            content = str(event.payload.get("content", ""))
            if content:
                self.console.print(
                    Panel(
                        _clip(content, 20_000),
                        title=str(event.payload.get("resource_ref", "task output")),
                        border_style="magenta",
                    )
                )
            return
        if event.event_type == AgentEventType.TASK_COMPLETED:
            self.console.print(f"[green][TASK][/green] {_escape(event.summary)}")
            return
        if event.event_type == AgentEventType.TASK_STATUS_CHANGED:
            self.console.print(f"[yellow][TASK][/yellow] {_escape(event.summary)}")
            return
        if event.event_type in {AgentEventType.AGENT_COMPLETED, AgentEventType.AGENT_FAILED}:
            style = "green" if event.event_type == AgentEventType.AGENT_COMPLETED else "red"
            self.console.print(
                Panel(Text(event.summary), title=event.event_type.value, style=style)
            )
            return
        if self.verbose:
            self.console.print(f"[dim]{event.event_type.value}[/dim] {_escape(event.summary)}")

    def _render_model_decision(self, event: AgentEvent) -> None:
        decision = event.payload.get("decision")
        if not isinstance(decision, dict):
            self.console.print(f"[cyan][MODEL][/cyan] {_escape(event.summary)}")
            return
        if decision.get("type") == "tool":
            tool_name = str(decision.get("tool_name", ""))
            reason = str(decision.get("reason_summary", ""))
            arguments = decision.get("arguments", {})
            raw_output = str(decision.get("raw_output") or "")
            body = Table.grid(padding=(0, 1))
            body.add_column(style="bold cyan", no_wrap=True)
            body.add_column()
            body.add_row("tool", tool_name)
            body.add_row("reason", reason)
            body.add_row("arguments", _json_text(arguments))
            self.console.print(Panel(body, title="Model Decision", border_style="cyan"))
            self._render_model_raw_output(raw_output)
            return
        if decision.get("type") == "final":
            summary = str(decision.get("summary", ""))
            details = str(decision.get("details") or "")
            raw_output = str(decision.get("raw_output") or "")
            final_text = summary if not details else f"{summary}\n\n{details}"
            self.console.print(Panel(final_text, title="Model Final", border_style="green"))
            if raw_output.strip() != final_text.strip():
                self._render_model_raw_output(raw_output)
            return
        self.console.print(Panel(_json_text(decision), title="Model Output", border_style="cyan"))

    def _render_model_raw_output(self, raw_output: str) -> None:
        if not raw_output:
            return
        lexer = "json" if raw_output.lstrip().startswith(("{", "[")) else "text"
        self.console.print(
            Panel(
                Syntax(_clip(raw_output, 20_000), lexer, word_wrap=True),
                title="Model Raw Output",
                border_style="dim cyan",
            )
        )

    def _render_tool_started(self, event: AgentEvent) -> None:
        name = str(event.payload.get("tool_name", ""))
        arguments = event.payload.get("arguments", {})
        label = _tool_label(name, arguments)
        self.console.print(f"[blue][TOOL][/blue] {_escape(label)}")
        if self.verbose and arguments:
            self.console.print(Syntax(_json_text(arguments), "json", word_wrap=True))

    def _render_tool_result(self, event: AgentEvent, ok: bool) -> None:
        name = _tool_name_from_payload(event.payload)
        style = "green" if ok else "red"
        prefix = "OK" if ok else "ERROR"
        summary = event.summary
        metadata = event.payload.get("metadata", {})
        content = str(event.payload.get("content") or "")
        resource_ref = str(event.payload.get("resource_ref") or "")
        state = event.payload.get("state")
        cursor = event.payload.get("cursor")
        self.console.print(
            f"[{style}][{prefix}][/{style}] {_escape(_result_label(name, summary, metadata))}"
        )
        details = _result_details(name, resource_ref, state, cursor, metadata)
        if details:
            self.console.print(details)
        if content:
            self._render_content(name, resource_ref, content)

    def _render_task_started(self, event: AgentEvent) -> None:
        argv = event.payload.get("argv")
        cwd = event.payload.get("cwd")
        task_id = event.payload.get("task_id")
        command = _command_text(argv)
        body = Table.grid(padding=(0, 1))
        body.add_column(style="bold magenta", no_wrap=True)
        body.add_column()
        if task_id:
            body.add_row("task", str(task_id))
        if command:
            body.add_row("command", command)
        if cwd:
            body.add_row("cwd", str(cwd))
        self.console.print(Panel(body, title="Task Started", border_style="magenta"))

    def _render_content(self, tool_name: str, resource_ref: str, content: str) -> None:
        title = resource_ref or f"{tool_name} output"
        if tool_name == "read_file":
            self.console.print(Panel(_clip(content, 20_000), title=title, border_style="blue"))
            return
        if tool_name in {"observe_task", "observe_terminal", "ensure_remote_tool"}:
            self.console.print(Panel(_clip(content, 20_000), title=title, border_style="magenta"))
            return
        if self.verbose:
            self.console.print(Panel(_clip(content, 20_000), title=title))


def _tool_name_from_payload(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata", {})
    if isinstance(metadata, dict):
        if metadata.get("guard") == "final":
            return "final_guard"
        if "argv" in metadata:
            return "run_command"
        if "line_count" in metadata:
            return "read_file"
        if (
            "replace_all" in metadata
            and payload.get("resource_ref", "").startswith("file:")
        ):
            return "edit_file"
        if "size" in metadata and payload.get("resource_ref", "").startswith("file:"):
            return "write_file"
        if "exit_code" in metadata:
            return "observe_task"
        if "backend" in metadata:
            return "open_terminal"
        if "frame_count" in metadata:
            return "observe_terminal"
        if "bytes" in metadata:
            return "send_terminal_control" if "control" in metadata else "terminal_command"
        if "tool" in metadata:
            return "ensure_remote_tool"
    return ""


def _is_failed_task_observation(payload: dict[str, Any]) -> bool:
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict) or "exit_code" not in metadata:
        return False
    state = str(payload.get("state") or "")
    exit_code = metadata.get("exit_code")
    return state in {"FAILED", "CANCELLED", "LOST"} or (
        isinstance(exit_code, int) and exit_code != 0
    )


def _tool_label(name: str, arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return name
    if name in {"file", "command", "task", "remote", "sync", "terminal"}:
        return _facade_tool_label(name, arguments)
    if name in {"read_file", "write_file", "edit_file", "list_files"}:
        path = arguments.get("path", ".")
        return f"{name} {path}"
    if name == "run_command":
        return f"run_command {_command_text(arguments.get('argv'))}"
    if name == "terminal_command":
        input_only = bool(arguments.get("input_only", False))
        return (
            "terminal_command "
            f"{_terminal_input_display(str(arguments.get('data', '')), append_enter=not input_only)}"
        )
    if name == "send_terminal_control":
        return f"send_terminal_control {arguments.get('control', '')}"
    if name == "ensure_remote_tool":
        install = arguments.get("install", False)
        return f"ensure_remote_tool {arguments.get('tool', '')} install={install}"
    if name in {"open_terminal", "observe_terminal", "close_terminal"}:
        return name
    return name


def _result_label(name: str, summary: str, metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return summary
    if name in {"write_file", "edit_file"}:
        return f"{summary} ({metadata.get('path', '')})"
    if name == "read_file":
        return f"{summary} ({metadata.get('path', '')})"
    if name in {"run_command", "command"}:
        return f"{summary}: {_command_text(metadata.get('argv'))}"
    if name == "observe_task" or (
        name == "task" and metadata.get("inner_tool") == "observe_task"
    ):
        exit_code = metadata.get("exit_code")
        return summary if exit_code is None else f"{summary} exit={exit_code}"
    if name == "final_guard":
        reason = metadata.get("reason")
        return summary if not reason else f"{summary} reason={reason}"
    if name == "open_terminal" and metadata.get("fallback_from"):
        return f"{summary} fallback_error={metadata.get('fallback_error')}"
    return summary


def _facade_tool_label(name: str, arguments: dict[str, Any]) -> str:
    action = str(arguments.get("action") or "")
    if name == "file":
        return f"file {action} {arguments.get('path', '.')}"
    if name == "command":
        return f"command run target={arguments.get('target', 'current')} {_command_text(arguments.get('argv'))}"
    if name == "task":
        if action == "start":
            return f"task start target={arguments.get('target', 'current')} {_command_text(arguments.get('argv'))}"
        return f"task {action} {arguments.get('task_ref', '')}".rstrip()
    if name == "remote":
        if action == "ensure_tool":
            return f"remote ensure_tool {arguments.get('tool', '')}"
        return f"remote {action}"
    if name == "sync":
        return f"sync {action}"
    if name == "terminal":
        if action == "command":
            input_only = bool(arguments.get("input_only", False))
            return (
                "terminal command "
                f"{_terminal_input_display(str(arguments.get('data', '')), not input_only)}"
            )
        if action == "control":
            return f"terminal control {arguments.get('control', '')}"
        if action == "open":
            return f"terminal open target={arguments.get('target', 'current')}"
        return f"terminal {action} {arguments.get('session_ref', '')}".rstrip()
    return name


def _result_details(
    name: str,
    resource_ref: str,
    state: Any,
    cursor: Any,
    metadata: Any,
) -> Table | None:
    rows: list[tuple[str, str]] = []
    if resource_ref:
        rows.append(("resource", resource_ref))
    if state:
        rows.append(("state", str(state)))
    if cursor is not None:
        rows.append(("cursor", str(cursor)))
    if isinstance(metadata, dict):
        if name in {"run_command", "command"}:
            if metadata.get("cwd"):
                rows.append(("cwd", str(metadata["cwd"])))
            if metadata.get("task_id"):
                rows.append(("task", str(metadata["task_id"])))
        if name == "open_terminal" or (
            name == "terminal" and metadata.get("inner_tool") == "open_terminal"
        ):
            for key in ("backend", "fallback_from", "recommended_action"):
                if metadata.get(key):
                    rows.append((key, str(metadata[key])))
        if name == "ensure_remote_tool" or (
            name == "remote" and metadata.get("inner_tool") == "ensure_remote_tool"
        ):
            for key in ("tool", "present", "installed", "missing", "recommended_action"):
                if metadata.get(key) is not None:
                    rows.append((key, str(metadata[key])))
        if name == "final_guard":
            for key in (
                "attempted_final_summary",
                "active_task",
                "active_session",
                "terminal_input_pending",
                "last_command_exit_code",
            ):
                if metadata.get(key) is not None:
                    rows.append((key, str(metadata[key])))
    if not rows:
        return None
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    for key, value in rows:
        table.add_row(key, value)
    return table


def _command_text(argv: Any) -> str:
    if isinstance(argv, list):
        return " ".join(str(item) for item in argv)
    if argv is None:
        return ""
    return str(argv)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:] + "\n[truncated]"


def _terminal_input_display(data: str, append_enter: bool = False) -> str:
    if data == "":
        return "<ENTER>"
    if data == "\n":
        return "<ENTER>"
    if data == "\r":
        return "<CR>"
    normalized = data
    if append_enter and not normalized.endswith(("\n", "\r")):
        normalized += "\n"
    value = normalized.replace("\r\n", "<ENTER>").replace("\n", "<ENTER>").replace("\r", "<CR>")
    return _clip(value, 80)


def _escape(value: object) -> str:
    return escape(str(value))
