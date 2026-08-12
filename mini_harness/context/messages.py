from __future__ import annotations

from dataclasses import dataclass

from mini_harness.config import AgentConfig
from mini_harness.models.schemas import FinalDecision, ModelMessage
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.schemas import ToolDefinition, ToolResult

SYSTEM_PROMPT = """You are Mini Harness Agent, a small runtime-integration coding agent.
All file and command operations must use the provided tools.
Inspect before editing. Run tests when the task asks for verification.
For large files, read_file supports block reads with start_line and max_lines;
use next_start_line from metadata to continue reading.
For targeted edits to existing files, prefer edit_file with exact old_text and
expected_sha256 from the most recent read_file result. Use write_file mainly for
new files or deliberate full-file rewrites.
Use run_command for short one-shot non-interactive commands such as checks,
builds, tests, and inspections. Use start_task for long-running non-interactive
commands such as Flask/Vite/Uvicorn dev servers, file watchers, and background
services; then use observe_task, list_tasks, and cancel_task to manage them.
Use terminal tools only when live human interaction, passwords, REPL input, or
stateful shell context is required.
After starting a short task with run_command, call observe_task until it reaches
a terminal state. After starting a long-running service with start_task, observe
startup logs and keep the task_ref for later list_tasks/observe_task/cancel_task.
Before each tool call, briefly state in natural language what you are trying
to learn or change, why that tool is the next step, and what result you expect.
For commands which require interaction, use open_terminal, observe_terminal,
send_terminal_input, and close_terminal instead of run_command or start_task.
Use open_local_terminal when interaction must happen on the user's machine, such
as building local artifacts or using Windows PowerShell. Use open_remote_terminal
when interaction must happen on the configured SSH host. open_terminal uses the
default command target shown in the work context.
run_command and start_task start clean non-interactive tasks and do not inherit terminal
state such as sudo/root shell, cd, exported env vars, activated venv, nested
ssh/login, or tmux shell state. If an active terminal session has the needed
state, use run_in_session instead. Only set run_command force_clean=true when
you deliberately want a clean task without session state.
In SSH runtime mode, open_remote_terminal already starts the process on the
remote host. Do not run ssh inside open_remote_terminal; leave argv unset or use
argv ["bash", "-l"] for a remote shell.
When sync is enabled in SSH runtime mode, call sync_status before relying on the
remote mirror and call sync_push before remote commands that need fresh local
files. sync_status reports the local-to-remote manifest diff summary.
If an SSH connection must be configured during chat, use request_ssh_connection
or ask the user to enter /connect-ssh. Never ask the user to paste SSH passwords
into normal chat, config files, command arguments, or tool arguments.
Before sending terminal commands, check the active terminal target, OS, and shell
in the work context and use the matching command syntax.
If you opened root privileges or changed shell state inside a terminal, keep
running dependent commands with run_in_session until that state is no longer
needed.
When observing a terminal command expected to run for N seconds, call
observe_terminal with wait_seconds set to at least N plus a small buffer.
For send_terminal_input, an empty data string means press Enter. Prefer sending
commands with run_directly=true, such as data "id" plus run_directly true,
instead of sending "id" and Enter separately.
To interrupt a running foreground terminal process, use send_terminal_control
with control="ctrl_c"; do not send literal "\\x03" text.
If open_terminal reports fallback_from=ssh_tmux, use ensure_remote_tool with
tool=tmux when a durable remote terminal is needed.
When asked whether terminal sessions are still open, use list_terminal_sessions
and inspect_terminal_session. Do not open another shell and run tmux commands to
discover sessions; Runtime session state is the authoritative source.
list_terminal_sessions defaults to the 10 most recent sessions and includes
short conversation-local session briefs when available. Narrow it with
scope="conversation", state_filter="active", max_sessions, or date filters when
many sessions exist. If a session is DISCONNECTED, TERMINATED, or LOST,
historical output may still be readable but the session cannot accept
interactive input. Use activate_terminal_session only for an ACTIVE Runtime
session.
Do not claim tests passed unless a runtime task showed success.
Use only relative paths inside the project root.
Respect sandbox denials as runtime boundaries; do not try to bypass them with
absolute paths, nested shells, or alternate tools.
Return tool decisions through the available tool-call format. For final answers,
plain text is accepted; structured final JSON is also supported."""


@dataclass(frozen=True)
class ContextCompactResult:
    compacted: bool
    reason: str
    user_turns_before: int
    user_turns_after: int
    tool_turns_before: int
    tool_turns_after: int
    summary_chars: int

    @property
    def summary(self) -> str:
        if not self.compacted:
            return "Context compact skipped; there was no older history to summarize."
        return (
            "Context compacted: "
            f"user turns {self.user_turns_before}->{self.user_turns_after}, "
            f"tool turns {self.tool_turns_before}->{self.tool_turns_after}, "
            f"summary chars={self.summary_chars}."
        )


class AgentContext:
    def __init__(
        self,
        config: AgentConfig,
        work_context: WorkContext,
        user_task: str | None = None,
    ) -> None:
        self.config = config
        self.work_context = work_context
        self.user_tasks: list[str] = []
        self.assistant_finals: list[str] = []
        self.tool_turns: list[tuple[str, dict[str, object], ToolResult]] = []
        self.compacted_summary: str | None = None
        self.truncated = False
        self.compacted = False
        self.last_compact_result: ContextCompactResult | None = None
        if user_task:
            self.add_user_task(user_task)

    @property
    def user_task(self) -> str:
        return self.user_tasks[-1] if self.user_tasks else ""

    def add_user_task(self, user_task: str) -> None:
        self.user_tasks.append(user_task)

    def add_final_decision(self, decision: FinalDecision) -> None:
        text = (
            decision.summary
            if not decision.details
            else f"{decision.summary}\n\n{decision.details}"
        )
        self.assistant_finals.append(text)

    def add_tool_result(
        self,
        name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> None:
        self.tool_turns.append((name, arguments, result))

    def build_messages(self, tools: list[ToolDefinition]) -> list[ModelMessage]:
        self.compacted = False
        self.maybe_auto_compact()
        messages = self._build_messages(tools, recent_tool_turns=self.config.recent_tool_turns)
        rendered_len = sum(len(message.content) for message in messages)
        if rendered_len <= self.config.max_context_chars:
            return messages

        self.compact(reason="size")
        messages = self._build_messages(tools, recent_tool_turns=self.config.recent_tool_turns)
        rendered_len = sum(len(message.content) for message in messages)
        if rendered_len <= self.config.max_context_chars:
            self.truncated = True
            return messages

        self.truncated = True
        keep_tool_turns = max(1, self.config.recent_tool_turns // 2)
        return self._build_messages(tools, recent_tool_turns=keep_tool_turns)

    def maybe_auto_compact(self) -> ContextCompactResult:
        if len(self.user_tasks) > self.config.auto_compact_turns:
            return self.compact(reason="auto:user-turns")
        if len(self.tool_turns) > self.config.auto_compact_tool_turns:
            return self.compact(reason="auto:tool-turns")
        return self._compact_result(False, "auto")

    def compact(self, reason: str = "manual") -> ContextCompactResult:
        before_users = len(self.user_tasks)
        before_tools = len(self.tool_turns)
        self.compacted = False
        self._compact_history()
        result = ContextCompactResult(
            compacted=self.compacted,
            reason=reason,
            user_turns_before=before_users,
            user_turns_after=len(self.user_tasks),
            tool_turns_before=before_tools,
            tool_turns_after=len(self.tool_turns),
            summary_chars=len(self.compacted_summary or ""),
        )
        self.last_compact_result = result
        return result

    def _build_messages(
        self,
        tools: list[ToolDefinition],
        *,
        recent_tool_turns: int,
    ) -> list[ModelMessage]:
        messages = [
            ModelMessage(role="system", content=SYSTEM_PROMPT),
        ]
        if self.compacted_summary:
            messages.append(
                ModelMessage(
                    role="system",
                    content=f"Compacted conversation context:\n{self.compacted_summary}",
                )
            )
        messages.extend(self._conversation_messages())
        messages.append(ModelMessage(role="system", content=self._work_context_summary(tools)))
        history = self.tool_turns[-recent_tool_turns:]
        for name, arguments, result in history:
            content = (
                f"Tool call: {name}\n"
                f"Arguments: {_format_tool_arguments(name, arguments)}\n"
                f"Result ok: {result.ok}\n"
                f"Summary: {result.summary}\n"
                f"State: {result.state}\n"
                f"Content:\n{_truncate(result.content or '', self.config.max_tool_result_chars)}"
            )
            messages.append(ModelMessage(role="tool", name=name, content=content))
        return messages

    def _conversation_messages(self) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        for index, task in enumerate(self.user_tasks):
            messages.append(ModelMessage(role="user", content=task))
            if index < len(self.assistant_finals):
                messages.append(
                    ModelMessage(role="assistant", content=self.assistant_finals[index])
                )
        if not messages:
            messages.append(ModelMessage(role="user", content="Continue."))
        return messages

    def _compact_history(self) -> None:
        lines: list[str] = []
        if self.compacted_summary:
            lines.append(self.compacted_summary)
            lines.append("")

        compact_user_count = max(0, len(self.user_tasks) - 1)
        if compact_user_count:
            lines.append("Earlier conversation:")
            for index, task in enumerate(self.user_tasks[:compact_user_count], start=1):
                lines.append(f"- User {index}: {_one_line(task)}")
                assistant_index = index - 1
                if assistant_index < len(self.assistant_finals):
                    lines.append(
                        f"- Assistant {index}: {_one_line(self.assistant_finals[assistant_index])}"
                    )

        keep_tool_count = max(1, self.config.recent_tool_turns // 2)
        compact_tools = self.tool_turns[:-keep_tool_count]
        if compact_tools:
            lines.append("Earlier tool results:")
            for name, arguments, result in compact_tools:
                args = _format_tool_arguments(name, arguments)
                lines.append(
                    "- "
                    f"{name} args={_one_line(str(args), 240)} "
                    f"ok={result.ok} state={result.state} summary={_one_line(result.summary)}"
                )

        if not lines:
            return
        self.compacted_summary = _truncate("\n".join(lines), 20_000)
        if compact_user_count:
            self.user_tasks = self.user_tasks[compact_user_count:]
            self.assistant_finals = self.assistant_finals[compact_user_count:]
        if compact_tools:
            self.tool_turns = self.tool_turns[-keep_tool_count:]
        self.compacted = True

    def _compact_result(self, compacted: bool, reason: str) -> ContextCompactResult:
        return ContextCompactResult(
            compacted=compacted,
            reason=reason,
            user_turns_before=len(self.user_tasks),
            user_turns_after=len(self.user_tasks),
            tool_turns_before=len(self.tool_turns),
            tool_turns_after=len(self.tool_turns),
            summary_chars=len(self.compacted_summary or ""),
        )

    def _work_context_summary(self, tools: list[ToolDefinition]) -> str:
        active = self.work_context.task_ref() or "none"
        active_session = self.work_context.session_state_summary()
        tool_names = ", ".join(tool.name for tool in tools)
        return (
            f"Environment: {self.work_context.environment_id}\n"
            f"Endpoint: {self.work_context.endpoint_id}\n"
            f"Runtime mode: {self.work_context.runtime_mode}\n"
            f"Remote connection: host={self.work_context.remote_address_summary()}, "
            f"configured={str(self.work_context.remote_hostname is not None).lower()}, "
            f"connected={str(self.work_context.remote_target() is not None).lower()}\n"
            f"{self.work_context.target_summary()}\n"
            f"{self.work_context.sandbox_summary()}\n"
            f"{self.work_context.sync_summary()}\n"
            f"Project root: {self.work_context.project_root}\n"
            f"Remote root: {self.work_context.remote_root or 'n/a'}\n"
            f"Current directory: {self.work_context.cwd}\n"
            f"Active task: {active}\n"
            f"Task log cursor: {self.work_context.task_log_cursor}\n"
            f"Active session: {active_session}\n"
            f"Terminal cursor: {self.work_context.terminal_cursor}\n"
            f"Terminal input pending: {self.work_context.terminal_input_pending}\n"
            f"{self.work_context.runtime_activity_summary()}\n"
            f"Iteration: {self.work_context.iteration}/{self.config.max_iterations}\n"
            f"Tools: {tool_names}"
        )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:] + "\n[truncated]"


def _one_line(text: str, max_chars: int = 500) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 13] + " ...[clipped]"


def _format_tool_arguments(name: str, arguments: dict[str, object]) -> dict[str, object]:
    if name != "send_terminal_input":
        return arguments
    rendered = dict(arguments)
    data = rendered.get("data")
    if isinstance(data, str):
        run_directly = bool(rendered.get("run_directly", False))
        rendered["data_display"] = _terminal_input_display(data, run_directly)
        rendered.pop("data", None)
    return rendered


def _terminal_input_display(data: str, run_directly: bool = False) -> str:
    if data == "":
        return "<ENTER> (empty string normalized by tool)"
    if data == "\n":
        return "<ENTER>"
    if data == "\r":
        return "<CR>"
    normalized = data
    if run_directly and not normalized.endswith(("\n", "\r")):
        normalized += "\n"
    value = normalized.replace("\r\n", "<ENTER>").replace("\n", "<ENTER>").replace("\r", "<CR>")
    return value if len(value) <= 120 else value[:117] + "..."
