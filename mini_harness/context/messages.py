from __future__ import annotations

from mini_harness.config import AgentConfig
from mini_harness.models.schemas import FinalDecision, ModelMessage
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.schemas import ToolDefinition, ToolResult

SYSTEM_PROMPT = """You are Mini Harness Agent, a small runtime-integration coding agent.
All file and command operations must use the provided tools.
Inspect before editing. Run tests when the task asks for verification.
After starting a task, call observe_task until it reaches a terminal state.
Before each tool call, briefly state in natural language what you are trying
to learn or change, why that tool is the next step, and what result you expect.
For commands which require interaction, use open_terminal, observe_terminal,
send_terminal_input, and close_terminal instead of run_command.
Use open_local_terminal when interaction must happen on the user's machine, such
as building local artifacts or using Windows PowerShell. Use open_remote_terminal
when interaction must happen on the configured SSH host. open_terminal uses the
default command target shown in the work context.
run_command starts a clean non-interactive task and does not inherit terminal
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
Do not claim tests passed unless a runtime task showed success.
Use only relative paths inside the project root.
Respect sandbox denials as runtime boundaries; do not try to bypass them with
absolute paths, nested shells, or alternate tools.
Return tool decisions through the available tool-call format. For final answers,
plain text is accepted; structured final JSON is also supported."""


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
        messages = self._build_messages(tools, recent_tool_turns=self.config.recent_tool_turns)
        rendered_len = sum(len(message.content) for message in messages)
        if rendered_len <= self.config.max_context_chars:
            return messages

        self._compact_history()
        messages = self._build_messages(tools, recent_tool_turns=self.config.recent_tool_turns)
        rendered_len = sum(len(message.content) for message in messages)
        if rendered_len <= self.config.max_context_chars:
            self.truncated = True
            return messages

        self.truncated = True
        keep_tool_turns = max(1, self.config.recent_tool_turns // 2)
        return self._build_messages(tools, recent_tool_turns=keep_tool_turns)

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

    def _work_context_summary(self, tools: list[ToolDefinition]) -> str:
        active = self.work_context.task_ref() or "none"
        active_session = self.work_context.session_state_summary()
        tool_names = ", ".join(tool.name for tool in tools)
        return (
            f"Environment: {self.work_context.environment_id}\n"
            f"Endpoint: {self.work_context.endpoint_id}\n"
            f"Runtime mode: {self.work_context.runtime_mode}\n"
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
