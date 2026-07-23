from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from ..capabilities import Capability
from ..ids import new_id


class EndpointStatus(StrEnum):
    DECLARED = "DECLARED"
    READY = "READY"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"


class Endpoint(BaseModel):
    endpoint_id: str = Field(default_factory=lambda: new_id("endpoint"))
    name: str
    provider_type: str
    config: dict = Field(default_factory=dict)
    capabilities: set[Capability] = Field(default_factory=set)
    status: EndpointStatus = EndpointStatus.DECLARED


class SSHEndpointConfig(BaseModel):
    hostname: str
    port: int = 22
    username: str
    identity_file: str | None = None
    use_ssh_agent: bool = True
    known_hosts_file: str
    proxy_jump: str | None = None
    connect_timeout: float = 15.0
    keepalive_interval: float = 20.0

    @field_validator("hostname", "username", "known_hosts_file")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be empty")
        return value

    @field_validator("port")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if value <= 0 or value > 65535:
            raise ValueError("port must be between 1 and 65535")
        return value
