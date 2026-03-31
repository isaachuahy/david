from datetime import datetime
from zoneinfo import ZoneInfo

USER_TIMEZONE = ZoneInfo("America/Toronto")

def parse_iso(iso_str: str) -> datetime:
    """
    Parses an ISO 8601 string into a timezone-aware datetime object.
    Safely handles the 'Z' suffix commonly output by LLMs and APIs.
    """
    clean_str = iso_str.replace('Z', '+00:00')
    return datetime.fromisoformat(clean_str)

def parse_user_datetime(iso_str: str) -> datetime:
    """
    Parses an ISO 8601 string for user-facing scheduling.
    If no timezone offset is present, interpret the value in America/Toronto.
    """
    dt = parse_iso(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=USER_TIMEZONE)
    return dt.astimezone(USER_TIMEZONE)

def format_user_datetime(dt: datetime) -> str:
    """Formats a datetime for display in America/Toronto."""
    return dt.astimezone(USER_TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")


def parse_calendar_sortable_datetime(value: str) -> datetime:
    """
    Normalizes Google Calendar date/dateTime values into sortable datetimes.
    All-day dates are interpreted at midnight in the user's local timezone.
    """
    if "T" not in value:
        return datetime.fromisoformat(value).replace(tzinfo=USER_TIMEZONE)
    return parse_user_datetime(value)


def calendar_event_sort_key(event: dict) -> datetime:
    """Builds a stable chronological sort key for a Google Calendar event dict."""
    start = event["start"].get("dateTime", event["start"].get("date"))
    return parse_calendar_sortable_datetime(start)
