from __future__ import annotations

import sys
from pathlib import Path

from environment_runtime.broker import BrokerAddress
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient
from mini_harness.agent.events import AgentEventSink, InMemoryEventSink
from mini_harness.agent.loop import AgentLoop, AgentRunResult
from mini_harness.config import AgentConfig, ModelConfig
from mini_harness.models.anthropic import AnthropicModelProvider
from mini_harness.models.base import ModelProvider
from mini_harness.models.fake import FakeModelProvider
from mini_harness.models.openai_compatible import OpenAICompatibleModelProvider
from mini_harness.models.schemas import FinalDecision, ToolDecision
from mini_harness.runtime.client import SDKRuntimeClient
from mini_harness.runtime.work_context import WorkContext
from mini_harness.tools.adapter import build_runtime_tools
from mini_harness.tools.registry import ToolRegistry


class FanoutEventSink:
    def __init__(self, sinks: list[AgentEventSink]) -> None:
        self.sinks = sinks

    def emit(self, event_type, state, summary, payload=None):
        event = None
        for sink in self.sinks:
            event = sink.emit(event_type, state, summary, payload)
        if event is None:
            raise RuntimeError("fanout sink has no targets")
        return event


class AgentController:
    def __init__(
        self,
        runtime_client: SDKRuntimeClient,
        model_provider: ModelProvider,
        config: AgentConfig | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.runtime_client = runtime_client
        self.model_provider = model_provider
        self.config = config or AgentConfig()
        self.event_sink = event_sink or InMemoryEventSink()

    async def run(
        self,
        task: str,
        project: str,
        endpoint_id: str | None = None,
        environment_id: str | None = None,
        target_id: str | None = None,
    ) -> AgentRunResult:
        project_path = str(Path(project).resolve())
        if endpoint_id and environment_id and target_id:
            work_context = WorkContext(
                endpoint_id=endpoint_id,
                environment_id=environment_id,
                target_id=target_id,
                project_root=project_path,
            )
        else:
            bundle = self.runtime_client.ensure_local("mini-harness-local", project_path)
            work_context = WorkContext(
                endpoint_id=str(bundle["endpoint"]["endpoint_id"]),
                environment_id=str(bundle["environment"]["environment_id"]),
                target_id=str(bundle["target_id"]),
                project_root=project_path,
            )

        registry = ToolRegistry(self.config)
        for tool in build_runtime_tools(self.runtime_client):
            registry.register(tool)
        loop = AgentLoop(
            model=self.model_provider,
            tools=registry,
            config=self.config,
            event_sink=self.event_sink,
        )
        return await loop.run(task, work_context)


def build_sdk_controller(
    model_provider: ModelProvider,
    config: AgentConfig,
    event_sink: AgentEventSink,
    address: BrokerAddress | None = None,
    settings: RuntimeSettings | None = None,
) -> tuple[AgentController, AgentRuntimeClient]:
    client = AgentRuntimeClient.from_broker(
        address=address,
        settings=settings,
        principal_id="mini-harness",
    )
    return AgentController(SDKRuntimeClient(client), model_provider, config, event_sink), client


def default_fake_model() -> FakeModelProvider:
    return FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="list_files",
                arguments={"path": ".", "recursive": False},
                reason_summary="First inspect the project files.",
            ),
            ToolDecision(
                type="tool",
                tool_name="read_file",
                arguments={"path": "test_calculator.py"},
                reason_summary="Read the failing test to understand expected behavior.",
            ),
            ToolDecision(
                type="tool",
                tool_name="read_file",
                arguments={"path": "calculator.py"},
                reason_summary="Read the implementation before editing it.",
            ),
            ToolDecision(
                type="tool",
                tool_name="run_command",
                arguments={"argv": [sys.executable, "-m", "pytest", "-q"], "cwd": "."},
                reason_summary="Run the tests through the runtime task API.",
            ),
            ToolDecision(
                type="tool",
                tool_name="observe_task",
                arguments={"wait_seconds": 10.0},
                reason_summary="Observe the pytest task and collect logs.",
            ),
            ToolDecision(
                type="tool",
                tool_name="write_file",
                arguments={
                    "path": "calculator.py",
                    "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
                },
                reason_summary="Fix add to perform addition.",
            ),
            ToolDecision(
                type="tool",
                tool_name="run_command",
                arguments={"argv": [sys.executable, "-m", "pytest", "-q"], "cwd": "."},
                reason_summary="Rerun tests after the fix.",
            ),
            ToolDecision(
                type="tool",
                tool_name="observe_task",
                arguments={"wait_seconds": 10.0},
                reason_summary="Confirm the verification task completed.",
            ),
            FinalDecision(
                type="final",
                summary="Fixed calculator.py and verified pytest passes.",
                details="The runtime task completed successfully after updating add().",
            ),
        ]
    )


def build_model_provider(
    fake_model: bool,
    model_config: ModelConfig | None = None,
    model_name: str | None = None,
) -> ModelProvider:
    if fake_model:
        return default_fake_model()
    config = model_config or ModelConfig.from_env(model_name)
    if model_name is not None:
        config = config.model_copy(update={"model": model_name})
    if config.provider == "anthropic":
        return AnthropicModelProvider(config)
    return OpenAICompatibleModelProvider(config)
