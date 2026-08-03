from __future__ import annotations

import httpx
import pytest

from mini_harness.config import ModelConfig
from mini_harness.errors import MiniHarnessError
from mini_harness.models.openai_compatible import OpenAICompatibleModelProvider
from mini_harness.models.schemas import FinalDecision, ModelMessage, ToolDecision


@pytest.mark.asyncio
async def test_openai_provider_keeps_raw_tool_call_output() -> None:
    provider = OpenAICompatibleModelProvider(
        ModelConfig(
            provider="openai",
            model="test-model",
            api_key="test-key",
            base_url="https://api.openai.test/v1",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "I will inspect the calculator implementation first.",
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"calculator.py"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        ),
    )

    decision = await provider.decide([ModelMessage(role="user", content="read")], [])

    assert isinstance(decision, ToolDecision)
    assert decision.tool_name == "read_file"
    assert decision.arguments == {"path": "calculator.py"}
    assert decision.reason_summary == "I will inspect the calculator implementation first."
    assert decision.raw_output is not None
    assert "tool_calls" in decision.raw_output


@pytest.mark.asyncio
async def test_openai_provider_keeps_raw_final_output() -> None:
    raw = '{"type":"final","summary":"done","details":"ok"}'
    provider = OpenAICompatibleModelProvider(
        ModelConfig(
            provider="openai",
            model="test-model",
            api_key="test-key",
            base_url="https://api.openai.test/v1",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": raw}}]},
            )
        ),
    )

    decision = await provider.decide([ModelMessage(role="user", content="finish")], [])

    assert isinstance(decision, FinalDecision)
    assert decision.summary == "done"
    assert decision.raw_output == raw


@pytest.mark.asyncio
async def test_openai_provider_reports_http_error_body() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            400,
            headers={"x-request-id": "req_openai_test"},
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "unsupported_model",
                    "message": "model does not support tools",
                }
            },
            request=request,
        )

    provider = OpenAICompatibleModelProvider(
        ModelConfig(
            provider="openai",
            model="bad-model",
            api_key="bad-key",
            base_url="https://api.openai.test/v1",
            max_retries=2,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MiniHarnessError) as exc_info:
        await provider.decide([ModelMessage(role="user", content="hello")], [])

    assert calls["count"] == 1
    message = str(exc_info.value)
    assert "HTTP 400" in message
    assert "request_id=req_openai_test" in message
    assert "invalid_request_error" in message
    assert "unsupported_model" in message
    assert "model does not support tools" in message
