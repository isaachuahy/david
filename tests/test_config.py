from pathlib import Path
from unittest.mock import patch

import pytest

from config import ConfigError, load_config


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
