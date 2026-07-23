from __future__ import annotations

import pytest

from environment_runtime.core.errors import ValidationError
from environment_runtime.services.endpoint import EndpointService


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_ssh_endpoint_persists_config_and_capabilities(runtime, tmp_path) -> None:
    known_hosts = tmp_path / "known_hosts"
    identity = tmp_path / "id_ed25519"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")
    identity.write_text("not-a-real-key")

    endpoint = await EndpointService(runtime).add_ssh(
        name="ssh-demo",
        hostname="example.test",
        username="envrt",
        port=2222,
        identity_file=str(identity),
        use_ssh_agent=False,
        known_hosts_file=str(known_hosts),
    )

    assert endpoint.provider_type == "ssh"
    assert endpoint.config["hostname"] == "example.test"
    assert endpoint.config["port"] == 2222
    assert "ssh_transport" in endpoint.capabilities
    assert "remote_execution" in endpoint.capabilities
    assert "remote_filesystem" in endpoint.capabilities


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_ssh_rejects_no_identity_when_agent_disabled(runtime, tmp_path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")

    with pytest.raises(ValidationError):
        await EndpointService(runtime).add_ssh(
            name="ssh-demo",
            hostname="example.test",
            username="envrt",
            use_ssh_agent=False,
            known_hosts_file=str(known_hosts),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_health_dispatches_endpoint_config(runtime, tmp_path) -> None:
    class FakeSSHTransportProvider:
        def __init__(self) -> None:
            self.endpoint_id: str | None = None

        async def healthcheck(self, endpoint):
            self.endpoint_id = endpoint.endpoint_id
            return {"status": "ok", "hostname": endpoint.config["hostname"]}

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")
    fake_provider = FakeSSHTransportProvider()
    runtime.providers.transport["ssh"] = fake_provider

    endpoint = await EndpointService(runtime).add_ssh(
        name="ssh-demo",
        hostname="example.test",
        username="envrt",
        known_hosts_file=str(known_hosts),
    )
    health = await EndpointService(runtime).health(endpoint.endpoint_id)

    assert fake_provider.endpoint_id == endpoint.endpoint_id
    assert health["status"] == "ok"
    assert health["hostname"] == "example.test"
