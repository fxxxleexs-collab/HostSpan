from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from mini_harness.agent.events import AgentEventSink, AgentEventType, InMemoryEventSink
from mini_harness.agent.state import AgentState, StateMachine
from mini_harness.agent.termination import validate_final
from mini_harness.config import AgentConfig
from mini_harness.context.messages import AgentContext
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.models.base import ModelProvider
from mini_harness.models.schemas import FinalDecision, ToolDecision
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.registry import ToolRegistry, tool_call_fingerprint
from mini_harness.tools.schemas import ToolResult


class AgentRunResult(BaseModel):
    final_state: AgentState
    summary: str
    details: str | None = None
    iterations: int
    tool_call_count: int
    tool_error_count: int
    duration_seconds: float
    error_code: str | None = None


class AgentLoop:
    def __init__(
        self,
        model: ModelProvider,
        tools: ToolRegistry,
        config: AgentConfig | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.config = config or AgentConfig()
        self.event_sink = event_sink or InMemoryEventSink()
        self.machine = StateMachine()
        self.tool_call_count = 0
        self.tool_error_count = 0
        self._consecutive_tool_errors = 0
        self._recent_fingerprints: list[str] = []
        self._iterations = 0

    async def run(self, user_task: str, work_context: WorkContext) -> AgentRunResult:
        context = AgentContext(self.config, work_context, user_task)
        return await self.run_with_context(user_task, work_context, context)

    async def run_with_context(
        self,
        user_task: str,
        work_context: WorkContext,
        context: AgentContext,
    ) -> AgentRunResult:
        started = time.monotonic()
        self._reset_run_state()
        if context.user_task != user_task:
            context.add_user_task(user_task)
        self._emit(AgentEventType.AGENT_STARTED, "Agent started")
        try:
            self._transition(AgentState.INITIALIZING)
            self._transition(AgentState.READY)
            while self._iterations < self.config.max_iterations:
                work_context.iteration += 1
                self._iterations += 1
                self._transition(AgentState.PLANNING)
                definitions = self.tools.definitions()
                messages = context.build_messages(definitions)
                if context.compacted:
                    self._emit(AgentEventType.CONTEXT_TRUNCATED, "Context was compacted")
                    context.compacted = False
                if context.truncated:
                    self._emit(AgentEventType.CONTEXT_TRUNCATED, "Context was truncated")
                    context.truncated = False
                self._transition(AgentState.WAITING_FOR_MODEL)
                self._emit(
                    AgentEventType.MODEL_REQUEST_STARTED,
                    "Model request started",
                    {
                        "messages": [message.model_dump(exclude_none=True) for message in messages],
                        "tools": [tool.model_dump() for tool in definitions],
                    },
                )
                decision = await self.model.decide(messages, definitions)
                self._emit(
                    AgentEventType.MODEL_REQUEST_COMPLETED,
                    f"Model returned {decision.type}",
                    {"decision": decision.model_dump()},
                )
                if isinstance(decision, FinalDecision):
                    self._transition(AgentState.PROCESSING_RESULT)
                    guard = validate_final(decision, work_context, user_task)
                    if guard is not None:
                        context.add_tool_result("final_guard", {}, guard)
                        self.tool_error_count += 1
                        self._consecutive_tool_errors += 1
                        self._emit(
                            AgentEventType.TOOL_FAILED,
                            guard.summary,
                            guard.model_dump(exclude_none=True),
                        )
                        continue
                    context.add_final_decision(decision)
                    self._transition(AgentState.COMPLETED)
                    self._emit(AgentEventType.AGENT_COMPLETED, decision.summary)
                    return self._result(
                        started,
                        AgentState.COMPLETED,
                        decision.summary,
                        decision.details,
                    )
                await self._execute_decision(decision, context, work_context)
            raise MiniHarnessError(
                ErrorCode.MAX_ITERATIONS_EXCEEDED,
                "maximum iterations exceeded",
                recoverable=False,
            )
        except MiniHarnessError as exc:
            if self.machine.state != AgentState.FAILED:
                self._transition(AgentState.FAILED)
            self._emit(
                AgentEventType.AGENT_FAILED,
                str(exc),
                {"error_code": exc.code.value, "recoverable": exc.recoverable},
            )
            return self._result(started, AgentState.FAILED, str(exc), None, exc.code.value)
        except Exception as exc:
            if self.machine.state != AgentState.FAILED:
                self._transition(AgentState.FAILED)
            self._emit(AgentEventType.AGENT_FAILED, str(exc))
            return self._result(
                started,
                AgentState.FAILED,
                str(exc),
                None,
                ErrorCode.RUNTIME_OPERATION_FAILED.value,
            )

    def _reset_run_state(self) -> None:
        self.machine = StateMachine()
        self.tool_call_count = 0
        self.tool_error_count = 0
        self._consecutive_tool_errors = 0
        self._recent_fingerprints = []
        self._iterations = 0

    async def _execute_decision(
        self,
        decision: ToolDecision,
        context: AgentContext,
        work_context: WorkContext,
    ) -> None:
        self._transition(AgentState.TOOL_SELECTED)
        self._emit(
            AgentEventType.TOOL_SELECTED,
            decision.reason_summary,
            {"tool_name": decision.tool_name, "arguments": decision.arguments},
        )
        repeat_warning = self._record_fingerprint(decision.tool_name, decision.arguments)
        if repeat_warning is not None:
            context.add_tool_result(decision.tool_name, decision.arguments, repeat_warning)
            self.tool_error_count += 1
            self._consecutive_tool_errors += 1
            self._transition(AgentState.EXECUTING_TOOL)
            self._transition(AgentState.PROCESSING_RESULT)
            self._emit(AgentEventType.TOOL_FAILED, repeat_warning.summary)
            if self._consecutive_tool_errors >= self.config.max_consecutive_tool_errors:
                raise MiniHarnessError(
                    ErrorCode.CONSECUTIVE_ERROR_LIMIT,
                    "consecutive tool error limit reached",
                    recoverable=False,
                )
            return
        self._transition(AgentState.EXECUTING_TOOL)
        self._emit(
            AgentEventType.TOOL_STARTED,
            f"Started {decision.tool_name}",
            {"tool_name": decision.tool_name, "arguments": decision.arguments},
        )
        result = await self.tools.execute(decision.tool_name, decision.arguments, work_context)
        self.tool_call_count += 1
        work_context.last_tool_name = decision.tool_name
        if decision.tool_name == "run_command" and result.ok:
            self._transition(AgentState.OBSERVING_TASK)
            self._emit(AgentEventType.TASK_STARTED, result.summary, result.metadata)
            self._transition(AgentState.PROCESSING_RESULT)
        else:
            self._transition(AgentState.PROCESSING_RESULT)
        if decision.tool_name == "observe_task":
            self._emit_task_events(result)
        if result.ok:
            self._consecutive_tool_errors = 0
            self._emit(
                AgentEventType.TOOL_COMPLETED,
                result.summary,
                result.model_dump(exclude_none=True),
            )
        else:
            self.tool_error_count += 1
            self._consecutive_tool_errors += 1
            self._emit(
                AgentEventType.TOOL_FAILED,
                result.summary,
                result.model_dump(exclude_none=True),
            )
            if not result.recoverable:
                raise MiniHarnessError(
                    ErrorCode(result.error_code or ErrorCode.RUNTIME_OPERATION_FAILED.value),
                    result.summary,
                    recoverable=False,
                )
            if self._consecutive_tool_errors >= self.config.max_consecutive_tool_errors:
                raise MiniHarnessError(
                    ErrorCode.CONSECUTIVE_ERROR_LIMIT,
                    "consecutive tool error limit reached",
                    recoverable=False,
                )
        context.add_tool_result(decision.tool_name, decision.arguments, result)

    def _record_fingerprint(self, name: str, arguments: dict[str, Any]) -> ToolResult | None:
        fingerprint = tool_call_fingerprint(name, arguments)
        self._recent_fingerprints.append(fingerprint)
        self._recent_fingerprints = self._recent_fingerprints[-self.config.repeated_action_limit :]
        if (
            len(self._recent_fingerprints) == self.config.repeated_action_limit
            and len(set(self._recent_fingerprints)) == 1
        ):
            return ToolResult(
                ok=False,
                summary=(
                    "The same action has already been attempted repeatedly. "
                    "Inspect the previous result or choose another action."
                ),
                error_code=ErrorCode.REPEATED_ACTION_LIMIT.value,
                recoverable=True,
            )
        return None

    def _emit_task_events(self, result: ToolResult) -> None:
        if result.content:
            self._emit(
                AgentEventType.TASK_OUTPUT,
                "Task produced output",
                {"resource_ref": result.resource_ref, "content": result.content},
            )
        if result.state:
            event_type = (
                AgentEventType.TASK_COMPLETED
                if result.state in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}
                else AgentEventType.TASK_STATUS_CHANGED
            )
            self._emit(event_type, result.summary, result.model_dump(exclude_none=True))

    def _transition(self, new_state: AgentState) -> None:
        old, new = self.machine.transition(new_state)
        self._emit(
            AgentEventType.STATE_CHANGED,
            f"{old.value} -> {new.value}",
            {"from": old.value, "to": new.value},
        )

    def _emit(
        self,
        event_type: AgentEventType,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.event_sink.emit(event_type, self.machine.state, summary, payload)

    def _result(
        self,
        started: float,
        final_state: AgentState,
        summary: str,
        details: str | None,
        error_code: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            final_state=final_state,
            summary=summary,
            details=details,
            iterations=self._iterations,
            tool_call_count=self.tool_call_count,
            tool_error_count=self.tool_error_count,
            duration_seconds=time.monotonic() - started,
            error_code=error_code,
        )
