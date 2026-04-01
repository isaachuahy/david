import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "assistant.db"
DEFAULT_GOOGLE_TOKEN_PATH = BASE_DIR / "token.json"
DEFAULT_GOOGLE_CREDENTIALS_PATH = BASE_DIR / "credentials.json"


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
    allowed_user_id = _load_allowed_user_id()

    db_path = _resolve_path("DAVID_DB_PATH", DEFAULT_DB_PATH)
    google_token_path = _resolve_path("GOOGLE_TOKEN_PATH", DEFAULT_GOOGLE_TOKEN_PATH)
    google_credentials_path = _resolve_path(
        "GOOGLE_CREDENTIALS_PATH",
        DEFAULT_GOOGLE_CREDENTIALS_PATH,
    )

    _validate_google_auth_paths(google_token_path, google_credentials_path)

    return AppConfig(
        telegram_bot_token=telegram_bot_token,
        allowed_user_id=allowed_user_id,
        gemini_api_key=gemini_api_key,
        db_path=db_path,
        google_token_path=google_token_path,
        google_credentials_path=google_credentials_path,
    )
