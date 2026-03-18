import uuid
from datetime import datetime, timezone, timedelta
from loguru import logger
from telegram.ext import ContextTypes

from persistence.database import get_db
from orchestrator.trigger_scheduler import prompt_next_trigger

SESSION_INACTIVITY_TIMEOUT = timedelta(minutes=30)
SESSION_TIMEOUT_JOB_PREFIX = "session_inactivity_timeout"

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

    if context.user_data.get('session_state') != 'ACTIVE':
        logger.info(f"Inactivity timeout fired for chat {chat_id}, but no active session remained.")
        return

    logger.info(f"Session timed out after 30 minutes of inactivity for chat {chat_id}.")
    await end_session(context, chat_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text="Session closed after 30 minutes of inactivity. Transcript ready for synthesis."
    )

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
    context.user_data['session_state'] = 'ACTIVE'
    context.user_data['chat_history'] = []
    
    db = get_db()
    db["sessions"].insert({
        "id": session_id,
        "status": "ACTIVE",
        "start_time": datetime.now(timezone.utc).isoformat(),
        "end_time": None
    })
    logger.info(f"Started new session: {session_id}")
    return session_id

async def end_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Ends the active session, clears short-term memory, and checks for pending triggers."""
    session_id = context.user_data.get('current_session_id')
    if session_id:
        db = get_db()
        db["sessions"].update(session_id, {
            "status": "CLOSING",
            "end_time": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Closed session: {session_id}")
        
    # Clear short-term memory
    context.user_data['chat_history'] = []
    context.user_data['session_state'] = 'IDLE'
    context.user_data['current_session_id'] = None
    
    # Evaluate the trigger queue to see if anything was delayed
    await prompt_next_trigger(context, chat_id)
