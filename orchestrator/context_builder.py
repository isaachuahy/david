from datetime import datetime
from typing import Iterable, Literal, Optional

from loguru import logger
from integrations.calendar import get_upcoming_events, resolve_calendar_display_name
from orchestrator.time_utils import calendar_event_sort_key
from telegram.ext import ContextTypes
from runtime_paths import get_context_dir
from orchestrator.time_utils import USER_TIMEZONE

# Type constraint for valid context sections to ensure type safety and prevent typos.
ContextSection = Literal[
    "CURRENT_DATETIME",
    "GOALS",
    "WEEKLY_STATE",
    "DECISION_LOG",
    "UPCOMING_CALENDAR",
]

# Define a stable order for context sections to ensure consistent prompt structure.
CONTEXT_SECTION_ORDER: tuple[ContextSection, ...] = (
    "CURRENT_DATETIME",
    "GOALS",
    "WEEKLY_STATE",
    "DECISION_LOG",
    "UPCOMING_CALENDAR",
)

# These profiles define the minimum context bundle we want to expose for
# common routing cases. The names should describe why the extra sections are
# present, not just how many sections are included.
CONTEXT_PROFILES: dict[str, tuple[ContextSection, ...]] = {
    "lean": ("CURRENT_DATETIME", "WEEKLY_STATE"),
    "calendar_context": ("CURRENT_DATETIME", "WEEKLY_STATE", "UPCOMING_CALENDAR"),
    "priority_strategy": ("CURRENT_DATETIME", "GOALS", "WEEKLY_STATE", "DECISION_LOG"),
    "full": CONTEXT_SECTION_ORDER,
}

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
    calendar_name_cache: dict[str, str] = {}

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
        calendar_id = event.get("calendar_id", "primary")

        # Keep the canonical calendar ID visible in model context so later
        # scheduling proposals can anchor to a real Google Calendar target.
        if calendar_id not in calendar_name_cache:
            calendar_name_cache[calendar_id] = resolve_calendar_display_name(calendar_id)

        calendar_display_name = calendar_name_cache[calendar_id]
        lines.append(
            f"- [{start}] {summary} "
            f"(Calendar: {calendar_display_name}; calendar_id: {calendar_id})"
        )
        
    return "\n".join(lines)

def _resolve_requested_sections(
    sections: Optional[Iterable[ContextSection]] = None,
    profile: Optional[str] = None, # we only usually use profile, but allow sections for flexibility in testing and future use cases
) -> tuple[ContextSection, ...]:
    """
    Resolves either an explicit section list or a named profile into a stable,
    deduplicated section tuple.
    """
    if sections is not None and profile is not None:
        raise ValueError("Pass either sections or profile, not both.")

    if profile is not None:
        if profile not in CONTEXT_PROFILES:
            raise ValueError(f"Unknown context profile: {profile}")
        requested_sections = CONTEXT_PROFILES[profile]
    elif sections is None:
        requested_sections = CONTEXT_SECTION_ORDER
    else:
        requested_sections = tuple(sections)

    requested_set = set(requested_sections)
    unknown_sections = requested_set.difference(CONTEXT_SECTION_ORDER)
    if unknown_sections:
        unknown_list = ", ".join(sorted(unknown_sections))
        raise ValueError(f"Unknown context sections: {unknown_list}")

    # Preserve canonical ordering so prompt structure stays predictable even
    # when callers pass sections in an arbitrary order.
    return tuple(
        section for section in CONTEXT_SECTION_ORDER if section in requested_set
    )


def build_context(
    tg_context: ContextTypes.DEFAULT_TYPE = None,
    sections: Optional[Iterable[ContextSection]] = None,
    profile: Optional[str] = None,
) -> str:
    """
    Assembles the full context block to be injected into LLM calls.

    Callers can request either explicit sections or a named profile to keep
    the prompt aligned with the task at hand. When neither is provided, the
    builder returns the full context for backward compatibility.
    """
    logger.info("Building context block for LLM...")
    requested_sections = _resolve_requested_sections(sections=sections, profile=profile)
    section_content: dict[ContextSection, str] = {}

    # Load only the requested sections so lean profiles reduce both token
    # footprint and unnecessary work, especially calendar fetches.
    if "CURRENT_DATETIME" in requested_sections:
        section_content["CURRENT_DATETIME"] = _current_datetime_block()
    if "GOALS" in requested_sections:
        section_content["GOALS"] = _read_file_safely("goals.md", "No goals defined.")
    if "WEEKLY_STATE" in requested_sections:
        section_content["WEEKLY_STATE"] = _read_file_safely(
            "weekly_state.md",
            "No weekly state defined.",
        )
    if "DECISION_LOG" in requested_sections:
        section_content["DECISION_LOG"] = _read_file_safely(
            "decision_log.md",
            "No recent decisions.",
        )
    if "UPCOMING_CALENDAR" in requested_sections:
        section_content["UPCOMING_CALENDAR"] = _format_calendar_events(tg_context)

    context_parts = ["<CONTEXT>"]
    for section in requested_sections:
        context_parts.append(
            f"<{section}>\n{section_content[section]}\n</{section}>"
        )

    context_parts.append("</CONTEXT>")
    return "\n\n".join(context_parts)
