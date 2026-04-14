import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger

from persistence.database import get_db
from integrations.calendar import insert_event
from orchestrator.time_utils import parse_iso
from persistence.models import CalendarWriteRecord, CalendarWriteStatus

# This module manages the confirmation queue for proposed calendar writes.
# When Gemini Flash identifies a message that requires calendar modification, it will create a pending write in the database. 
# The user can then confirm or reject these pending writes through the Telegram bot interface, which will call the confirm_write or reject_write functions.
# Confirmation queue is necessary to ensure that David does not make any calendar changes without explicit user approval, adding a layer of safety and control.

def add_pending_write(
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str = "",
    calendar_id: str = "primary",
) -> str:
    """
    Adds a proposed calendar write to the database and returns its unique ID.
    """
    db = get_db()
    write_id = f"cw_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    
    record = CalendarWriteRecord(
        id=write_id,
        summary=summary,
        start_time=start_time.isoformat(),
        end_time=end_time.isoformat(),
        description=description,
        calendar_id=calendar_id,
        status=CalendarWriteStatus.PENDING,
        created_at=now_iso
    )
    
    # Serialize enums to plain strings so fresh SQLite tables can be created cleanly.
    db["calendar_writes"].insert(record.model_dump(mode="json"))  # type: ignore
    logger.info(f"Added pending calendar write [{write_id}]: '{summary}'")
    return write_id

def get_pending_write(write_id: str) -> Optional[CalendarWriteRecord]:
    """Retrieves a pending write from the database by ID as a typed record."""
    db = get_db()
    try:
        row = db["calendar_writes"].get(write_id)  # type: ignore
        return CalendarWriteRecord(**row) if row else None
    except Exception as e:
        logger.error(f"Failed to retrieve pending write {write_id}: {e}")
        return None

def confirm_write(write_id: str) -> Optional[dict]:
    """
    Executes a pending write by pushing it to Google Calendar, then marks it executed.
    Returns the created event dictionary on success, otherwise None.
    """

    db = get_db()
    record = get_pending_write(write_id)
    
    if not record or record.status != CalendarWriteStatus.PENDING:
        logger.warning(f"Cannot confirm write {write_id}: not found or not pending.")
        return None
        
    # Lazy Expiration: Check if the proposal is older than 2 hours
    created_at = parse_iso(record.created_at)
    if datetime.now(timezone.utc) - created_at > timedelta(hours=2):
        logger.warning(f"Pending write {write_id} has expired (older than 2 hours). Auto-rejecting.")
        db["calendar_writes"].update(write_id, {"status": CalendarWriteStatus.EXPIRED.value})  # type: ignore
        return None
        
    logger.info(f"Confirming write {write_id}...")
    
    # Convert ISO strings back to datetime objects
    start_dt = parse_iso(record.start_time)
    end_dt = parse_iso(record.end_time)
    
    created_event = insert_event(
        summary=record.summary,
        start_time=start_dt,
        end_time=end_dt,
        description=record.description,
        calendar_id=record.calendar_id,
    )
    
    if created_event:
        db["calendar_writes"].update(
            write_id,
            {
                "status": CalendarWriteStatus.EXECUTED.value,
                "created_event_id": created_event.get("id"),
                "created_event_calendar_id": created_event.get("calendar_id", record.calendar_id),
            },
        )  # type: ignore
        logger.success(f"Successfully executed write {write_id} to Google Calendar.")
        # Return created_event for caching within same session
        return created_event
    else:
        logger.error(f"Failed to insert event for write {write_id} via API.")
        return None

def reject_write(write_id: str) -> bool:
    """Marks a pending write as rejected."""
    db = get_db()
    record = get_pending_write(write_id)
    
    if not record or record.status != CalendarWriteStatus.PENDING:
        logger.warning(f"Cannot reject write {write_id}: not found or not pending.")
        return False
        
    db["calendar_writes"].update(write_id, {"status": CalendarWriteStatus.REJECTED.value})  # type: ignore
    logger.info(f"Rejected pending write {write_id}.")
    return True
