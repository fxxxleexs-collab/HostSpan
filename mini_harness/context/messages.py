from __future__ import annotations

from mini_harness.config import AgentConfig
from mini_harness.models.schemas import ModelMessage
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
run_command starts a clean non-interactive task and does not inherit terminal
state such as sudo/root shell, cd, exported env vars, activated venv, nested
ssh/login, or tmux shell state. If an active terminal session has the needed
state, use run_in_session instead. Only set run_command force_clean=true when
you deliberately want a clean task without session state.
In SSH runtime mode, open_terminal already starts the process on the remote
host. Do not run ssh inside open_terminal; use argv ["bash", "-l"] for a shell.
If you opened root privileges or changed shell state inside a terminal, keep
running dependent commands with run_in_session until that state is no longer
needed.
When observing a terminal command expected to run for N seconds, call
observe_terminal with wait_seconds set to at least N plus a small buffer.
For send_terminal_input, an empty data string means press Enter. Prefer sending
commands with run_directly=true, such as data "id" plus run_directly true,
instead of sending "id" and Enter separately.
If open_terminal reports fallback_from=ssh_tmux, use ensure_remote_tool with
tool=tmux when a durable remote terminal is needed.
Do not claim tests passed unless a runtime task showed success.
Use only relative paths inside the project root.
Return either a structured tool decision or a structured final decision."""


class AgentContext:
    def __init__(
        self,
        user_task: str,
        config: AgentConfig,
        work_context: WorkContext,
    ) -> None:
        self.user_task = user_task
        self.config = config
        self.work_context = work_context
        self.tool_turns: list[tuple[str, dict[str, object], ToolResult]] = []
        self.truncated = False

    def add_tool_result(
        self,
        name: str,
        arguments: dict[str, object],
        result: ToolResult,
    ) -> None:
        self.tool_turns.append((name, arguments, result))

    def build_messages(self, tools: list[ToolDefinition]) -> list[ModelMessage]:
        messages = [
            ModelMessage(role="system", content=SYSTEM_PROMPT),
            ModelMessage(role="user", content=self.user_task),
            ModelMessage(role="system", content=self._work_context_summary(tools)),
        ]
        history = self.tool_turns[-self.config.recent_tool_turns :]
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
        rendered_len = sum(len(message.content) for message in messages)
        if rendered_len <= self.config.max_context_chars:
            return messages
        self.truncated = True
        keep = messages[:3]
        tail = messages[-max(1, self.config.recent_tool_turns // 2) :]
        return keep + tail

    def _work_context_summary(self, tools: list[ToolDefinition]) -> str:
        active = self.work_context.task_ref() or "none"
        active_session = self.work_context.session_state_summary()
        tool_names = ", ".join(tool.name for tool in tools)
        return (
            f"Environment: {self.work_context.environment_id}\n"
            f"Endpoint: {self.work_context.endpoint_id}\n"
            f"Runtime mode: {self.work_context.runtime_mode}\n"
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
