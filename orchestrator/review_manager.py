import uuid
from datetime import datetime
from loguru import logger
from telegram.ext import ContextTypes

from observability.sentry import capture_exception as capture_sentry_exception
from orchestrator.context_builder import build_context
from integrations.calendar import get_past_events
from persistence.database import get_db
from reasoning.pro_client import generate_sunday_review, SundayReviewResponse
from runtime_paths import get_context_dir

def run_sunday_review(tg_context: ContextTypes.DEFAULT_TYPE = None) -> SundayReviewResponse:
    """
    Generates the Sunday Review analysis by fetching context and past events,
    then calling the reasoning layer. Returns a structured response object.
    This is a pure business logic function with no side effects.
    """
    try:
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
    except Exception as error:
        logger.error(f"Failed to run Sunday review: {error}")
        capture_sentry_exception(error, component="review_manager", operation="run_sunday_review")
        raise

def execute_weekly_state_update(content: str) -> bool:
    """
    Backs up the current weekly_state.md and overwrites it with new content.
    Returns True on success, False on failure.
    This is a pure file I/O function.
    """
    try:
        context_dir = get_context_dir()
        context_dir.mkdir(parents=True, exist_ok=True)
        weekly_state_path = context_dir / "weekly_state.md"
        
        if weekly_state_path.exists():
            backup_filename = f"weekly_state_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            backup_path = context_dir / backup_filename
            with weekly_state_path.open("r", encoding="utf-8") as src, backup_path.open("w", encoding="utf-8") as dst:
                dst.write(src.read())
            logger.info(f"Backed up weekly state to {backup_filename}")
                
        with weekly_state_path.open("w", encoding="utf-8") as f:
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
        capture_sentry_exception(e, component="review_manager", operation="execute_weekly_state_update")
        return False
