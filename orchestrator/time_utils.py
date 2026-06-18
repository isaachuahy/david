from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USER_TIMEZONE = ZoneInfo("America/Toronto")
USER_TIMEZONE_NAME = "America/Toronto"

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


def parse_scheduling_datetime(
    iso_str: str,
    timezone_name: str = USER_TIMEZONE_NAME,
) -> datetime:
    """
    Parses and validates a model-proposed scheduling datetime.

    The ISO value must include an explicit UTC offset that is valid for the
    declared IANA timezone on that date. This prevents a timezone-aware but
    semantically wrong value, such as 09:00Z for an intended 09:00 Toronto
    event, from being silently shifted before confirmation.
    """
    dt = parse_iso(iso_str)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            "Calendar proposal times must include an explicit UTC offset."
        )

    try:
        expected_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            f"Unknown calendar proposal timezone: {timezone_name}."
        ) from error

    normalized_dt = dt.astimezone(expected_timezone)
    if normalized_dt.utcoffset() != dt.utcoffset():
        raise ValueError(
            f"Calendar proposal time {iso_str} has an offset that is invalid "
            f"for {timezone_name} on that date."
        )

    return normalized_dt


def validate_scheduling_window(
    start_time: str,
    end_time: str,
    timezone_name: str = USER_TIMEZONE_NAME,
) -> tuple[datetime, datetime]:
    """
    Validates one proposed event window against its declared timezone.

    Returning normalized datetime objects gives persistence and execution code
    one canonical result after all timezone and ordering checks have passed.
    """
    start_dt = parse_scheduling_datetime(start_time, timezone_name)
    end_dt = parse_scheduling_datetime(end_time, timezone_name)
    if end_dt <= start_dt:
        raise ValueError(
            "Calendar proposal end_time must be later than start_time."
        )
    return start_dt, end_dt


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
