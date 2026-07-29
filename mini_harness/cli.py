from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from pathlib import Path
from uuid import uuid4

import typer

from environment_runtime.broker import BrokerAddress, LocalBrokerServer
from environment_runtime.config import RuntimeSettings, SecuritySettings
from mini_harness.agent.controller import (
    FanoutEventSink,
    build_model_provider,
    build_sdk_controller,
)
from mini_harness.config import load_harness_config
from mini_harness.trace.writer import TraceWriter
from mini_harness.ui.console import RichEventRenderer

app = typer.Typer(help="Mini Harness Agent")


@app.callback()
def _main_callback() -> None:
    return None


@app.command("run")
def run(
    task: str,
    project: str = typer.Option(".", "--project", help="Project root to expose to the runtime."),
    config_file: str | None = typer.Option(
        None, "--config", help="Path to mini-harness TOML config."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="Model provider: openai, openai-compatible, or anthropic."
    ),
    model: str | None = typer.Option(None, "--model", help="Model name for real model mode."),
    fake_model: bool = typer.Option(
        False, "--fake-model", help="Use deterministic fake decisions."
    ),
    max_iterations: int = typer.Option(30, "--max-iterations"),
    runtime_url: str | None = typer.Option(None, "--runtime-url", help="Broker address override."),
    endpoint_id: str | None = typer.Option(None, "--endpoint-id"),
    environment_id: str | None = typer.Option(None, "--environment-id"),
    target_id: str | None = typer.Option(None, "--target-id"),
    verbose: bool = typer.Option(False, "--verbose"),
    no_color: bool = typer.Option(False, "--no-color"),
    embedded_broker: bool = typer.Option(
        False, "--embedded-broker", help="Start a local broker for this run."
    ),
) -> None:
    try:
        asyncio.run(
            _run_async(
                task=task,
                project=project,
                config_file=config_file,
                provider=provider,
                model=model,
                fake_model=fake_model,
                max_iterations=max_iterations,
                runtime_url=runtime_url,
                endpoint_id=endpoint_id,
                environment_id=environment_id,
                target_id=target_id,
                verbose=verbose,
                no_color=no_color,
                embedded_broker=embedded_broker,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


async def _run_async(
    task: str,
    project: str,
    config_file: str | None,
    provider: str | None,
    model: str | None,
    fake_model: bool,
    max_iterations: int,
    runtime_url: str | None,
    endpoint_id: str | None,
    environment_id: str | None,
    target_id: str | None,
    verbose: bool,
    no_color: bool,
    embedded_broker: bool,
) -> None:
    project_root = str(Path(project).resolve())
    harness_config = load_harness_config(
        config_path=config_file,
        project_root=project_root,
        model_override=model,
        provider_override=provider,
        max_iterations_override=max_iterations,
    )
    address = _address_from_runtime_url(runtime_url)
    settings = RuntimeSettings(security=SecuritySettings(allowed_local_roots=[Path(project_root)]))
    server, thread = _start_embedded_broker(settings, address) if embedded_broker else (None, None)
    if server is not None:
        address = server.address
    renderer = RichEventRenderer(no_color=no_color, verbose=verbose)
    trace = TraceWriter(project_root, task)
    config = harness_config.agent
    model_provider = build_model_provider(fake_model=fake_model, model_config=harness_config.model)
    renderer.startup(
        model="fake"
        if fake_model
        else f"{harness_config.model.provider}:{harness_config.model.model}",
        environment=environment_id or "ensure-local",
        project=project_root,
        max_iterations=max_iterations,
        transport="BrokerTransport",
    )
    sink = FanoutEventSink([trace, renderer])
    controller, client = build_sdk_controller(
        model_provider,
        config,
        sink,
        address=address,
        settings=settings,
    )
    try:
        result = await controller.run(
            task,
            project_root,
            endpoint_id=endpoint_id,
            environment_id=environment_id,
            target_id=target_id,
        )
        trace.write_summary(
            final_state=result.final_state.value,
            iterations=result.iterations,
            tool_call_count=result.tool_call_count,
            tool_error_count=result.tool_error_count,
            final_message=result.summary,
        )
        if result.error_code:
            raise typer.Exit(1)
    finally:
        with contextlib.suppress(Exception):
            client.close()
        if server is not None:
            await server.stop()
        if thread is not None:
            thread.join(timeout=10)


def _address_from_runtime_url(runtime_url: str | None) -> BrokerAddress | None:
    if not runtime_url:
        return None
    if os.name == "nt" and runtime_url.startswith("\\\\.\\pipe\\"):
        return BrokerAddress(runtime_url, "AF_PIPE")
    return BrokerAddress(runtime_url, "AF_UNIX")


def _start_embedded_broker(
    settings: RuntimeSettings,
    address: BrokerAddress | None,
) -> tuple[LocalBrokerServer, threading.Thread]:
    if address is None and os.name == "nt":
        address = BrokerAddress(rf"\\.\pipe\mini-harness-{uuid4().hex}", "AF_PIPE")
    elif address is None:
        address = BrokerAddress(
            str(Path(".mini-harness") / f"broker-{uuid4().hex}.sock"), "AF_UNIX"
        )
    server = LocalBrokerServer(settings, address)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve_forever()), daemon=True)
    thread.start()
    if not server.ready.wait(timeout=10):
        raise RuntimeError("embedded broker did not become ready")
    return server, thread


def main() -> None:
    app()
