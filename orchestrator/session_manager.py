import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple
from loguru import logger
from telegram.ext import ContextTypes

from persistence.database import get_db
from orchestrator.confirmation_queue import get_pending_write, reject_write
from orchestrator.trigger_scheduler import prompt_next_trigger
from persistence.models import CalendarWriteStatus, SessionRecord, SessionStatus
from reasoning.flash_client import generate_session_synthesis

SESSION_INACTIVITY_TIMEOUT = timedelta(minutes=30)
SESSION_TIMEOUT_JOB_PREFIX = "session_inactivity_timeout"
SESSION_READY_MESSAGE = "Session synthesis is done. I'm ready for your next message."

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT_DIR = os.path.join(BASE_DIR, "context")
DECISION_LOG_PATH = os.path.join(CONTEXT_DIR, "decision_log.md")

def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Returns user_data when available, otherwise a safe empty dict."""
    data = getattr(context, "user_data", None)
    return data if isinstance(data, dict) else {}

def is_session_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user currently has an active session."""
    return _user_data(context).get('session_state') == SessionStatus.ACTIVE

def get_session_state(context: ContextTypes.DEFAULT_TYPE) -> SessionStatus:
    """Retrieves the current session state."""
    return _user_data(context).get('session_state', SessionStatus.IDLE)

def get_chat_history(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Retrieves the current session's chat history."""
    return _user_data(context).get('chat_history', [])

def append_chat_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str):
    """Appends a message to the current session's chat history."""
    user_data = _user_data(context)
    if 'chat_history' not in user_data:
        user_data['chat_history'] = []
    user_data['chat_history'].append({"role": role, "content": content})

def track_confirmation_message(context: ContextTypes.DEFAULT_TYPE, write_id: str, message_id: int):
    """Appends the UI state of a pending calendar write to the list."""
    user_data = _user_data(context)
    if 'pending_writes' not in user_data:
        user_data['pending_writes'] = []
    user_data['pending_writes'].append((write_id, message_id))

def get_tracked_confirmation_messages(context: ContextTypes.DEFAULT_TYPE) -> List[Tuple[str, int]]:
    """Retrieves the list of pending calendar write UI states."""
    return _user_data(context).get('pending_writes', [])

def untrack_confirmation_message(context: ContextTypes.DEFAULT_TYPE, write_id: str):
    """Removes a specific pending write from the UI state tracking."""
    user_data = _user_data(context)
    writes = user_data.get('pending_writes', [])
    user_data['pending_writes'] = [w for w in writes if w[0] != write_id]

def clear_tracked_confirmation_messages(context: ContextTypes.DEFAULT_TYPE):
    """Clears all pending calendar write UI states."""
    _user_data(context)['pending_writes'] = []

def get_session_timeout_job_name(user_id: int) -> str:
    """Builds a stable job name for a user's inactivity timeout."""
    return f"{SESSION_TIMEOUT_JOB_PREFIX}_{user_id}"

def cancel_session_timeout(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Cancels any scheduled inactivity timeout for the user."""
    for job in context.job_queue.get_jobs_by_name(get_session_timeout_job_name(user_id)):
        job.schedule_removal()

async def timeout_inactive_session(context: ContextTypes.DEFAULT_TYPE):
    """Closes the active session after 30 minutes without a new message."""
    chat_id = context.job.data["chat_id"]
    user_id = context.job.user_id

    if not is_session_active(context):
        logger.info(f"Inactivity timeout fired for chat {chat_id}, but no active session remained.")
        return

    logger.info(f"Session timed out after 30 minutes of inactivity for chat {chat_id}.")
    await end_session(context, chat_id, reason="timeout", user_id=user_id)

def reset_session_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """Restarts the inactivity timeout countdown for the active session."""
    cancel_session_timeout(context, user_id)
    context.job_queue.run_once(
        timeout_inactive_session,
        SESSION_INACTIVITY_TIMEOUT,
        data={"chat_id": chat_id},
        name=get_session_timeout_job_name(user_id),
        chat_id=chat_id,
        user_id=user_id,
    )

def start_session(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Starts a new conversational session and logs it to the database."""
    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    user_data = _user_data(context)
    user_data['current_session_id'] = session_id
    user_data['session_state'] = SessionStatus.ACTIVE
    user_data['chat_history'] = []
    
    record = SessionRecord(
        id=session_id,
        status=SessionStatus.ACTIVE,
        start_time=datetime.now(timezone.utc).isoformat()
    )
    
    db = get_db()
    db["sessions"].insert(record.model_dump())  # type: ignore
    logger.info(f"Started new session: {session_id}")
    return session_id

def reconcile_orphaned_sessions() -> int:
    """
    Marks any sessions left in ACTIVE or CLOSING state as INTERRUPTED.
    This is used at startup to recover from VPS or process restarts.
    """
    db = get_db()
    orphaned_sessions = list(  # type: ignore
        db["sessions"].rows_where(
            "status IN (?, ?)",
            [SessionStatus.ACTIVE.value, SessionStatus.CLOSING.value],
        )
    )
    if not orphaned_sessions:
        logger.info("No orphaned ACTIVE/CLOSING sessions found during startup reconciliation.")
        return 0

    interrupted_at = datetime.now(timezone.utc).isoformat()
    for session in orphaned_sessions:
        session_id = session["id"]
        db["sessions"].update(session_id, {  # type: ignore
            "status": SessionStatus.INTERRUPTED.value,
            "end_time": interrupted_at,
        })
        logger.warning(
            f"Marked orphaned session {session_id} ({session['status']}) as INTERRUPTED during startup reconciliation."
        )

    logger.info(f"Reconciled {len(orphaned_sessions)} orphaned ACTIVE/CLOSING session(s).")
    return len(orphaned_sessions)

def append_to_decision_log(content: str):
    """Appends synthesized session notes to the decision log."""
    with open(DECISION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n\n{content.strip()}\n")

def persist_decision(session_id: str, content: str):
    """Persists a synthesized session decision block to SQLite."""
    decision_id = f"dec_{uuid.uuid4().hex[:8]}"
    logger.info(f"Persisting decision {decision_id} for session {session_id}...")
    try:
        db = get_db()
        db["decisions"].insert({  # type: ignore
            "id": decision_id,
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "content": content,
        })
        logger.success(f"Persisted decision {decision_id} for session {session_id}.")
    except Exception as e:
        logger.error(f"Failed to persist decision for session {session_id}: {e}")
        raise

async def cancel_pending_writes(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Rejects any pending calendar confirmations when a session closes."""
    for write_id, message_id in get_tracked_confirmation_messages(context):
        record = get_pending_write(write_id)
        if record and record.status == CalendarWriteStatus.PENDING:
            logger.info(f"Session closing. Auto-rejecting pending write {write_id}.")
            reject_write(write_id)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="🚫 *Event cancelled because the session closed before confirmation.*",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to update cancelled proposal UI for {write_id}: {e}")
    clear_tracked_confirmation_messages(context)

async def execute_synthesis_task(context: ContextTypes.DEFAULT_TYPE):
    """Background job to synthesize the session transcript and finalize closing."""
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    session_id = job_data.get("session_id")
    chat_history = list(job_data.get("chat_history", []))
    session_date = datetime.now(timezone.utc).date().isoformat()
    
    logger.info(f"Running background synthesis for session {session_id}...")
    try:
        if chat_history:
            synthesis = await asyncio.to_thread(
                generate_session_synthesis,
                chat_history,
                session_date=session_date,
            )
            if session_id:
                persist_decision(session_id, synthesis.content)
            append_to_decision_log(synthesis.content)
            logger.success(f"Appended session synthesis to decision log for session {session_id}.")
        else:
            logger.info(f"Skipping synthesis for session {session_id}: no chat history found.")
    except Exception as e:
        logger.error(f"Failed to synthesize session {session_id}: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ I closed the session, but failed to update the decision log. Please check the logs."
            )
        except Exception as notify_error:
            logger.error(f"Failed to send synthesis failure message: {notify_error}")
    finally:
        # Finalise transition to IDLE even if synthesis fails.
        # Clear both short-term chat state and the per-session calendar cache
        # so the next session always starts from a fresh local view.
        if session_id:
            try:
                get_db()["sessions"].update(session_id, {  # type: ignore
                    "status": SessionStatus.COMPLETED.value,
                })
                logger.info(f"Marked session {session_id} as COMPLETED after synthesis finalization.")
            except Exception as e:
                logger.error(f"Failed to mark session {session_id} as COMPLETED: {e}")

        user_data = _user_data(context)
        user_data['chat_history'] = []
        user_data.pop('cached_events', None)
        user_data['session_state'] = SessionStatus.IDLE
        user_data['current_session_id'] = None

        try:
            await prompt_next_trigger(context, chat_id)
        except Exception as e:
            logger.error(f"Failed to evaluate the trigger queue after session {session_id}: {e}")

        logger.info(f"Session {session_id} synthesis finalization complete. Ready for new messages.")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=SESSION_READY_MESSAGE,
            )
        except Exception as e:
            logger.error(f"Failed to send ready-for-next-message prompt for session {session_id}: {e}")

async def end_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reason: str = "done", user_id: Optional[int] = None):
    """Ends the active session, clears short-term memory, and checks for pending triggers."""
    user_data = _user_data(context)
    session_id = user_data.get('current_session_id')
    chat_history_snapshot = list(user_data.get('chat_history', []))
    if session_id:
        db = get_db()
        db["sessions"].update(session_id, {
            "status": SessionStatus.CLOSING.value,
            "end_time": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Transitioning session {session_id} to CLOSING.")
        
    user_data['session_state'] = SessionStatus.CLOSING
        
    if reason == "timeout":
        text = "Session timed out. Synthesizing decisions in the background..."
    else:
        text = "Session closed. Synthesizing decisions in the background..."
        
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send session close message: {e}")

    await cancel_pending_writes(context, chat_id)

    # Schedule the synthesis task to run immediately without blocking the UI.
    # Passing user_id ensures PTB binds the same user_data into the job callback
    # so the finalizer can clear the real session state instead of a detached context.
    job_kwargs = {
        "data": {
            "chat_id": chat_id,
            "session_id": session_id,
            "chat_history": chat_history_snapshot,
        },
        "chat_id": chat_id,
    }
    if user_id is not None:
        job_kwargs["user_id"] = user_id

    context.job_queue.run_once(execute_synthesis_task, 0, **job_kwargs)
