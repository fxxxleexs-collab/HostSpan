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

    async def run(self, command: str, check: bool = False):
        _ = command, check
        return FakeSSHRunResult(
            stdout=(
                "ENVRT_REMOTE_PROBE_BEGIN\n"
                "uname_s=Linux\n"
                "uname_m=x86_64\n"
                "shell=/bin/bash\n"
                "sh_path=/bin/sh\n"
                "bash_path=/bin/bash\n"
                "python3_path=/usr/bin/python3\n"
                "python3_version=Python 3.11.2\n"
                "python_path=\n"
                "nohup_path=/usr/bin/nohup\n"
                "tmux_path=/usr/bin/tmux\n"
                "tmux_version=tmux 3.4\n"
                "sudo_path=/usr/bin/sudo\n"
                "ENVRT_REMOTE_PROBE_END\n"
            ),
            exit_status=0,
        )


class FakeSSHRunResult:
    def __init__(self, *, stdout: str, exit_status: int) -> None:
        self.stdout = stdout
        self.exit_status = exit_status


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
    assert "client_factory" not in calls[0]


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
    assert "client_factory" not in calls[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_transport_trust_host_once_uses_unverified_next_connect(
    monkeypatch, tmp_path
) -> None:
    calls: list[dict[str, object]] = []
    missing_known_hosts = tmp_path / "missing_known_hosts"
    identity = tmp_path / "id_ed25519"
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
            "known_hosts_file": str(missing_known_hosts),
            "auth_method": "key",
            "identity_file": str(identity),
            "use_ssh_agent": False,
        },
    )
    provider = SSHTransportProvider()

    provider.trust_host_once(endpoint.endpoint_id)
    await provider.connect(endpoint)

    assert calls[0]["known_hosts"] == b""
    assert calls[0]["client_factory"] != ()
    assert calls[0]["server_host_key_algs"] == "default"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_transport_does_not_skip_host_key_check_without_one_shot_approval(
    monkeypatch, tmp_path
) -> None:
    calls: list[dict[str, object]] = []
    known_hosts = tmp_path / "known_hosts"
    identity = tmp_path / "id_ed25519"
    known_hosts.write_text("")
    identity.write_text("not-a-real-key")

    async def fake_connect(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        if "client_factory" not in kwargs:
            raise OSError("Host key is not trusted")
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
    provider = SSHTransportProvider()

    with pytest.raises(Exception, match="Host key is not trusted"):
        await provider.connect(endpoint)

    provider.trust_host_once(endpoint.endpoint_id)
    await provider.connect(endpoint)

    assert "client_factory" not in calls[0]
    assert calls[1]["known_hosts"] == b""
    assert calls[1]["client_factory"] != ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ssh_healthcheck_returns_remote_environment(monkeypatch, tmp_path) -> None:
    known_hosts = tmp_path / "known_hosts"
    identity = tmp_path / "id_ed25519"
    known_hosts.write_text("example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n")
    identity.write_text("not-a-real-key")

    async def fake_connect(*args, **kwargs):
        _ = args, kwargs
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

    health = await SSHTransportProvider().healthcheck(endpoint)

    assert health["status"] == "ok"
    assert health["environment"]["uname_s"] == "Linux"
    assert health["environment"]["python3_path"] == "/usr/bin/python3"
    assert health["environment"]["tmux_version"] == "tmux 3.4"
