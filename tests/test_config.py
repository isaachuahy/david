from pathlib import Path
from unittest.mock import patch

import pytest

from config import ConfigError, MODEL_DEFAULTS, ROUTING_MODELS, get_model_name, get_routing_model, load_config


@pytest.fixture(autouse=True)
def routing_environment(monkeypatch):
    """Keep startup tests independent of locally configured routing credentials."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.delenv("DAVID_ROUTING_MODEL", raising=False)
    monkeypatch.delenv("DAVID_CONTEXT_EDITOR_URL", raising=False)
    monkeypatch.delenv("DAVID_CONTEXT_EDITOR_PORT", raising=False)


@patch("config._validate_google_auth_paths")
def test_load_config_reads_required_env_vars(mock_validate_paths, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("ALLOWED_USER_ID", "123")
    monkeypatch.setenv("DAVID_DB_PATH", "/tmp/david.db")
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "/tmp/token.json")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", "/tmp/credentials.json")

    config = load_config()

    assert config.telegram_bot_token == "telegram-token"
    assert config.gemini_api_key == "gemini-key"
    assert config.allowed_user_id == 123
    assert config.db_path == Path("/tmp/david.db")
    assert config.context_editor_url is None
    assert config.context_editor_port == 8080
    mock_validate_paths.assert_called_once_with(
        Path("/tmp/token.json"),
        Path("/tmp/credentials.json"),
    )


@patch("config._validate_google_auth_paths")
def test_load_config_supports_legacy_authorized_user_id(mock_validate_paths, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.delenv("ALLOWED_USER_ID", raising=False)
    monkeypatch.setenv("AUTHORIZED_USER_ID", "123")

    config = load_config()

    assert config.allowed_user_id == 123
    mock_validate_paths.assert_called_once()


def test_load_config_requires_integer_allowed_user_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("ALLOWED_USER_ID", "abc")

    with pytest.raises(ConfigError, match="ALLOWED_USER_ID must be an integer"):
        load_config()


def test_load_config_requires_routing_credentials(monkeypatch):
    """Missing production routing access must fail at startup, not the first turn."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("DAVID_ROUTING_MODEL", "openai/gpt-5.6-luna")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config()


@pytest.mark.parametrize("model", ROUTING_MODELS)
def test_routing_candidates_use_low_reasoning(monkeypatch, model):
    """Production candidates use the centrally configured model and reasoning."""
    monkeypatch.setenv("DAVID_ROUTING_MODEL", model)
    selected = get_routing_model()
    assert selected.model == model
    assert selected.provider == ("gemini" if model.startswith("gemini-") else "openrouter")
    assert selected.reasoning == "low"


@patch("config._validate_google_auth_paths")
def test_native_gemini_does_not_require_openrouter(mock_validate_paths, monkeypatch):
    """The old selector also migrates to native Gemini without another API key."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("ALLOWED_USER_ID", "123")
    monkeypatch.setenv("DAVID_ROUTING_MODEL", "google/gemini-3.5-flash-lite")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert get_routing_model().provider == "gemini"
    assert get_routing_model().model == "gemini-3.5-flash-lite"
    load_config()


def test_unknown_routing_model_is_rejected(monkeypatch):
    """A misspelled model ID must not silently change the selected model."""
    monkeypatch.setenv("DAVID_ROUTING_MODEL", "typo")
    with pytest.raises(ConfigError, match="Unsupported DAVID_ROUTING_MODEL"):
        get_routing_model()


@pytest.mark.parametrize("role", MODEL_DEFAULTS)
def test_model_roles_support_independent_environment_overrides(monkeypatch, role):
    """Changing one model role must not require credentials or code edits."""
    env_name = f"GEMINI_{role.upper()}_MODEL"
    monkeypatch.delenv(env_name, raising=False)
    assert get_model_name(role) == MODEL_DEFAULTS[role]

    monkeypatch.setenv(env_name, "  custom-model  ")
    assert get_model_name(role) == "custom-model"

    monkeypatch.setenv(env_name, "   ")
    assert get_model_name(role) == MODEL_DEFAULTS[role]


@pytest.fixture
def editor_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-telegram-token")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setenv("ALLOWED_USER_ID", "123")
    monkeypatch.setattr("config._validate_google_auth_paths", lambda *args: None)


@pytest.mark.parametrize("url", ["https://context.example.com/context", "https://context.example.com:443/context"])
def test_editor_accepts_https_page_url(editor_environment, monkeypatch, url):
    monkeypatch.setenv("DAVID_CONTEXT_EDITOR_URL", url)
    assert load_config().context_editor_url == url


@pytest.mark.parametrize("url", [
    "http://context.example.com/context", "https:///context", "https://context.example.com/",
    "https://user:password@context.example.com/context", "https://context.example.com/context?token=x",
    "https://context.example.com/context#fragment", "https://context.example.com:8443/context",
    "https://context.example.com:invalid/context", "https://[broken/context", "https://bad host/context",
])
def test_editor_rejects_invalid_public_urls(editor_environment, monkeypatch, url):
    monkeypatch.setenv("DAVID_CONTEXT_EDITOR_URL", url)
    with pytest.raises(ConfigError, match="DAVID_CONTEXT_EDITOR_URL") as error:
        load_config()
    assert "password" not in str(error.value)


@pytest.mark.parametrize("port", [1024, 8080, 65535])
def test_editor_accepts_unprivileged_local_port(editor_environment, monkeypatch, port):
    monkeypatch.setenv("DAVID_CONTEXT_EDITOR_URL", "https://context.example.com/context")
    monkeypatch.setenv("DAVID_CONTEXT_EDITOR_PORT", str(port))
    assert load_config().context_editor_port == port


@pytest.mark.parametrize("port", ["0", "443", "65536", "abc", "1.5"])
def test_editor_rejects_invalid_local_ports(editor_environment, monkeypatch, port):
    monkeypatch.setenv("DAVID_CONTEXT_EDITOR_URL", "https://context.example.com/context")
    monkeypatch.setenv("DAVID_CONTEXT_EDITOR_PORT", port)
    with pytest.raises(ConfigError, match="DAVID_CONTEXT_EDITOR_PORT"):
        load_config()
