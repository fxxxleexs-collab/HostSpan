from __future__ import annotations

import sys
from pathlib import Path

from environment_runtime.broker import BrokerAddress
from environment_runtime.config import RuntimeSettings
from environment_runtime.sdk import AgentRuntimeClient, RuntimePolicy
from mini_harness.agent.events import AgentEventSink, InMemoryEventSink
from mini_harness.agent.loop import AgentLoop, AgentRunResult
from mini_harness.approvals import ApprovalHandler
from mini_harness.config import AgentConfig, ModelConfig, RuntimeConfig
from mini_harness.context.messages import AgentContext, ContextCompactResult
from mini_harness.models.anthropic import AnthropicModelProvider
from mini_harness.models.base import ModelProvider
from mini_harness.models.fake import FakeModelProvider
from mini_harness.models.openai_compatible import OpenAICompatibleModelProvider
from mini_harness.models.schemas import FinalDecision, ToolDecision
from mini_harness.permissions import PermissionsConfig
from mini_harness.runtime.client import SDKRuntimeClient
from mini_harness.runtime.ssh_trust import approve_trust_host_once, is_untrusted_host_key_error
from mini_harness.runtime.work_context import WorkContext
from mini_harness.sync.config import SyncConfig
from mini_harness.tools.adapter import build_runtime_tools
from mini_harness.tools.registry import ToolRegistry
from mini_harness.tools.runtime.remote import probe_remote_environment
from mini_harness.workspace import SandboxConfig


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
        runtime_config: RuntimeConfig | None = None,
        sandbox_config: SandboxConfig | None = None,
        permissions_config: PermissionsConfig | None = None,
        sync_config: SyncConfig | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self.runtime_client = runtime_client
        self.model_provider = model_provider
        self.config = config or AgentConfig()
        self.runtime_config = runtime_config or RuntimeConfig()
        self.sandbox_config = sandbox_config or SandboxConfig()
        self.permissions_config = permissions_config or PermissionsConfig()
        self.sync_config = sync_config or SyncConfig()
        self.approval_handler = approval_handler
        self.event_sink = event_sink or InMemoryEventSink()

    async def run(
        self,
        task: str,
        project: str,
        endpoint_id: str | None = None,
        environment_id: str | None = None,
        target_id: str | None = None,
    ) -> AgentRunResult:
        session = self.start_session(
            project,
            endpoint_id=endpoint_id,
            environment_id=environment_id,
            target_id=target_id,
        )
        return await session.run_turn(task)

    def start_session(
        self,
        project: str,
        endpoint_id: str | None = None,
        environment_id: str | None = None,
        target_id: str | None = None,
    ) -> AgentSession:
        work_context = self._build_work_context(
            project,
            endpoint_id=endpoint_id,
            environment_id=environment_id,
            target_id=target_id,
        )
        return AgentSession(self, work_context)

    def _build_work_context(
        self,
        project: str,
        endpoint_id: str | None = None,
        environment_id: str | None = None,
        target_id: str | None = None,
    ) -> WorkContext:
        project_path = str(Path(project).resolve())
        if endpoint_id and environment_id and target_id:
            return WorkContext(
                endpoint_id=endpoint_id,
                environment_id=environment_id,
                target_id=target_id,
                project_root=project_path,
                runtime_mode=self.runtime_config.mode,
                runtime_name=self.runtime_config.name,
                remote_root=self.runtime_config.ssh.remote_root,
                remote_hostname=self.runtime_config.ssh.hostname,
                remote_username=self.runtime_config.ssh.username,
                remote_port=self.runtime_config.ssh.port,
                remote_auth_method=self.runtime_config.ssh.auth_method,
                sandbox_config=self.sandbox_config,
                sync_config=self.sync_config,
                approval_handler=self.approval_handler,
            )
        if self.runtime_config.mode == "ssh":
            local_bundle = self.runtime_client.ensure_local("mini-harness-local", project_path)
            return WorkContext(
                endpoint_id=str(local_bundle["endpoint"]["endpoint_id"]),
                environment_id=str(local_bundle["environment"]["environment_id"]),
                target_id=str(local_bundle["target_id"]),
                project_root=project_path,
                runtime_mode="local",
                runtime_name=self.runtime_config.name,
                remote_root=self.runtime_config.ssh.remote_root,
                remote_hostname=self.runtime_config.ssh.hostname,
                remote_username=self.runtime_config.ssh.username,
                remote_port=self.runtime_config.ssh.port,
                remote_auth_method=self.runtime_config.ssh.auth_method,
                local_endpoint_id=str(local_bundle["endpoint"]["endpoint_id"]),
                local_environment_id=str(local_bundle["environment"]["environment_id"]),
                local_target_id=str(local_bundle["target_id"]),
                sandbox_config=self.sandbox_config,
                sync_config=self.sync_config,
                approval_handler=self.approval_handler,
            )
        bundle = self.runtime_client.ensure_local("mini-harness-local", project_path)
        return WorkContext(
            endpoint_id=str(bundle["endpoint"]["endpoint_id"]),
            environment_id=str(bundle["environment"]["environment_id"]),
            target_id=str(bundle["target_id"]),
            project_root=project_path,
            runtime_name=self.runtime_config.name,
            remote_hostname=self.runtime_config.ssh.hostname,
            remote_username=self.runtime_config.ssh.username,
            remote_port=self.runtime_config.ssh.port,
            remote_auth_method=self.runtime_config.ssh.auth_method,
            sandbox_config=self.sandbox_config,
            sync_config=self.sync_config,
            approval_handler=self.approval_handler,
        )

    async def ensure_remote_runtime(self, work_context: WorkContext) -> None:
        if self.runtime_config.mode != "ssh" or work_context.runtime_mode == "ssh":
            return
        password_secret_ref = await self._prepare_ssh_password_secret()
        try:
            bundle = self._ensure_ssh_bundle_with_root(
                password_secret_ref=password_secret_ref,
                trust_host_once=False,
            )
        except Exception as exc:
            if not is_untrusted_host_key_error(exc) or not await approve_trust_host_once(
                self.approval_handler,
                tool_name="ssh_connect",
                ssh=self.runtime_config.ssh,
                error=exc,
            ):
                if password_secret_ref is not None:
                    self.runtime_client.delete_secret(password_secret_ref)
                raise
            try:
                bundle = self._ensure_ssh_bundle_with_root(
                    password_secret_ref=password_secret_ref,
                    trust_host_once=True,
                )
            except Exception:
                if password_secret_ref is not None:
                    self.runtime_client.delete_secret(password_secret_ref)
                raise
        endpoint = bundle["endpoint"]
        environment = bundle["environment"]
        remote_root = self.runtime_config.ssh.remote_root
        work_context.endpoint_id = str(endpoint["endpoint_id"])
        work_context.environment_id = str(environment["environment_id"])
        work_context.target_id = str(bundle["target_id"])
        work_context.runtime_mode = "ssh"
        work_context.remote_root = remote_root
        work_context.remote_hostname = self.runtime_config.ssh.hostname
        work_context.remote_username = self.runtime_config.ssh.username
        work_context.remote_port = self.runtime_config.ssh.port
        work_context.remote_auth_method = self.runtime_config.ssh.auth_method
        work_context.remote_os = "unknown"
        work_context.remote_shell = "unknown"
        work_context.refresh_workspace_policy()
        await probe_remote_environment(self.runtime_client, work_context)

    def _ensure_ssh_bundle_with_root(
        self,
        *,
        password_secret_ref: str | None,
        trust_host_once: bool,
    ) -> dict:
        bundle = self.runtime_client.ensure_ssh(
            self.runtime_config.name,
            self.runtime_config.ssh,
            password_secret_ref=password_secret_ref,
            trust_host_once=trust_host_once,
        )
        endpoint = bundle["endpoint"]
        remote_root = self.runtime_config.ssh.remote_root
        self.runtime_client.ensure_dir(str(endpoint["endpoint_id"]), remote_root)
        return bundle

    async def _prepare_ssh_password_secret(self) -> str | None:
        if self.runtime_config.ssh.auth_method != "password":
            return None
        if self.approval_handler is None:
            raise ValueError("ssh password auth requires interactive secret input")
        prompt_secret = getattr(self.approval_handler, "prompt_secret", None)
        if prompt_secret is None:
            raise ValueError("ssh password auth requires interactive secret input")
        password = await prompt_secret(
            f"SSH password for {self.runtime_config.ssh.username}@{self.runtime_config.ssh.hostname}"
        )
        if not password:
            raise ValueError("ssh password auth was cancelled")
        return self.runtime_client.put_secret(password, purpose="ssh-password")

    def build_tool_registry(self) -> ToolRegistry:
        registry = ToolRegistry(
            self.config,
            permission_policy=self.permissions_config.build_policy(),
            approval_handler=self.approval_handler,
            approve_sandbox_denials=self.permissions_config.approve_sandbox_denials,
            approve_terminal_open=self.permissions_config.approve_terminal_open,
            approve_root_escalation=self.permissions_config.approve_root_escalation,
        )
        for tool in build_runtime_tools(self.runtime_client):
            registry.register(tool)
        return registry


class AgentSession:
    def __init__(self, controller: AgentController, work_context: WorkContext) -> None:
        self.controller = controller
        self.work_context = work_context
        self.context = AgentContext(controller.config, work_context)

    async def run_turn(self, task: str) -> AgentRunResult:
        await self.controller.ensure_remote_runtime(self.work_context)
        registry = self.controller.build_tool_registry()
        loop = AgentLoop(
            model=self.controller.model_provider,
            tools=registry,
            config=self.controller.config,
            event_sink=self.controller.event_sink,
        )
        return await loop.run_with_context(task, self.work_context, self.context)

    def compact_context(self) -> ContextCompactResult:
        return self.context.compact(reason="manual")


def build_sdk_controller(
    model_provider: ModelProvider,
    config: AgentConfig,
    event_sink: AgentEventSink,
    address: BrokerAddress | None = None,
    settings: RuntimeSettings | None = None,
    runtime_config: RuntimeConfig | None = None,
    sandbox_config: SandboxConfig | None = None,
    permissions_config: PermissionsConfig | None = None,
    sync_config: SyncConfig | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> tuple[AgentController, AgentRuntimeClient]:
    runtime_config = runtime_config or RuntimeConfig()
    client = AgentRuntimeClient.from_broker(
        address=address,
        settings=settings,
        principal_id="mini-harness",
        policy=RuntimePolicy(
            remote_terminal_backend="ssh_tmux" if runtime_config.ssh.prefer_tmux else "ssh_pty",
            allow_ssh_pty_fallback=runtime_config.ssh.allow_ssh_pty_fallback,
        ),
    )
    return (
        AgentController(
            SDKRuntimeClient(client),
            model_provider,
            config,
            event_sink,
            runtime_config,
            sandbox_config,
            permissions_config,
            sync_config,
            approval_handler,
        ),
        client,
    )


def default_fake_model() -> FakeModelProvider:
    return FakeModelProvider(
        [
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={"action": "list", "path": ".", "recursive": False},
                reason_summary="First inspect the project files.",
            ),
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={"action": "read", "path": "test_calculator.py"},
                reason_summary="Read the failing test to understand expected behavior.",
            ),
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={"action": "read", "path": "calculator.py"},
                reason_summary="Read the implementation before editing it.",
            ),
            ToolDecision(
                type="tool",
                tool_name="command",
                arguments={
                    "action": "run",
                    "argv": [*_test_python_argv(), "-m", "pytest", "-q"],
                    "cwd": ".",
                },
                reason_summary="Run the tests through the runtime task API.",
            ),
            ToolDecision(
                type="tool",
                tool_name="task",
                arguments={"action": "observe", "wait_seconds": 10.0},
                reason_summary="Observe the pytest task and collect logs.",
            ),
            ToolDecision(
                type="tool",
                tool_name="file",
                arguments={
                    "action": "write",
                    "path": "calculator.py",
                    "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
                },
                reason_summary="Fix add to perform addition.",
            ),
            ToolDecision(
                type="tool",
                tool_name="command",
                arguments={
                    "action": "run",
                    "argv": [*_test_python_argv(), "-m", "pytest", "-q"],
                    "cwd": ".",
                },
                reason_summary="Rerun tests after the fix.",
            ),
            ToolDecision(
                type="tool",
                tool_name="task",
                arguments={"action": "observe", "wait_seconds": 10.0},
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


def _test_python_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        local_venv_python = Path.cwd() / ".venv" / "Scripts" / "python.exe"
        if local_venv_python.exists():
            return [str(local_venv_python)]
        return ["python"]
    return [sys.executable]
