from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONTEXT_DIR = BASE_DIR / "context"
DEFAULT_DB_PATH = BASE_DIR / "data" / "assistant.db"
DEFAULT_TELEGRAM_PERSISTENCE_PATH = BASE_DIR / "data" / "telegram_state.pkl"
DEFAULT_GOOGLE_TOKEN_PATH = BASE_DIR / "token.json"
DEFAULT_GOOGLE_CREDENTIALS_PATH = BASE_DIR / "credentials.json"


def _path_from_env(env_name: str, default: Path) -> Path:
    value = os.getenv(env_name)
    if value is None:
        return default
    return Path(value).expanduser()


def get_context_dir() -> Path:
    return _path_from_env("DAVID_CONTEXT_DIR", DEFAULT_CONTEXT_DIR)


def get_db_path() -> Path:
    return _path_from_env("DAVID_DB_PATH", DEFAULT_DB_PATH)


def get_telegram_persistence_path() -> Path:
    return _path_from_env(
        "DAVID_TELEGRAM_PERSISTENCE_PATH",
        DEFAULT_TELEGRAM_PERSISTENCE_PATH,
    )


def get_google_token_path() -> Path:
    return _path_from_env("GOOGLE_TOKEN_PATH", DEFAULT_GOOGLE_TOKEN_PATH)


def get_google_credentials_path() -> Path:
    return _path_from_env(
        "GOOGLE_CREDENTIALS_PATH",
        DEFAULT_GOOGLE_CREDENTIALS_PATH,
    )
