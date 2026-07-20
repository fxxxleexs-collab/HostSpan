from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    url: str = "sqlite+aiosqlite:///./environment-runtime.db"


class RuntimeSection(BaseModel):
    data_dir: Path = Path("~/.environment-runtime").expanduser()
    log_level: str = "INFO"
    subscriber_queue_size: int = 1000


class SecuritySettings(BaseModel):
    allowed_local_roots: list[Path] = Field(default_factory=lambda: [Path.cwd()])
    allow_shell_commands: bool = False


class OutputSettings(BaseModel):
    max_inline_bytes: int = 1_048_576


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENVRT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
