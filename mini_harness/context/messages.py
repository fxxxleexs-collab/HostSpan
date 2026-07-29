from __future__ import annotations

from mini_harness.config import AgentConfig
from mini_harness.models.schemas import ModelMessage
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.schemas import ToolDefinition, ToolResult

SYSTEM_PROMPT = """You are Mini Harness Agent, a small runtime-integration coding agent.
All file and command operations must use the provided tools.
Inspect before editing. Run tests when the task asks for verification.
After starting a task, call observe_task until it reaches a terminal state.
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
                f"Arguments: {arguments}\n"
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
        tool_names = ", ".join(tool.name for tool in tools)
        return (
            f"Environment: {self.work_context.environment_id}\n"
            f"Endpoint: {self.work_context.endpoint_id}\n"
            f"Project root: {self.work_context.project_root}\n"
            f"Current directory: {self.work_context.cwd}\n"
            f"Active task: {active}\n"
            f"Task log cursor: {self.work_context.task_log_cursor}\n"
            f"Iteration: {self.work_context.iteration}/{self.config.max_iterations}\n"
            f"Tools: {tool_names}"
        )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:] + "\n[truncated]"
