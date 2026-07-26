from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from environment_runtime.config import RuntimeSettings


@dataclass(frozen=True)
class BrokerAddress:
    address: str
    family: str


def default_broker_address(settings: RuntimeSettings) -> BrokerAddress:
    if os.name == "nt":
        digest = hashlib.sha256(str(Path.cwd()).encode("utf-8")).hexdigest()[:16]
        return BrokerAddress(rf"\\.\pipe\environment-runtime-{digest}", "AF_PIPE")
    data_dir = settings.runtime.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return BrokerAddress(str(data_dir / "broker.sock"), "AF_UNIX")
