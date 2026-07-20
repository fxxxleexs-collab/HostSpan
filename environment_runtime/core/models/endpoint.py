from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

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
