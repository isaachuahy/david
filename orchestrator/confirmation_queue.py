import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger

from persistence.database import get_db
from integrations.calendar import insert_event

# This module manages the confirmation queue for proposed calendar writes.
# When Gemini Flash identifies a message that requires calendar modification, it will create a pending write in the database. 
# The user can then confirm or reject these pending writes through the Telegram bot interface, which will call the confirm_write or reject_write functions.
# Confirmation queue is necessary to ensure that David does not make any calendar changes without explicit user approval, adding a layer of safety and control.

def add_pending_write(summary: str, start_time: datetime, end_time: datetime, description: str = "") -> str:
    """
    Adds a proposed calendar write to the database and returns its unique ID.
    """
    db = get_db()
    write_id = f"cw_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    
    record = {
        "id": write_id,
        "summary": summary,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "description": description,
        "status": "pending",
        "created_at": now_iso
    }
    
    db["calendar_writes"].insert(record)  # type: ignore
    logger.info(f"Added pending calendar write [{write_id}]: '{summary}'")
    return write_id

def get_pending_write(write_id: str) -> Optional[dict]:
    """Retrieves a pending write from the database by ID."""
    db = get_db()
    try:
        return db["calendar_writes"].get(write_id)  # type: ignore
    except Exception as e:
        logger.error(f"Failed to retrieve pending write {write_id}: {e}")
        return None

def confirm_write(write_id: str) -> bool:
    """
    Executes a pending write by pushing it to Google Calendar, then marks it executed.
    """
    db = get_db()
    record = get_pending_write(write_id)
    
    if not record or record["status"] != "pending":
        logger.warning(f"Cannot confirm write {write_id}: not found or not pending.")
        return False
        
    # Lazy Expiration: Check if the proposal is older than 2 hours
    created_at = datetime.fromisoformat(record["created_at"])
    if datetime.now(timezone.utc) - created_at > timedelta(hours=2):
        logger.warning(f"Pending write {write_id} has expired (older than 2 hours). Auto-rejecting.")
        db["calendar_writes"].update(write_id, {"status": "expired"})  # type: ignore
        return False
        
    logger.info(f"Confirming write {write_id}...")
    
    # Convert ISO strings back to datetime objects
    start_dt = datetime.fromisoformat(record["start_time"])
    end_dt = datetime.fromisoformat(record["end_time"])
    
    created_event = insert_event(
        summary=record["summary"],
        start_time=start_dt,
        end_time=end_dt,
        description=record["description"]
    )
    
    if created_event:
        db["calendar_writes"].update(write_id, {"status": "executed"})  # type: ignore
        logger.success(f"Successfully executed write {write_id} to Google Calendar.")
        return True
    else:
        logger.error(f"Failed to insert event for write {write_id} via API.")
        return False

def reject_write(write_id: str) -> bool:
    """Marks a pending write as rejected."""
    db = get_db()
    record = get_pending_write(write_id)
    
    if not record or record["status"] != "pending":
        logger.warning(f"Cannot reject write {write_id}: not found or not pending.")
        return False
        
    db["calendar_writes"].update(write_id, {"status": "rejected"})  # type: ignore
    logger.info(f"Rejected pending write {write_id}.")
    return True
