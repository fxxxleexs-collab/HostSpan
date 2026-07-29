from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
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
            self.console.print(f"[dim][STATE][/dim] {event.summary}")
            return
        if event.event_type == AgentEventType.TOOL_SELECTED:
            self.console.print(f"[cyan][THINK][/cyan] {event.summary}")
            return
        if event.event_type == AgentEventType.TOOL_STARTED:
            self.console.print(f"[blue][TOOL][/blue] {event.payload.get('tool_name', '')}")
            if self.verbose:
                self.console.print(event.payload.get("arguments", {}))
            return
        if event.event_type == AgentEventType.TOOL_COMPLETED:
            self.console.print(f"[green][OK][/green] {event.summary}")
            return
        if event.event_type == AgentEventType.TOOL_FAILED:
            self.console.print(f"[red][ERROR][/red] {event.summary}")
            return
        if event.event_type == AgentEventType.TASK_STARTED:
            self.console.print(f"[magenta][TASK][/magenta] {event.summary}")
            return
        if event.event_type == AgentEventType.TASK_OUTPUT:
            content = str(event.payload.get("content", ""))
            if content:
                self.console.print(
                    Panel(content, title=str(event.payload.get("resource_ref", "task output")))
                )
            return
        if event.event_type in {AgentEventType.AGENT_COMPLETED, AgentEventType.AGENT_FAILED}:
            style = "green" if event.event_type == AgentEventType.AGENT_COMPLETED else "red"
            self.console.print(
                Panel(Text(event.summary), title=event.event_type.value, style=style)
            )
            return
        if self.verbose:
            self.console.print(f"[dim]{event.event_type.value}[/dim] {event.summary}")
