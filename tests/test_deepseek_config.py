"""Offline composition tests for the DeepSeek Flash Agent path."""

from __future__ import annotations

from roboagent.model.client import OpenAICompatibleModel

from roserver.app import create_app
from roserver.config import Settings


def test_deepseek_flash_config_builds_a_vision_capable_model(tmp_path, monkeypatch):
    config = tmp_path / "deepseek.yaml"
    config.write_text(
        """default_model: deepseek-flash
models:
  - name: deepseek-flash
    provider: deepseek
    params:
      model: deepseek-flash
      api_key: ${DEEPSEEK_API_KEY}
      reasoning_effort: low
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    app = create_app(Settings(data_dir=tmp_path, model_config_path=config))

    model = app.state.service.agent.model
    assert isinstance(model, OpenAICompatibleModel)
    assert model.model_name == "deepseek-flash"
    assert model.api_key == "test-key"
    assert model.preserve_reasoning_content is True
    assert model.artifact_reader is not None


def test_deepseek_flash_config_loads_key_from_sibling_dotenv(tmp_path, monkeypatch):
    config = tmp_path / "deepseek.yaml"
    config.write_text(
        """default_model: deepseek-flash
models:
  - name: deepseek-flash
    provider: deepseek
    params:
      model: deepseek-flash
      api_key: ${DEEPSEEK_API_KEY}
      reasoning_effort: low
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=key-from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    app = create_app(Settings(data_dir=tmp_path, model_config_path=config))

    model = app.state.service.agent.model
    assert isinstance(model, OpenAICompatibleModel)
    assert model.api_key == "key-from-dotenv"
