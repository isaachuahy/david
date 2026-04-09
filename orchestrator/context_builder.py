from datetime import datetime

from loguru import logger
from integrations.calendar import get_upcoming_events
from orchestrator.time_utils import calendar_event_sort_key
from telegram.ext import ContextTypes
from runtime_paths import get_context_dir
from orchestrator.time_utils import USER_TIMEZONE

def _read_file_safely(filename: str, fallback: str) -> str:
    """Reads a text file safely, returning a fallback if it fails or is missing."""
    filepath = get_context_dir() / filename
    if not filepath.exists():
        logger.warning(f"Context file missing: {filepath}")
        return fallback
        
    try:
        with filepath.open("r", encoding="utf-8") as f:
            content = f.read().strip()
            return content if content else fallback
    except Exception as e:
        logger.error(f"Error reading {filepath}: {e}")
        return fallback


def _current_datetime_block() -> str:
    """Builds a stable, explicit current datetime block for the LLM context."""
    now = datetime.now(USER_TIMEZONE)
    human_date = f"{now.strftime('%A')}, {now.strftime('%B')} {now.day}, {now.year}"
    return (
        f"Current local datetime: {now.isoformat()}\n"
        f"Today is {human_date}."
    )

def _format_calendar_events(tg_context: ContextTypes.DEFAULT_TYPE = None, days: int = 7) -> str:
    """Fetches and formats upcoming calendar events, utilizing a session cache if available."""
    events = None

    # tg_context is a cache for calendar events to avoid hitting the API on every message. 
    # This is for each user session, and needs to be maintained within the same session.
    if tg_context is not None:
        events = tg_context.user_data.get('cached_events')
        
    if events is None:
        logger.info("Calendar cache miss. Fetching fresh events from API...")
        events = get_upcoming_events(days=days)
        if tg_context is not None:
            tg_context.user_data['cached_events'] = events

    events = sorted(events, key=calendar_event_sort_key)
    if tg_context is not None:
        tg_context.user_data['cached_events'] = events

    if not events:
        return "No upcoming events scheduled."
        
    lines = []
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        summary = event.get('summary', 'Busy / No Title')
        lines.append(f"- [{start}] {summary}")
        
    return "\n".join(lines)

def build_context(tg_context: ContextTypes.DEFAULT_TYPE = None) -> str:
    """
    Assembles the full context block to be injected into LLM calls.
    Includes goals, weekly state, decision log, and live calendar.
    """
    logger.info("Building context block for LLM...")
    
    goals = _read_file_safely("goals.md", "No goals defined.")
    weekly = _read_file_safely("weekly_state.md", "No weekly state defined.")
    decisions = _read_file_safely("decision_log.md", "No recent decisions.")
    calendar = _format_calendar_events(tg_context)
    current_datetime = _current_datetime_block()

    return (f"<CONTEXT>\n<CURRENT_DATETIME>\n{current_datetime}\n</CURRENT_DATETIME>\n\n"
            f"<GOALS>\n{goals}\n</GOALS>\n\n"
            f"<WEEKLY_STATE>\n{weekly}\n</WEEKLY_STATE>\n\n"
            f"<DECISION_LOG>\n{decisions}\n</DECISION_LOG>\n\n"
            f"<UPCOMING_CALENDAR>\n{calendar}\n</UPCOMING_CALENDAR>\n</CONTEXT>")
