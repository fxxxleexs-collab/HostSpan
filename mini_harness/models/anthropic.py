from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from mini_harness.config import ModelConfig
from mini_harness.errors import ErrorCode, MiniHarnessError
from mini_harness.models.http_errors import format_http_status_error, should_retry_status
from mini_harness.models.openai_compatible import parse_final_decision
from mini_harness.models.schemas import AgentDecision, ModelMessage, ToolDecision
from mini_harness.tools.schemas import ToolDefinition


class AnthropicModelProvider:
    def __init__(
        self, config: ModelConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport

    async def decide(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
    ) -> AgentDecision:
        if not self.config.api_key:
            raise MiniHarnessError(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "ANTHROPIC_API_KEY or MINI_AGENT_API_KEY is required for Anthropic",
                recoverable=False,
            )
        if not self.config.model:
            raise MiniHarnessError(
                ErrorCode.MODEL_INVALID_RESPONSE,
                "model is required for Anthropic; set [model].model or MINI_AGENT_MODEL",
                recoverable=False,
            )
        payload = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": _system_prompt(messages),
            "messages": _anthropic_messages(messages),
            "tools": [_tool_payload(tool) for tool in tools],
            "tool_choice": {"type": "auto"},
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.config.base_url or "https://api.anthropic.com",
                    timeout=self.config.timeout_seconds,
                    transport=self.transport,
                    headers={
                        "x-api-key": self.config.api_key,
                        "anthropic-version": self.config.anthropic_version,
                    },
                ) as client:
                    response = await client.post(_messages_path(self.config), json=payload)
                response.raise_for_status()
                return _convert_response(response.json())
            except (httpx.TimeoutException, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise MiniHarnessError(
                        ErrorCode.MODEL_TIMEOUT,
                        "Anthropic model request timed out",
                        recoverable=True,
                    ) from exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if attempt >= self.config.max_retries or not should_retry_status(
                    exc.response.status_code
                ):
                    raise MiniHarnessError(
                        ErrorCode.MODEL_INVALID_RESPONSE,
                        format_http_status_error("Anthropic", exc),
                        recoverable=True,
                    ) from exc
            except MiniHarnessError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise MiniHarnessError(
                        ErrorCode.MODEL_INVALID_RESPONSE,
                        f"Anthropic model request failed: {type(exc).__name__}",
                        recoverable=True,
                    ) from exc
            await asyncio.sleep(0.25 * (attempt + 1))
        raise MiniHarnessError(
            ErrorCode.MODEL_INVALID_RESPONSE,
            f"Anthropic model request failed: {type(last_error).__name__}",
            recoverable=True,
        )


def _messages_path(config: ModelConfig) -> str:
    base_url = (config.base_url or "").rstrip("/")
    if base_url.endswith("/v1"):
        return "/messages"
    return "/v1/messages"


def _system_prompt(messages: list[ModelMessage]) -> str:
    return "\n\n".join(message.content for message in messages if message.role == "system")


def _anthropic_messages(messages: list[ModelMessage]) -> list[dict[str, str]]:
    converted: list[dict[str, str]] = []
    for message in messages:
        if message.role == "system":
            continue
        role = "assistant" if message.role == "assistant" else "user"
        content = message.content
        if message.role == "tool":
            content = f"Tool result from {message.name or 'tool'}:\n{message.content}"
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"] += "\n\n" + content
        else:
            converted.append({"role": role, "content": content})
    if not converted:
        converted.append({"role": "user", "content": "Continue."})
    return converted


def _tool_payload(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _convert_response(payload: dict[str, Any]) -> AgentDecision:
    content = payload.get("content")
    if not isinstance(content, list):
        raise MiniHarnessError(
            ErrorCode.MODEL_INVALID_RESPONSE,
            "Anthropic response did not include content blocks",
            recoverable=True,
        )
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {}
            return ToolDecision(
                type="tool",
                tool_name=str(block.get("name") or ""),
                arguments=arguments,
                reason_summary=_tool_reason(text_parts),
                raw_output="\n".join(
                    [
                        *text_parts,
                        json.dumps(block, ensure_ascii=False, indent=2),
                    ]
                ).strip(),
            )
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
    text = "\n".join(part for part in text_parts if part).strip()
    return parse_final_decision(text)


def _tool_reason(text_parts: list[str]) -> str:
    text = "\n".join(part.strip() for part in text_parts if part.strip()).strip()
    return text or "Model selected a tool call."
