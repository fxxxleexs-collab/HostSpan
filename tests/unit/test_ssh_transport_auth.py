from __future__ import annotations

import pytest

from environment_runtime.core.models import Endpoint
from environment_runtime.providers.transport import ssh as ssh_transport
from environment_runtime.providers.transport.ssh import SSHTransportProvider


class FakeSSHConnection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_transport_passes_password_auth(monkeypatch, tmp_path) -> None:
    calls: list[dict[str, object]] = []
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")

    async def fake_connect(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        return FakeSSHConnection()

    monkeypatch.setattr(ssh_transport.asyncssh, "connect", fake_connect)
    endpoint = Endpoint(
        name="ssh-demo",
        provider_type="ssh",
        config={
            "hostname": "example.test",
            "username": "envrt",
            "known_hosts_file": str(known_hosts),
            "auth_method": "password",
            "password_secret_ref": "secret:test",
            "use_ssh_agent": False,
        },
    )

    await SSHTransportProvider(lambda secret_ref: {"secret:test": "secret"}[secret_ref]).connect(
        endpoint
    )

    assert calls[0]["password"] == "secret"
    assert calls[0]["client_keys"] is None
    assert calls[0]["agent_path"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_transport_passes_key_auth(monkeypatch, tmp_path) -> None:
    calls: list[dict[str, object]] = []
    known_hosts = tmp_path / "known_hosts"
    identity = tmp_path / "id_ed25519"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")
    identity.write_text("not-a-real-key")

    async def fake_connect(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        return FakeSSHConnection()

    monkeypatch.setattr(ssh_transport.asyncssh, "connect", fake_connect)
    endpoint = Endpoint(
        name="ssh-demo",
        provider_type="ssh",
        config={
            "hostname": "example.test",
            "username": "envrt",
            "known_hosts_file": str(known_hosts),
            "auth_method": "key",
            "identity_file": str(identity),
            "use_ssh_agent": False,
        },
    )

    await SSHTransportProvider().connect(endpoint)

    assert calls[0]["password"] is None
    assert calls[0]["client_keys"] == [str(identity)]
    assert calls[0]["agent_path"] is None
