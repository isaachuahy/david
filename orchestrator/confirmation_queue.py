import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger

from observability.sentry import capture_exception as capture_sentry_exception
from persistence.database import get_db
from integrations.calendar import insert_event, delete_event, update_event
from orchestrator.time_utils import parse_iso
from persistence.models import (
    CalendarWriteRecord,
    CalendarWriteStatus,
    ProposalItemRecord,
    ProposalItemStatus,
    ProposalThreadRecord,
    ProposalThreadStatus,
)
from reasoning.schemas import ProposedEvent

# This module manages revision-aware calendar proposal threads.
# Model-generated proposals are stored as ProposalItemRecord drafts first, so
# user feedback can revise the active item without creating a calendar write.
# CalendarWriteRecord is created only after the user confirms a proposal item;
# confirm_write/reject_write remain the execution layer for accepted writes.

def add_pending_write(
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str = "",
    calendar_id: str = "primary",
    action_type: str = "schedule",
    target_event_id: Optional[str] = None,
    target_event_calendar_id: Optional[str] = None,
    proposal_item_id: Optional[str] = None,
) -> str:
    """
    Adds a proposed calendar write to the database and returns its unique ID.
    """
    db = get_db()
    write_id = f"cw_{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    record = CalendarWriteRecord(
        id=write_id,
        proposal_item_id=proposal_item_id,
        action_type=action_type,
        summary=summary,
        start_time=start_time.isoformat(),
        end_time=end_time.isoformat(),
        description=description,
        calendar_id=calendar_id,
        target_event_id=target_event_id,
        target_event_calendar_id=target_event_calendar_id,
        status=CalendarWriteStatus.PENDING,
        created_at=now_iso
    )

    # Serialize enums to plain strings so fresh SQLite tables can be created cleanly.
    db["calendar_writes"].insert(record.model_dump(mode="json"))  # type: ignore
    logger.info(f"Added pending calendar write [{write_id}] ({action_type}): '{summary}'")
    return write_id


def create_proposal_thread(
    *,
    source_type: str,
    source_id: Optional[str] = None,
    title: str = "",
) -> ProposalThreadRecord:
    """
    Creates a durable proposal thread for one related calendar-planning intent.

    Threads are shared by normal conversations and Sunday review. They provide
    the stable scope needed to revise one active proposal item without losing
    the rest of the queue or confusing unrelated calendar topics.
    """
    db = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    record = ProposalThreadRecord(
        id=f"pt_{uuid.uuid4().hex[:8]}",
        source_type=source_type,
        source_id=source_id,
        title=title,
        status=ProposalThreadStatus.ACTIVE,
        created_at=now_iso,
        updated_at=now_iso,
    )

    db["proposal_threads"].insert(record.model_dump(mode="json"))  # type: ignore
    logger.info("Created proposal thread [{}] from {}.", record.id, source_type)
    return record


def get_proposal_thread(thread_id: str) -> Optional[ProposalThreadRecord]:
    """Loads one proposal thread by ID."""
    db = get_db()
    try:
        row = db["proposal_threads"].get(thread_id)  # type: ignore
        return ProposalThreadRecord(**row) if row else None
    except Exception as error:
        logger.error(f"Failed to retrieve proposal thread {thread_id}: {error}")
        capture_sentry_exception(
            error,
            component="confirmation_queue",
            operation="get_proposal_thread",
            tags={"thread_id": thread_id},
        )
        return None


def get_proposal_item(item_id: str) -> Optional[ProposalItemRecord]:
    """Loads one proposal item by ID."""
    db = get_db()
    try:
        row = db["proposal_items"].get(item_id)  # type: ignore
        return ProposalItemRecord(**row) if row else None
    except Exception as error:
        logger.error(f"Failed to retrieve proposal item {item_id}: {error}")
        capture_sentry_exception(
            error,
            component="confirmation_queue",
            operation="get_proposal_item",
            tags={"item_id": item_id},
        )
        return None


def list_proposal_items(thread_id: str) -> list[ProposalItemRecord]:
    """Returns proposal items for a thread in their intended confirmation order."""
    db = get_db()
    rows = list(
        db["proposal_items"].rows_where(  # type: ignore
            "thread_id = ? ORDER BY sequence_index, created_at",
            [thread_id],
        )
    )
    return [ProposalItemRecord(**row) for row in rows]


def get_active_proposal_item(thread_id: str) -> Optional[ProposalItemRecord]:
    """Returns the current active proposal item for a thread, if one exists."""
    thread = get_proposal_thread(thread_id)
    if not thread or not thread.active_item_id:
        return None
    return get_proposal_item(thread.active_item_id)


def _update_proposal_thread(thread: ProposalThreadRecord) -> ProposalThreadRecord:
    """Persists a full proposal thread row after a state transition."""
    thread.updated_at = datetime.now(timezone.utc).isoformat()
    get_db()["proposal_threads"].update(thread.id, thread.model_dump(mode="json"))  # type: ignore
    return thread


def _update_proposal_item(item: ProposalItemRecord) -> ProposalItemRecord:
    """Persists a full proposal item row after a state transition or revision."""
    item.updated_at = datetime.now(timezone.utc).isoformat()
    get_db()["proposal_items"].update(item.id, item.model_dump(mode="json"))  # type: ignore
    return item


def add_proposal_item(
    thread_id: str,
    action: ProposedEvent,
    *,
    sequence_index: int = 0,
    status: ProposalItemStatus = ProposalItemStatus.QUEUED,
) -> ProposalItemRecord:
    """
    Adds one draft calendar action to a proposal thread.

    The proposal item mirrors the executable calendar fields but remains a
    draft until the user accepts it. This is what lets feedback revise the same
    active item instead of rejecting it and appending a new proposal.
    """
    db = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    record = ProposalItemRecord(
        id=f"pi_{uuid.uuid4().hex[:8]}",
        thread_id=thread_id,
        status=status,
        sequence_index=sequence_index,
        action_type=action.action_type,
        summary=action.summary,
        start_time=action.start_time,
        end_time=action.end_time,
        description=action.description,
        calendar_id=action.calendar_id,
        target_event_id=action.target_event_id,
        target_event_calendar_id=action.target_event_calendar_id,
        created_at=now_iso,
        updated_at=now_iso,
    )

    db["proposal_items"].insert(record.model_dump(mode="json"))  # type: ignore
    logger.info("Added proposal item [{}] to thread [{}].", record.id, thread_id)
    return record


def activate_next_proposal_item(thread_id: str) -> Optional[ProposalItemRecord]:
    """
    Activates the next actionable proposal item in a thread.

    Items are handled in their original sequence. A queued item becomes
    confirmable, while an in-revision item blocks later proposals until user
    clarification repairs or rejects it.
    """
    thread = get_proposal_thread(thread_id)
    if not thread or thread.status != ProposalThreadStatus.ACTIVE:
        return None

    for item in list_proposal_items(thread_id):
        if item.status == ProposalItemStatus.QUEUED:
            item.status = ProposalItemStatus.ACTIVE
            _update_proposal_item(item)
            thread.active_item_id = item.id
            _update_proposal_thread(thread)
            return item

        if item.status == ProposalItemStatus.IN_REVISION:
            thread.active_item_id = item.id
            _update_proposal_thread(thread)
            return item

    thread.active_item_id = None
    thread.status = ProposalThreadStatus.COMPLETED
    _update_proposal_thread(thread)
    return None


def revise_proposal_item(
    item_id: str,
    action: ProposedEvent,
    *,
    feedback: str,
) -> Optional[ProposalItemRecord]:
    """
    Replaces the active draft details for a proposal item after user feedback.

    This preserves the proposal item's identity and increments revision_count
    so the UI and later synthesis can distinguish revisions from rejections.
    """
    item = get_proposal_item(item_id)
    if not item or item.status not in {
        ProposalItemStatus.ACTIVE,
        ProposalItemStatus.IN_REVISION,
    }:
        return None

    item.status = ProposalItemStatus.ACTIVE
    item.revision_count += 1
    item.last_feedback = feedback
    item.action_type = action.action_type
    item.summary = action.summary
    item.start_time = action.start_time
    item.end_time = action.end_time
    item.description = action.description
    item.calendar_id = action.calendar_id
    item.target_event_id = action.target_event_id
    item.target_event_calendar_id = action.target_event_calendar_id
    return _update_proposal_item(item)


def mark_proposal_item_in_revision(
    item_id: str,
    *,
    feedback: str,
) -> Optional[ProposalItemRecord]:
    """
    Marks an active proposal item as being revised from user feedback.

    revision_count is intentionally not incremented here. It increments only
    when revise_proposal_item replaces the draft with a concrete revised event.
    """
    item = get_proposal_item(item_id)
    if not item or item.status not in {
        ProposalItemStatus.ACTIVE,
        ProposalItemStatus.IN_REVISION,
    }:
        return None

    item.status = ProposalItemStatus.IN_REVISION
    item.last_feedback = feedback
    return _update_proposal_item(item)


def reject_proposal_item(item_id: str) -> Optional[ProposalItemRecord]:
    """Rejects one proposal item without affecting unrelated items in its thread."""
    item = get_proposal_item(item_id)
    if not item or item.status not in {
        ProposalItemStatus.ACTIVE,
        ProposalItemStatus.IN_REVISION,
        ProposalItemStatus.QUEUED,
    }:
        return None

    item.status = ProposalItemStatus.REJECTED
    updated_item = _update_proposal_item(item)

    thread = get_proposal_thread(item.thread_id)
    if thread and thread.active_item_id == item.id:
        thread.active_item_id = None
        _update_proposal_thread(thread)

    return updated_item


def accept_proposal_item(item_id: str) -> Optional[str]:
    """
    Creates the executable pending calendar write for one accepted proposal item.

    The proposal item intentionally remains ACTIVE until confirm_write succeeds.
    This keeps failed calendar execution recoverable instead of stranding the
    draft in a terminal accepted state before anything was written.
    """
    item = get_proposal_item(item_id)
    if not item or item.status != ProposalItemStatus.ACTIVE:
        return None

    return add_pending_write(
        item.summary,
        parse_iso(item.start_time),
        parse_iso(item.end_time),
        item.description,
        item.calendar_id,
        action_type=item.action_type,
        target_event_id=item.target_event_id,
        target_event_calendar_id=item.target_event_calendar_id,
        proposal_item_id=item.id,
    )


def mark_proposal_item_accepted(item_id: str) -> Optional[ProposalItemRecord]:
    """
    Marks a proposal item accepted after its calendar write succeeds.

    This is deliberately separate from accept_proposal_item so API or database
    failures during calendar execution do not prematurely close the proposal.
    """
    try:
        item = get_proposal_item(item_id)
        if not item or item.status != ProposalItemStatus.ACTIVE:
            return None

        item.status = ProposalItemStatus.ACCEPTED
        updated_item = _update_proposal_item(item)

        thread = get_proposal_thread(item.thread_id)
        if thread and thread.active_item_id == item.id:
            thread.active_item_id = None
            _update_proposal_thread(thread)

        return updated_item
    except Exception as error:
        logger.error(f"Failed to mark proposal item {item_id} accepted: {error}")
        capture_sentry_exception(
            error,
            component="confirmation_queue",
            operation="mark_proposal_item_accepted",
            tags={"item_id": item_id},
        )
        raise

def get_pending_write(write_id: str) -> Optional[CalendarWriteRecord]:
    """Retrieves a pending write from the database by ID as a typed record."""
    db = get_db()
    try:
        row = db["calendar_writes"].get(write_id)  # type: ignore
        return CalendarWriteRecord(**row) if row else None
    except Exception as e:
        logger.error(f"Failed to retrieve pending write {write_id}: {e}")
        capture_sentry_exception(
            e,
            component="confirmation_queue",
            operation="get_pending_write",
            tags={"write_id": write_id},
        )
        return None

def confirm_write(write_id: str) -> Optional[dict]:
    """
    Executes a pending write by pushing it to Google Calendar, then marks it executed.
    Returns the resulting event dictionary on success for schedule/reschedule,
    or a minimal payload for cancellation.
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

    logger.info(f"Confirming write {write_id} ({record.action_type})...")

    # Convert ISO strings back to datetime objects
    start_dt = parse_iso(record.start_time)
    end_dt = parse_iso(record.end_time)

    try:
        if record.action_type == "cancel":
            if not record.target_event_id or not record.target_event_calendar_id:
                logger.error(f"Cancellation write {write_id} missing target event identifiers.")
                return None
            success = delete_event(
                event_id=record.target_event_id,
                calendar_id=record.target_event_calendar_id,
            )
            created_event = {
                "id": record.target_event_id,
                "calendar_id": record.target_event_calendar_id,
                "deleted": success,
            } if success else None
        elif record.action_type == "reschedule":
            if not record.target_event_id or not record.target_event_calendar_id:
                logger.error(f"Reschedule write {write_id} missing target event identifiers.")
                return None
            created_event = update_event(
                event_id=record.target_event_id,
                summary=record.summary,
                start_time=start_dt,
                end_time=end_dt,
                description=record.description,
                calendar_id=record.target_event_calendar_id,
            )
        else:
            created_event = insert_event(
                summary=record.summary,
                start_time=start_dt,
                end_time=end_dt,
                description=record.description,
                calendar_id=record.calendar_id,
            )
    except Exception as error:
        logger.error(f"Failed to execute pending write {write_id}: {error}")
        capture_sentry_exception(
            error,
            component="confirmation_queue",
            operation="confirm_write_execute",
            tags={"write_id": write_id, "action_type": record.action_type},
        )
        raise

    if created_event:
        try:
            db["calendar_writes"].update(
                write_id,
                {
                    "status": CalendarWriteStatus.EXECUTED.value,
                    "created_event_id": created_event.get("id"),
                    "created_event_calendar_id": created_event.get("calendar_id", record.calendar_id),
                },
            )  # type: ignore
            logger.success(f"Successfully executed write {write_id} to Google Calendar.")
            return created_event
        except Exception as error:
            logger.error(f"Failed to persist executed write {write_id}: {error}")
            capture_sentry_exception(
                error,
                component="confirmation_queue",
                operation="confirm_write_persist_execution",
                tags={"write_id": write_id},
            )
            raise
    else:
        logger.error(f"Failed to execute write {write_id} via API.")
        return None

def reject_write(write_id: str) -> bool:
    """Marks a pending write as rejected."""
    db = get_db()
    record = get_pending_write(write_id)

    if not record or record.status != CalendarWriteStatus.PENDING:
        logger.warning(f"Cannot reject write {write_id}: not found or not pending.")
        return False

    try:
        db["calendar_writes"].update(write_id, {"status": CalendarWriteStatus.REJECTED.value})  # type: ignore
        logger.info(f"Rejected pending write {write_id}.")
        return True
    except Exception as error:
        logger.error(f"Failed to reject pending write {write_id}: {error}")
        capture_sentry_exception(
            error,
            component="confirmation_queue",
            operation="reject_write",
            tags={"write_id": write_id},
        )
        raise
