from __future__ import annotations

import sys

import pytest

from mini_harness.agent.controller import AgentController, default_fake_model
from mini_harness.agent.events import InMemoryEventSink
from mini_harness.agent.state import AgentState
from mini_harness.config import AgentConfig
from mini_harness.runtime.client import SDKRuntimeClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fake_model_repairs_sample_project_through_sdk(
    broker_client,
    sample_project_copy,
) -> None:
    client, _, _ = broker_client
    sink = InMemoryEventSink()
    controller = AgentController(
        SDKRuntimeClient(client),
        default_fake_model(),
        AgentConfig(max_iterations=20),
        sink,
    )

    result = await controller.run(
        "检查测试失败的原因，修改代码并确保所有测试通过。",
        str(sample_project_copy),
    )
    bundle = client.environments.ensure_local("mini-harness-local", str(sample_project_copy))
    final_task = client.tasks.start(
        bundle["environment"]["environment_id"],
        bundle["target_id"],
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(sample_project_copy),
        persistent=True,
    )
    final = client.tasks.wait(final_task["task_id"], timeout_seconds=20)

    assert result.final_state == AgentState.COMPLETED
    assert (
        client.files.read_text(bundle["endpoint"]["endpoint_id"], "calculator.py")
        .strip()
        .endswith("return a + b")
    )
    assert final["state"] == "SUCCEEDED"
    assert final["exit_code"] == 0
    assert [event.sequence for event in sink.events] == list(range(1, len(sink.events) + 1))
