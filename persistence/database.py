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
            "proposal_item_id": str,
            "action_type": str,
            "summary": str,
            "start_time": str,  # ISO format
            "end_time": str,    # ISO format
            "description": str,
            "calendar_id": str,
            "target_event_id": str,
            "target_event_calendar_id": str,
            "status": str,      # pending, confirmed, rejected, executed
            "created_at": str,
            "created_event_id": str,
            "created_event_calendar_id": str,
        }, pk="id")
        logger.info("Created table: calendar_writes")
    else:
        table = db["calendar_writes"]
        existing_columns = {column.name for column in table.columns}
        if "calendar_id" not in existing_columns:
            table.add_column("calendar_id", str, not_null_default="primary")
            logger.info("Added column calendar_writes.calendar_id")
        if "action_type" not in existing_columns:
            table.add_column("action_type", str, not_null_default="schedule")
            logger.info("Added column calendar_writes.action_type")
        if "target_event_id" not in existing_columns:
            table.add_column("target_event_id", str)
            logger.info("Added column calendar_writes.target_event_id")
        if "target_event_calendar_id" not in existing_columns:
            table.add_column("target_event_calendar_id", str)
            logger.info("Added column calendar_writes.target_event_calendar_id")
        if "created_event_id" not in existing_columns:
            table.add_column("created_event_id", str)
            logger.info("Added column calendar_writes.created_event_id")
        if "created_event_calendar_id" not in existing_columns:
            table.add_column("created_event_calendar_id", str)
            logger.info("Added column calendar_writes.created_event_calendar_id")
        if "proposal_item_id" not in existing_columns:
            table.add_column("proposal_item_id", str)
            logger.info("Added column calendar_writes.proposal_item_id")

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

    # 5. Durable Sunday Review Workflows
    if "review_workflows" not in db.table_names():
        db["review_workflows"].create({
            "id": str,
            "workflow_status": str,
            "updated_at": str,
            "state_json": str,
        }, pk="id")
        logger.info("Created table: review_workflows")
    else:
        table = db["review_workflows"]
        existing_columns = {column.name for column in table.columns}
        if "workflow_status" not in existing_columns:
            table.add_column("workflow_status", str, not_null_default="active")
            logger.info("Added column review_workflows.workflow_status")
        if "updated_at" not in existing_columns:
            table.add_column("updated_at", str, not_null_default="")
            logger.info("Added column review_workflows.updated_at")
        if "state_json" not in existing_columns:
            table.add_column("state_json", str, not_null_default="{}")
            logger.info("Added column review_workflows.state_json")

    # 6. Proposal Threads
    if "proposal_threads" not in db.table_names():
        db["proposal_threads"].create({
            "id": str,
            "source_type": str,
            "source_id": str,
            "title": str,
            "status": str,
            "active_item_id": str,
            "created_at": str,
            "updated_at": str,
        }, pk="id")
        logger.info("Created table: proposal_threads")

    # 7. Proposal Items
    if "proposal_items" not in db.table_names():
        db["proposal_items"].create({
            "id": str,
            "thread_id": str,
            "item_type": str,
            "status": str,
            "sequence_index": int,
            "revision_count": int,
            "last_feedback": str,
            "action_type": str,
            "summary": str,
            "start_time": str,
            "end_time": str,
            "description": str,
            "calendar_id": str,
            "target_event_id": str,
            "target_event_calendar_id": str,
            "created_at": str,
            "updated_at": str,
        }, pk="id")
        logger.info("Created table: proposal_items")

    # 8. Artifact Writes
    if "artifact_writes" not in db.table_names():
        db["artifact_writes"].create({
            "id": str,
            "artifact_type": str,
            "content": str,
            "status": str,
            "source_type": str,
            "source_id": str,
            "source_stage": str,
            "attempts": int,
            "last_error": str,
            "created_at": str,
            "updated_at": str,
            "executed_at": str,
        }, pk="id")
        logger.info("Created table: artifact_writes")

    logger.info("Database initialization complete.")


if __name__ == "__main__":
    init_db()
