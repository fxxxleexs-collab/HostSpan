from __future__ import annotations

import asyncio
import json

import typer

from environment_runtime.api.app import create_app
from environment_runtime.config import RuntimeSettings
from environment_runtime.services.artifact import ArtifactService
from environment_runtime.services.endpoint import EndpointService
from environment_runtime.services.environment import EnvironmentService
from environment_runtime.services.runtime import build_runtime, shutdown_runtime
from environment_runtime.services.security import WriterLeaseService
from environment_runtime.services.session import SessionService
from environment_runtime.services.task import TaskService
from environment_runtime.services.workspace import WorkspaceService

app = typer.Typer(help="Environment Runtime CLI")
endpoint_app = typer.Typer()
env_app = typer.Typer()
workspace_app = typer.Typer()
task_app = typer.Typer()
session_app = typer.Typer()
artifact_app = typer.Typer()

app.add_typer(endpoint_app, name="endpoint")
app.add_typer(env_app, name="env")
app.add_typer(workspace_app, name="workspace")
app.add_typer(task_app, name="task")
app.add_typer(session_app, name="session")
app.add_typer(artifact_app, name="artifact")


def print_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


async def with_runtime(callback):
    runtime = await build_runtime(RuntimeSettings())
    try:
        return await callback(runtime)
    finally:
        await shutdown_runtime(runtime)


@endpoint_app.command("add-local")
def endpoint_add_local(name: str, root: str) -> None:
    async def _run(runtime):
        result = await EndpointService(runtime).add_local(name, root)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@endpoint_app.command("add-ssh")
def endpoint_add_ssh(
    name: str,
    hostname: str,
    username: str,
    known_hosts_file: str,
    port: int = 22,
    identity_file: str | None = None,
    use_ssh_agent: bool = True,
    proxy_jump: str | None = None,
    connect_timeout: float = 15.0,
    keepalive_interval: float = 20.0,
) -> None:
    async def _run(runtime):
        result = await EndpointService(runtime).add_ssh(
            name=name,
            hostname=hostname,
            username=username,
            known_hosts_file=known_hosts_file,
            port=port,
            identity_file=identity_file,
            use_ssh_agent=use_ssh_agent,
            proxy_jump=proxy_jump,
            connect_timeout=connect_timeout,
            keepalive_interval=keepalive_interval,
        )
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@endpoint_app.command("list")
def endpoint_list() -> None:
    async def _run(runtime):
        result = await EndpointService(runtime).list_all()
        print_json([item.model_dump(mode="json") for item in result])

    asyncio.run(with_runtime(_run))


@endpoint_app.command("health")
def endpoint_health(endpoint_id: str) -> None:
    async def _run(runtime):
        result = await EndpointService(runtime).health(endpoint_id)
        print_json(result)

    asyncio.run(with_runtime(_run))


@env_app.command("create")
def env_create(name: str, endpoint_ids: list[str]) -> None:
    async def _run(runtime):
        result = await EnvironmentService(runtime).create(name, endpoint_ids)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@env_app.command("list")
def env_list() -> None:
    async def _run(runtime):
        result = await EnvironmentService(runtime).list_all()
        print_json([item.model_dump(mode="json") for item in result])

    asyncio.run(with_runtime(_run))


@env_app.command("inspect")
def env_inspect(environment_id: str) -> None:
    async def _run(runtime):
        result = await EnvironmentService(runtime).get(environment_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@env_app.command("reconcile")
def env_reconcile(environment_id: str) -> None:
    async def _run(runtime):
        result = await EnvironmentService(runtime).reconcile(environment_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@workspace_app.command("create")
def workspace_create(name: str) -> None:
    async def _run(runtime):
        result = await WorkspaceService(runtime).create(name)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@workspace_app.command("add-root")
def workspace_add_root(workspace_id: str, logical_path: str) -> None:
    async def _run(runtime):
        result = await WorkspaceService(runtime).add_root(workspace_id, logical_path)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@workspace_app.command("add-replica")
def workspace_add_replica(workspace_id: str, endpoint_id: str, physical_root: str) -> None:
    async def _run(runtime):
        result = await WorkspaceService(runtime).add_replica(workspace_id, endpoint_id, physical_root)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@workspace_app.command("bind")
def workspace_bind(workspace_id: str, source_replica_id: str, target_replica_id: str) -> None:
    async def _run(runtime):
        result = await WorkspaceService(runtime).bind(workspace_id, source_replica_id, target_replica_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@workspace_app.command("revision")
def workspace_revision(workspace_id: str, replica_id: str) -> None:
    async def _run(runtime):
        result = await WorkspaceService(runtime).create_revision(workspace_id, replica_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@workspace_app.command("sync")
def workspace_sync(workspace_id: str, binding_id: str) -> None:
    async def _run(runtime):
        result = await WorkspaceService(runtime).sync(workspace_id, binding_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@workspace_app.command("status")
def workspace_status() -> None:
    async def _run(runtime):
        result = await WorkspaceService(runtime).list_all()
        print_json([item.model_dump(mode="json") for item in result])

    asyncio.run(with_runtime(_run))


@task_app.command("run")
@task_app.command("start")
def task_start(environment_id: str, target_id: str, argv: list[str], cwd: str = "") -> None:
    async def _run(runtime):
        result = await TaskService(runtime).start(
            environment_id,
            target_id,
            argv,
            cwd=cwd or None,
        )
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@task_app.command("list")
def task_list() -> None:
    async def _run(runtime):
        result = await TaskService(runtime).list_all()
        print_json([item.model_dump(mode="json") for item in result])

    asyncio.run(with_runtime(_run))


@task_app.command("inspect")
def task_inspect(task_id: str) -> None:
    async def _run(runtime):
        result = await TaskService(runtime).get(task_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@task_app.command("logs")
def task_logs(task_id: str) -> None:
    async def _run(runtime):
        result = await TaskService(runtime).logs(task_id)
        print_json(result)

    asyncio.run(with_runtime(_run))


@task_app.command("cancel")
def task_cancel(task_id: str) -> None:
    async def _run(runtime):
        result = await TaskService(runtime).cancel(task_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@session_app.command("create")
def session_create(environment_id: str, target_id: str, argv: list[str], cwd: str = "") -> None:
    async def _run(runtime):
        result = await SessionService(runtime).create(
            environment_id, target_id, argv, cwd=cwd or None
        )
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@session_app.command("list")
def session_list() -> None:
    async def _run(runtime):
        result = await SessionService(runtime).list_all()
        print_json([item.model_dump(mode="json") for item in result])

    asyncio.run(with_runtime(_run))


@session_app.command("inspect")
def session_inspect(session_id: str) -> None:
    async def _run(runtime):
        result = await SessionService(runtime).get(session_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@session_app.command("attach")
def session_attach(session_id: str, owner_id: str) -> None:
    async def _run(runtime):
        lease = await WriterLeaseService(runtime).acquire(session_id, "human", owner_id, force=True)
        print_json(lease.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@session_app.command("terminate")
def session_terminate(session_id: str) -> None:
    async def _run(runtime):
        result = await SessionService(runtime).terminate(session_id)
        print_json(result.model_dump(mode="json"))

    asyncio.run(with_runtime(_run))


@artifact_app.command("list")
def artifact_list() -> None:
    async def _run(runtime):
        result = await ArtifactService(runtime).list_all()
        print_json([item.model_dump(mode="json") for item in result])

    asyncio.run(with_runtime(_run))


@artifact_app.command("download")
def artifact_download(artifact_id: str, destination: str) -> None:
    async def _run(runtime):
        path = await ArtifactService(runtime).download(artifact_id, destination)
        print_json({"path": path})

    asyncio.run(with_runtime(_run))


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
