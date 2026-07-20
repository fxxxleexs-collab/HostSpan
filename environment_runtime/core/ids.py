from __future__ import annotations

from uuid import uuid4

PREFIXES = {
    "endpoint": "endpoint",
    "environment": "env",
    "workspace": "workspace",
    "replica": "replica",
    "binding": "binding",
    "target": "target",
    "session": "session",
    "task": "task",
    "artifact": "artifact",
    "input": "input",
    "lease": "lease",
    "forward": "forward",
    "event": "event",
    "revision": "revision",
    "root": "root",
}


def new_id(kind: str) -> str:
    prefix = PREFIXES.get(kind, kind)
    return f"{prefix}_{uuid4().hex}"
