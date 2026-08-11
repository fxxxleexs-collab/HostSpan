from __future__ import annotations

import json

import httpx
import pytest

from mini_harness.config import ModelConfig
from mini_harness.errors import MiniHarnessError
from mini_harness.models.anthropic import AnthropicModelProvider
from mini_harness.models.schemas import FinalDecision, ModelMessage, ToolDecision
from mini_harness.tools.schemas import ToolDefinition


@pytest.mark.asyncio
async def test_anthropic_provider_converts_tool_use() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": "I will read the file before changing anything.",
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "calculator.py"},
                    },
                ]
            },
        )

    provider = AnthropicModelProvider(
        ModelConfig(
            provider="anthropic",
            model="claude-test",
            api_key="secret",
            base_url="https://anthropic.example",
        ),
        transport=httpx.MockTransport(handler),
    )

    decision = await provider.decide(
        [
            ModelMessage(role="system", content="system rules"),
            ModelMessage(role="user", content="fix tests"),
        ],
        [
            ToolDefinition(
                name="read_file",
                description="Read file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
    )

    assert isinstance(decision, ToolDecision)
    assert decision.tool_name == "read_file"
    assert decision.arguments == {"path": "calculator.py"}
    assert decision.reason_summary == "I will read the file before changing anything."
    assert decision.raw_output is not None
    assert "tool_use" in decision.raw_output
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["system"] == "system rules"
    assert payload["tools"] == [
        {
            "name": "read_file",
            "description": "Read file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


@pytest.mark.asyncio
async def test_anthropic_provider_converts_structured_final() -> None:
    provider = AnthropicModelProvider(
        ModelConfig(
            provider="anthropic",
            model="claude-test",
            api_key="secret",
            base_url="https://anthropic.example",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "text",
                            "text": '{"type":"final","summary":"done","details":"tests passed"}',
                        }
                    ]
                },
            )
        ),
    )

    decision = await provider.decide(
        [ModelMessage(role="user", content="finish")],
        [],
    )

    assert isinstance(decision, FinalDecision)
    assert decision.summary == "done"
    assert decision.details == "tests passed"
    assert decision.raw_output == '{"type":"final","summary":"done","details":"tests passed"}'


@pytest.mark.asyncio
async def test_anthropic_provider_accepts_plain_text_final_output() -> None:
    raw = "Done.\n\nI checked the project and no changes were needed."
    provider = AnthropicModelProvider(
        ModelConfig(
            provider="anthropic",
            model="claude-test",
            api_key="secret",
            base_url="https://anthropic.example",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "text",
                            "text": raw,
                        }
                    ]
                },
            )
        ),
    )

    decision = await provider.decide(
        [ModelMessage(role="user", content="finish")],
        [],
    )

    assert isinstance(decision, FinalDecision)
    assert decision.summary == "Done."
    assert decision.details == "I checked the project and no changes were needed."
    assert decision.raw_output == raw


@pytest.mark.asyncio
async def test_anthropic_provider_preserves_internal_model_errors() -> None:
    provider = AnthropicModelProvider(
        ModelConfig(
            provider="anthropic",
            model="claude-test",
            api_key="secret",
            base_url="https://anthropic.example",
            max_retries=0,
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"content": [{"type": "text", "text": ""}]},
            )
        ),
    )

    with pytest.raises(MiniHarnessError) as exc_info:
        await provider.decide([ModelMessage(role="user", content="finish")], [])

    message = str(exc_info.value)
    assert "model final response was empty" in message
    assert "MiniHarnessError" not in message


@pytest.mark.asyncio
async def test_anthropic_provider_reports_http_error_body() -> None:
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        return httpx.Response(
            401,
            headers={"anthropic-request-id": "req_test"},
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            },
            request=request,
        )

    provider = AnthropicModelProvider(
        ModelConfig(
            provider="anthropic",
            model="claude-test",
            api_key="bad-key",
            base_url="https://api.anthropic.com/v1",
            max_retries=2,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MiniHarnessError) as exc_info:
        await provider.decide([ModelMessage(role="user", content="hello")], [])

    assert captured_paths == ["/v1/messages"]
    message = str(exc_info.value)
    assert "HTTP 401" in message
    assert "request_id=req_test" in message
    assert "authentication_error" in message
    assert "invalid x-api-key" in message
