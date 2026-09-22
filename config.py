import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from loguru import logger
from runtime_paths import (
    DEFAULT_DB_PATH,
    get_google_credentials_path,
    get_google_token_path,
)


load_dotenv()


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str
    allowed_user_id: int
    gemini_api_key: str
    db_path: Path
    google_token_path: Path
    google_credentials_path: Path


def _get_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


ModelRole = Literal["chat", "review", "review_fallback", "synthesis"]

# Keep provider model IDs in one place. Each role can be changed independently
# through GEMINI_<ROLE>_MODEL without editing the conversation or review flows.
MODEL_DEFAULTS: dict[ModelRole, str] = {
    "chat": "gemini-3-flash-preview",
    "review": "gemini-3-flash-preview",
    "review_fallback": "gemini-3.1-pro-preview",
    "synthesis": "gemini-3-flash-preview",
}


def get_model_name(role: ModelRole) -> str:
    """Resolves a model role without requiring bot credentials or calendar access."""
    default = MODEL_DEFAULTS[role]
    return _get_env(f"GEMINI_{role.upper()}_MODEL") or default


@dataclass(frozen=True)
class RoutingModelConfig:
    """Keeps production routing settings independent of the transport client."""

    provider: str
    model: str
    reasoning: str
    timeout_seconds: float = 10


ROUTING_MODELS = {
    "gemini-3.5-flash-lite": RoutingModelConfig(
        "gemini", "gemini-3.5-flash-lite", "low",
    ),
    "openai/gpt-5.6-luna": RoutingModelConfig(
        "openrouter", "openai/gpt-5.6-luna", "low",
    ),
}
DEFAULT_ROUTING_MODEL = "gemini-3.5-flash-lite"

# Published limits checked 2026-09-18. Gemini publishes an input limit;
# Luna publishes a combined input/output window. Keep those denominators distinct.
# https://ai.google.dev/gemini-api/docs/models
# https://developers.openai.com/api/docs/models/gpt-5.6-luna
MODEL_CONTEXT_LIMITS = {
    ("gemini", "gemini-3.5-flash-lite"): ("input", 1_048_576),
    ("gemini", "gemini-3-flash-preview"): ("input", 1_048_576),
    ("gemini", "gemini-3.1-pro-preview"): ("input", 1_048_576),
    ("openrouter", "openai/gpt-5.6-luna"): ("input_and_output", 1_050_000),
}


def get_routing_model() -> RoutingModelConfig:
    """Selects a production candidate without experimental endpoint or price pins."""
    model = _get_env("DAVID_ROUTING_MODEL") or DEFAULT_ROUTING_MODEL
    # Existing deployments may still use the old OpenRouter-shaped selector.
    # Preserve that setting while sending all Gemini routing directly to Google.
    if model == "google/gemini-3.5-flash-lite":
        model = "gemini-3.5-flash-lite"
    if model not in ROUTING_MODELS:
        raise ConfigError(f"Unsupported DAVID_ROUTING_MODEL: {model}")
    return ROUTING_MODELS[model]


def _require_env(name: str, *, placeholder_values: set[str] | None = None) -> str:
    value = _get_env(name)
    if value is None:
        raise ConfigError(f"Missing required environment variable: {name}")

    if placeholder_values and value in placeholder_values:
        raise ConfigError(f"Environment variable {name} is still set to a placeholder value.")

    return value


def _load_allowed_user_id() -> int:
    raw_user_id = _get_env("ALLOWED_USER_ID")
    if raw_user_id is None:
        raw_user_id = _get_env("AUTHORIZED_USER_ID")
        if raw_user_id is not None:
            logger.warning(
                "AUTHORIZED_USER_ID is deprecated; rename it to ALLOWED_USER_ID before deployment."
            )

    if raw_user_id is None:
        raise ConfigError("Missing required environment variable: ALLOWED_USER_ID")

    try:
        return int(raw_user_id)
    except ValueError as exc:
        raise ConfigError("ALLOWED_USER_ID must be an integer Telegram user ID.") from exc


def _resolve_path(env_name: str, default: Path) -> Path:
    value = _get_env(env_name)
    if value is None:
        return default
    return Path(value).expanduser()


def _validate_google_auth_paths(token_path: Path, credentials_path: Path) -> None:
    if token_path.exists():
        return

    if credentials_path.exists():
        logger.warning(
            "Google Calendar token file is missing at {}. The bot can start, but the first "
            "calendar request will require interactive OAuth. For VPS deployment, pre-create "
            "the token file before going live.",
            token_path,
        )
        return

    raise ConfigError(
        "Google Calendar credentials are not ready. Expected either an existing token at "
        f"{token_path} or OAuth client credentials at {credentials_path}."
    )


def load_config() -> AppConfig:
    """Loads and validates the runtime configuration required to boot David."""
    telegram_bot_token = _require_env(
        "TELEGRAM_BOT_TOKEN",
        placeholder_values={"your_telegram_bot_token_here"},
    )
    gemini_api_key = _require_env(
        "GEMINI_API_KEY",
        placeholder_values={"your_gemini_api_key_here"},
    )
    # Validate routing access at startup instead of failing on the first turn.
    if get_routing_model().provider == "openrouter":
        _require_env("OPENROUTER_API_KEY", placeholder_values={"your_openrouter_api_key_here"})
    allowed_user_id = _load_allowed_user_id()

    db_path = _resolve_path("DAVID_DB_PATH", DEFAULT_DB_PATH)
    google_token_path = get_google_token_path()
    google_credentials_path = get_google_credentials_path()

    _validate_google_auth_paths(google_token_path, google_credentials_path)

    return AppConfig(
        telegram_bot_token=telegram_bot_token,
        allowed_user_id=allowed_user_id,
        gemini_api_key=gemini_api_key,
        db_path=db_path,
        google_token_path=google_token_path,
        google_credentials_path=google_credentials_path,
    )
