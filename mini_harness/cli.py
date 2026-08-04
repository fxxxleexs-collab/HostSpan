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

app = typer.Typer(help="Mini Harness Agent", invoke_without_command=True)


@app.callback()
def _main_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    typer.echo("Mini Harness Agent interactive launcher")
    project = typer.prompt("Project root", default=str(Path.cwd()))
    config_file = typer.prompt("Config file (blank for auto-discovery)", default="")
    task = typer.prompt("Task")
    fake_model = typer.confirm("Use fake model?", default=False)
    verbose = typer.confirm("Verbose output?", default=True)
    try:
        asyncio.run(
            _run_async(
                task=task,
                project=project,
                config_file=config_file or None,
                provider=None,
                model=None,
                fake_model=fake_model,
                max_iterations=30,
                runtime_mode=None,
                ssh_host=None,
                ssh_user=None,
                ssh_port=None,
                ssh_key=None,
                ssh_known_hosts=None,
                remote_root=None,
                runtime_url=None,
                endpoint_id=None,
                environment_id=None,
                target_id=None,
                verbose=verbose,
                no_color=False,
                embedded_broker=True,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    raise typer.Exit()


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
    runtime_mode: str | None = typer.Option(
        None, "--runtime-mode", help="Runtime target mode: local or ssh."
    ),
    ssh_host: str | None = typer.Option(None, "--ssh-host", help="SSH hostname override."),
    ssh_user: str | None = typer.Option(None, "--ssh-user", help="SSH username override."),
    ssh_port: int | None = typer.Option(None, "--ssh-port", help="SSH port override."),
    ssh_key: str | None = typer.Option(None, "--ssh-key", help="SSH identity file override."),
    ssh_known_hosts: str | None = typer.Option(
        None, "--ssh-known-hosts", help="SSH known_hosts file override."
    ),
    remote_root: str | None = typer.Option(
        None, "--remote-root", help="Remote project root for SSH mode."
    ),
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
                runtime_mode=runtime_mode,
                ssh_host=ssh_host,
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                ssh_key=ssh_key,
                ssh_known_hosts=ssh_known_hosts,
                remote_root=remote_root,
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
    runtime_mode: str | None,
    ssh_host: str | None,
    ssh_user: str | None,
    ssh_port: int | None,
    ssh_key: str | None,
    ssh_known_hosts: str | None,
    remote_root: str | None,
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
        runtime_mode_override=runtime_mode,
        ssh_host_override=ssh_host,
        ssh_user_override=ssh_user,
        ssh_port_override=ssh_port,
        ssh_key_override=ssh_key,
        ssh_known_hosts_override=ssh_known_hosts,
        remote_root_override=remote_root,
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
        environment=environment_id or f"ensure-{harness_config.runtime.mode}",
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
        runtime_config=harness_config.runtime,
        sandbox_config=harness_config.sandbox,
        permissions_config=harness_config.permissions,
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
