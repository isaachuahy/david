from pathlib import Path

from loguru import logger
from sqlite_utils import Database

from runtime_paths import (
    get_db_path,
    get_telegram_persistence_path as resolve_telegram_persistence_path,
)


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def get_telegram_persistence_path() -> Path:
    path = resolve_telegram_persistence_path()
    _ensure_parent_dir(path)
    return path


def get_db() -> Database:
    """Returns a connection to the SQLite database, ensuring the directory exists."""
    db_path = get_db_path()
    _ensure_parent_dir(db_path)
    return Database(db_path)


def init_db():
    """Initializes the database schema and creates necessary tables if they don't exist."""
    logger.info("Initializing database schema...")
    db = get_db()

    # 1. Calendar Writes (Confirmation Queue)
    if "calendar_writes" not in db.table_names():
        db["calendar_writes"].create({
            "id": str,
            "summary": str,
            "start_time": str,  # ISO format
            "end_time": str,    # ISO format
            "description": str,
            "status": str,      # pending, confirmed, rejected, executed
            "created_at": str
        }, pk="id")
        logger.info("Created table: calendar_writes")

    # 2. Sessions
    if "sessions" not in db.table_names():
        db["sessions"].create({
            "id": str,
            "status": str,      # IDLE, ACTIVE, CLOSING
            "start_time": str,
            "end_time": str
        }, pk="id")
        logger.info("Created table: sessions")

    # 3. Decisions
    if "decisions" not in db.table_names():
        db["decisions"].create({
            "id": str,
            "session_id": str,
            "timestamp": str,
            "content": str,
        }, pk="id")
        logger.info("Created table: decisions")

    # 4. Weekly Snapshots
    if "weekly_snapshots" not in db.table_names():
        db["weekly_snapshots"].create({
            "id": str,
            "timestamp": str,
            "weekly_state_content": str,
        }, pk="id")
        logger.info("Created table: weekly_snapshots")

    logger.info("Database initialization complete.")


if __name__ == "__main__":
    init_db()
