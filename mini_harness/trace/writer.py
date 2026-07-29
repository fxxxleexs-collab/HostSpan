from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from mini_harness.agent.events import AgentEvent, AgentEventType
from mini_harness.agent.state import AgentState


class TraceWriter:
    def __init__(self, project_root: str, task: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.run_id = uuid4().hex
        self.run_dir = self.project_root / ".mini-harness" / "runs" / self.run_id
        self.events_path = self.run_dir / "events.jsonl"
        self.messages_path = self.run_dir / "messages.json"
        self.summary_path = self.run_dir / "summary.json"
        self.task = task
        self.events: list[AgentEvent] = []
        self.messages: list[dict] = []
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._write_messages()

    def emit(
        self,
        event_type: AgentEventType,
        state: AgentState,
        summary: str,
        payload: dict | None = None,
    ) -> AgentEvent:
        self._capture_model_payload(event_type, payload or {})
        event = AgentEvent(
            sequence=len(self.events) + 1,
            timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC),
            event_type=event_type,
            state=state,
            summary=summary,
            payload=_event_payload(event_type, payload or {}),
        )
        self.events.append(event)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return event

    def _capture_model_payload(self, event_type: AgentEventType, payload: dict) -> None:
        if event_type == AgentEventType.MODEL_REQUEST_STARTED:
            self.messages.append(
                {
                    "type": "request",
                    "messages": _sanitize(payload.get("messages", [])),
                    "tools": _sanitize(payload.get("tools", [])),
                }
            )
            self._write_messages()
        if event_type == AgentEventType.MODEL_REQUEST_COMPLETED:
            self.messages.append(
                {
                    "type": "response",
                    "decision": _sanitize(payload.get("decision", {})),
                }
            )
            self._write_messages()

    def _write_messages(self) -> None:
        self.messages_path.write_text(
            json.dumps(self.messages, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def write_summary(
        self,
        final_state: str,
        iterations: int,
        tool_call_count: int,
        tool_error_count: int,
        final_message: str,
    ) -> None:
        started = self.events[0].timestamp.isoformat() if self.events else None
        ended = self.events[-1].timestamp.isoformat() if self.events else None
        payload = {
            "run_id": self.run_id,
            "task": self.task,
            "start_time": started,
            "end_time": ended,
            "final_state": final_state,
            "iterations": iterations,
            "tool_call_count": tool_call_count,
            "tool_error_count": tool_error_count,
            "files_written": [
                event.payload.get("metadata", {}).get("path")
                for event in self.events
                if event.event_type == AgentEventType.TOOL_COMPLETED
                and event.payload.get("resource_ref", "").startswith("file:")
            ],
            "tasks_started": [
                event.payload.get("task_id")
                for event in self.events
                if event.event_type == AgentEventType.TASK_STARTED
            ],
            "final_message": final_message,
        }
        self.summary_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _sanitize(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if (
                "key" in key_text.lower()
                or "secret" in key_text.lower()
                or "token" in key_text.lower()
            ):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _event_payload(event_type: AgentEventType, payload: dict) -> dict:
    if event_type == AgentEventType.MODEL_REQUEST_STARTED:
        return {
            "message_count": len(payload.get("messages", [])),
            "tools": [
                str(tool.get("name", ""))
                for tool in payload.get("tools", [])
                if isinstance(tool, dict)
            ],
        }
    return _sanitize(payload)
