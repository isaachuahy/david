from datetime import datetime, timezone, timedelta
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from integrations.auth import get_calendar_credentials
from orchestrator.time_utils import USER_TIMEZONE, calendar_event_sort_key

# Note that datetimes that get passed to the API must be in UTC ISO format, not naive datetime objects.

def get_calendar_service():
    """
    Builds and returns the Google Calendar API service instance.
    """
    creds = get_calendar_credentials()
    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Failed to build Calendar service: {e}")
        raise

def _normalize_calendar_reference(value: str) -> str:
    """
    Normalizes a calendar reference for deterministic matching.

    This keeps matching simple and readable while still handling natural
    phrases like "entertainment calendar" in addition to plain names.
    """
    normalized = " ".join(value.strip().lower().split())
    if normalized.endswith(" calendar"):
        normalized = normalized[: -len(" calendar")].strip()
    return normalized

def _build_calendar_match_keys(calendar: dict) -> set[str]:
    """
    Builds the set of normalized lookup keys for a single calendar entry.

    The ID remains authoritative, but the summary is included so application
    code can deterministically resolve user-facing references into that ID.
    """
    calendar_id = calendar.get("id", "").strip()
    summary = calendar.get("summary", calendar_id).strip()

    raw_keys = {calendar_id, summary}
    if summary:
        raw_keys.add(f"{summary} calendar")

    return {
        _normalize_calendar_reference(key)
        for key in raw_keys
        if key.strip()
    }

def resolve_calendar_reference(requested_calendar_text: str) -> dict:
    """
    Resolves a human-facing calendar reference into canonical calendar metadata.

    This is the single deterministic boundary where natural language such as
    "entertainment calendar" becomes the authoritative `calendar_id` that will
    be carried through storage, session state, and Google Calendar writes.
    """
    raw_reference = requested_calendar_text.strip() or "primary"
    normalized_reference = _normalize_calendar_reference(raw_reference)

    if normalized_reference == "primary":
        return {
            "calendar_id": "primary",
            "calendar_display_name": "Primary",
        }

    service = get_calendar_service()
    try:
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get("items", [])

        # Exact ID match wins immediately because calendar IDs are canonical.
        for calendar in calendars:
            calendar_id = calendar.get("id", "").strip()
            if calendar_id == raw_reference:
                return {
                    "calendar_id": calendar_id,
                    "calendar_display_name": calendar.get("summary", calendar_id),
                }

        matches = [
            calendar
            for calendar in calendars
            if normalized_reference in _build_calendar_match_keys(calendar)
        ]

        if len(matches) == 1:
            match = matches[0]
            calendar_id = match.get("id", "").strip()
            return {
                "calendar_id": calendar_id,
                "calendar_display_name": match.get("summary", calendar_id),
            }

        if len(matches) > 1:
            matching_names = [calendar.get("summary", calendar.get("id", "")) for calendar in matches]
            logger.error(
                f"Ambiguous calendar reference '{requested_calendar_text}'. "
                f"Matching calendars: {matching_names}"
            )
            raise ValueError(
                f"Ambiguous calendar reference '{requested_calendar_text}'. "
                "Please use a more specific calendar name."
            )

        logger.error(
            f"Unknown calendar reference '{requested_calendar_text}'. "
            "No matching Google Calendar ID was found."
        )
        raise ValueError(
            f"Unknown calendar reference '{requested_calendar_text}'. "
            "No matching Google Calendar ID was found."
        )
    except HttpError as error:
        logger.error(f"Could not resolve calendar reference '{requested_calendar_text}': {error}")
        raise

def resolve_calendar_display_name(calendar_id: str) -> str:
    """
    Returns a human-friendly calendar name for a calendar ID.
    Falls back to the raw calendar_id when no summary is available.
    """
    if calendar_id == "primary":
        return "Primary"

    service = get_calendar_service()
    try:
        calendar_list = service.calendarList().list().execute()
        for calendar in calendar_list.get("items", []):
            if calendar.get("id") == calendar_id:
                return calendar.get("summary", calendar_id)
    except HttpError as error:
        logger.warning(f"Could not resolve display name for calendar {calendar_id}: {error}")
    except Exception as error:
        logger.warning(f"Unexpected error resolving calendar display name for {calendar_id}: {error}")
    return calendar_id

def get_upcoming_events(days: int = 7) -> list:
    """
    Fetches the upcoming events across all of the user's calendars.
    """
    service = get_calendar_service()
    try:
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(days=days)
        
        now_iso = now.isoformat()
        time_max_iso = time_max.isoformat()
        logger.info(f"Fetching upcoming events for the next {days} days across all calendars...")
        
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get('items', [])
        
        all_events = []
        
        for cal in calendars:
            cal_id = cal['id']
            try:
                events_result = service.events().list(
                    calendarId=cal_id, timeMin=now_iso, timeMax=time_max_iso,
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                calendar_events = events_result.get('items', [])
                for event in calendar_events:
                    event.setdefault("calendar_id", cal_id)
                all_events.extend(calendar_events)
            except HttpError as e:
                logger.warning(f"Could not fetch events for calendar {cal.get('summary', cal_id)}: {e}")
                
        # Sort combined events by their normalized local start time.
        all_events.sort(key=calendar_event_sort_key)
        
        return all_events
        
    except HttpError as error:
        logger.error(f"An error occurred fetching events: {error}")
        return []

def get_past_events(days: int = 7) -> list:
    """
    Fetches the events from the past specified number of days across all calendars.
    """
    service = get_calendar_service()
    try:
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=days)
        
        now_iso = now.isoformat()
        past_iso = past.isoformat()
        
        logger.info(f"Fetching events from the past {days} days across all calendars...")
        
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get('items', [])
        
        all_events = []
        
        for cal in calendars:
            cal_id = cal['id']
            try:
                events_result = service.events().list(
                    calendarId=cal_id, timeMin=past_iso, timeMax=now_iso,
                    singleEvents=True, orderBy='startTime'
                ).execute()
                calendar_events = events_result.get('items', [])
                for event in calendar_events:
                    event.setdefault("calendar_id", cal_id)
                all_events.extend(calendar_events)
            except HttpError as e:
                logger.warning(f"Could not fetch past events for calendar {cal.get('summary', cal_id)}: {e}")
                
        # Sort combined events by their normalized local start time.
        all_events.sort(key=calendar_event_sort_key)
        
        return all_events
        
    except HttpError as error:
        logger.error(f"An error occurred fetching past events: {error}")
        return []

def insert_event(
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str = "",
    calendar_id: str = "primary",
) -> dict:
    """
    Inserts a new event into the selected calendar.
    """
    if start_time.tzinfo is None or end_time.tzinfo is None:
        raise ValueError("start_time and end_time must be timezone-aware datetime objects.")

    start_local = start_time.astimezone(USER_TIMEZONE)
    end_local = end_time.astimezone(USER_TIMEZONE)

    service = get_calendar_service()
    try:
        event_body = {
            'summary': summary,
            'description': description,
            'start': {
                'dateTime': start_local.isoformat(),
                'timeZone': 'America/Toronto',
            },
            'end': {
                'dateTime': end_local.isoformat(),
                'timeZone': 'America/Toronto',
            },
        }
        logger.info(f"Inserting event: '{summary}' into calendar '{calendar_id}'...")
        created_event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        created_event.setdefault("calendar_id", created_event.get("calendarId", calendar_id))
        return created_event
    except HttpError as error:
        logger.error(f"An error occurred inserting the event: {error}")
        return {}
