import uuid
from datetime import datetime, timezone
from loguru import logger
from telegram.ext import ContextTypes

from persistence.database import get_db
from orchestrator.trigger_scheduler import prompt_next_trigger

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
