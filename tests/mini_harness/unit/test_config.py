from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.config import load_harness_config


def test_load_config_supports_anthropic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("MINI_AGENT_MODEL", raising=False)
    monkeypatch.delenv("MINI_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[agent]
max_iterations = 9

[model]
provider = "anthropic"
model = "claude-test"
api_key = "from-file"
base_url = "https://anthropic.example"
max_tokens = 2048
""",
        encoding="utf-8",
    )

    config = load_harness_config(config_path=str(config_path), project_root=str(tmp_path))

    assert config.agent.max_iterations == 9
    assert config.model.provider == "anthropic"
    assert config.model.model == "claude-test"
    assert config.model.api_key == "from-file"
    assert config.model.base_url == "https://anthropic.example"
    assert config.model.max_tokens == 2048


def test_env_and_cli_override_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINI_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    config_path = tmp_path / "mini-harness.toml"
    config_path.write_text(
        """
[agent]
max_iterations = 5

[model]
provider = "openai"
model = "file-model"
api_key = "from-file"
""",
        encoding="utf-8",
    )

    config = load_harness_config(
        config_path=str(config_path),
        project_root=str(tmp_path),
        model_override="cli-model",
        max_iterations_override=3,
    )

    assert config.agent.max_iterations == 3
    assert config.model.provider == "anthropic"
    assert config.model.model == "cli-model"
    assert config.model.api_key == "from-env"
