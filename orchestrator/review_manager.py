import os
import uuid
from datetime import datetime
from loguru import logger
from telegram.ext import ContextTypes

from orchestrator.context_builder import build_context
from integrations.calendar import get_past_events
from persistence.database import get_db
from reasoning.pro_client import generate_sunday_review, SundayReviewResponse

def run_sunday_review(tg_context: ContextTypes.DEFAULT_TYPE = None) -> SundayReviewResponse:
    """
    Generates the Sunday Review analysis by fetching context and past events,
    then calling the reasoning layer. Returns a structured response object.
    This is a pure business logic function with no side effects.
    """
    context_block = build_context(tg_context)
    
    # Fetch and format past events
    past_events_raw = get_past_events(days=7)
    if not past_events_raw:
        past_events_block = "No events found in the past week."
    else:
        lines = []
        for event in past_events_raw:
            start_time = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'Busy / No Title')
            lines.append(f"- [{start_time}] {summary}")
        past_events_block = "\n".join(lines)
    
    review = generate_sunday_review(context_block, past_events_block)
    return review

def execute_weekly_state_update(content: str) -> bool:
    """
    Backs up the current weekly_state.md and overwrites it with new content.
    Returns True on success, False on failure.
    This is a pure file I/O function.
    """
    try:
        # Resolve context directory relative to the current file
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        context_dir = os.path.join(base_dir, "context")
        weekly_state_path = os.path.join(context_dir, "weekly_state.md")
        
        if os.path.exists(weekly_state_path):
            backup_filename = f"weekly_state_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            backup_path = os.path.join(context_dir, backup_filename)
            with open(weekly_state_path, "r", encoding="utf-8") as src, open(backup_path, "w", encoding="utf-8") as dst:
                dst.write(src.read())
            logger.info(f"Backed up weekly state to {backup_filename}")
                
        with open(weekly_state_path, "w", encoding="utf-8") as f:
            f.write(content)

        snapshot_id = f"wsnap_{uuid.uuid4().hex[:8]}"
        logger.info(f"Persisting weekly snapshot {snapshot_id}...")
        db = get_db()
        db["weekly_snapshots"].insert({  # type: ignore
            "id": snapshot_id,
            "timestamp": datetime.now().isoformat(),
            "weekly_state_content": content,
        })
        logger.success(f"Persisted weekly snapshot {snapshot_id}.")
        
        logger.success("Successfully updated weekly_state.md")
        return True
    except Exception as e:
        logger.error(f"Failed to execute weekly state update: {e}")
        return False
