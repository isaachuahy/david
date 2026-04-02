import os
from sqlite_utils import Database
from loguru import logger

# Resolve the absolute path to the data directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "assistant.db")
TELEGRAM_PERSISTENCE_PATH = os.path.join(DATA_DIR, "telegram_state.pkl")

def get_db() -> Database:
    """Returns a connection to the SQLite database, ensuring the directory exists."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    return Database(DB_PATH)

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
