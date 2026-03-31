import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Tuple
from loguru import logger
from telegram.ext import ContextTypes

from persistence.database import get_db
from orchestrator.trigger_scheduler import prompt_next_trigger
from persistence.models import SessionRecord, SessionStatus
from reasoning.flash_client import generate_session_synthesis

SESSION_INACTIVITY_TIMEOUT = timedelta(minutes=30)
SESSION_TIMEOUT_JOB_PREFIX = "session_inactivity_timeout"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT_DIR = os.path.join(BASE_DIR, "context")
DECISION_LOG_PATH = os.path.join(CONTEXT_DIR, "decision_log.md")

def is_session_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if the user currently has an active session."""
    return context.user_data.get('session_state') == SessionStatus.ACTIVE

def get_session_state(context: ContextTypes.DEFAULT_TYPE) -> SessionStatus:
    """Retrieves the current session state."""
    return context.user_data.get('session_state', SessionStatus.IDLE)

def get_chat_history(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Retrieves the current session's chat history."""
    return context.user_data.get('chat_history', [])

def append_chat_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str):
    """Appends a message to the current session's chat history."""
    if 'chat_history' not in context.user_data:
        context.user_data['chat_history'] = []
    context.user_data['chat_history'].append({"role": role, "content": content})

def track_confirmation_message(context: ContextTypes.DEFAULT_TYPE, write_id: str, message_id: int):
    """Appends the UI state of a pending calendar write to the list."""
    if 'pending_writes' not in context.user_data:
        context.user_data['pending_writes'] = []
    context.user_data['pending_writes'].append((write_id, message_id))

def get_tracked_confirmation_messages(context: ContextTypes.DEFAULT_TYPE) -> List[Tuple[str, int]]:
    """Retrieves the list of pending calendar write UI states."""
    return context.user_data.get('pending_writes', [])

def untrack_confirmation_message(context: ContextTypes.DEFAULT_TYPE, write_id: str):
    """Removes a specific pending write from the UI state tracking."""
    writes = context.user_data.get('pending_writes', [])
    context.user_data['pending_writes'] = [w for w in writes if w[0] != write_id]

def clear_tracked_confirmation_messages(context: ContextTypes.DEFAULT_TYPE):
    """Clears all pending calendar write UI states."""
    context.user_data['pending_writes'] = []

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

    if not is_session_active(context):
        logger.info(f"Inactivity timeout fired for chat {chat_id}, but no active session remained.")
        return

    logger.info(f"Session timed out after 30 minutes of inactivity for chat {chat_id}.")
    await end_session(context, chat_id, reason="timeout")

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
    context.user_data['current_session_id'] = session_id
    context.user_data['session_state'] = SessionStatus.ACTIVE
    context.user_data['chat_history'] = []
    
    record = SessionRecord(
        id=session_id,
        status=SessionStatus.ACTIVE,
        start_time=datetime.now(timezone.utc).isoformat()
    )
    
    db = get_db()
    db["sessions"].insert(record.model_dump())  # type: ignore
    logger.info(f"Started new session: {session_id}")
    return session_id

def append_to_decision_log(content: str):
    """Appends synthesized session notes to the decision log."""
    with open(DECISION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n\n{content.strip()}\n")

async def execute_synthesis_task(context: ContextTypes.DEFAULT_TYPE):
    """Background job to synthesize the session transcript and finalize closing."""
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    session_id = job_data.get("session_id")
    chat_history = list(get_chat_history(context))
    session_date = datetime.now(timezone.utc).date().isoformat()
    
    logger.info(f"Running background synthesis for session {session_id}...")
    try:
        if chat_history:
            synthesis = generate_session_synthesis(chat_history, session_date=session_date)
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
        context.user_data['chat_history'] = []
        context.user_data.pop('cached_events', None)
        context.user_data['session_state'] = SessionStatus.IDLE
        context.user_data['current_session_id'] = None
        
        # Evaluate the trigger queue
        await prompt_next_trigger(context, chat_id)

async def end_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reason: str = "done"):
    """Ends the active session, clears short-term memory, and checks for pending triggers."""
    session_id = context.user_data.get('current_session_id')
    if session_id:
        db = get_db()
        db["sessions"].update(session_id, {
            "status": SessionStatus.CLOSING.value,
            "end_time": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Transitioning session {session_id} to CLOSING.")
        
    context.user_data['session_state'] = SessionStatus.CLOSING
        
    if reason == "timeout":
        text = "Session timed out. Synthesizing decisions in the background..."
    else:
        text = "Session closed. Synthesizing decisions in the background..."
        
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send session close message: {e}")
        
    # Schedule the synthesis task to run immediately without blocking the UI
    context.job_queue.run_once(execute_synthesis_task, 0, data={"chat_id": chat_id, "session_id": session_id})
