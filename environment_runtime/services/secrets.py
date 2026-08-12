from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from environment_runtime.core.errors import ValidationError


@dataclass
class InMemorySecretStore:
    _values: dict[str, str] = field(default_factory=dict)
    _metadata: dict[str, dict[str, str]] = field(default_factory=dict)

    def put(self, value: str, *, purpose: str = "runtime") -> str:
        if not value:
            raise ValidationError("secret value cannot be empty")
        secret_ref = f"secret:{uuid4().hex}"
        self._values[secret_ref] = value
        self._metadata[secret_ref] = {
            "purpose": purpose,
            "created_at": datetime.now(UTC).isoformat(),
        }
        return secret_ref

    def get(self, secret_ref: str) -> str:
        value = self._values.get(secret_ref)
        if value is None:
            raise ValidationError(f"secret ref was not found or has expired: {secret_ref}")
        return value

    def delete(self, secret_ref: str) -> bool:
        existed = secret_ref in self._values
        self._values.pop(secret_ref, None)
        self._metadata.pop(secret_ref, None)
        return existed

    def metadata(self, secret_ref: str) -> dict[str, str]:
        return dict(self._metadata.get(secret_ref, {}))
