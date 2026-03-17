from datetime import datetime, timezone
from functools import lru_cache
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from integrations.auth import get_calendar_credentials

# Note that datetimes that get passed to the API must be in UTC ISO format, not naive datetime objects.

@lru_cache(maxsize=1)
def get_calendar_service():
    """
    Builds and returns the Google Calendar API service instance.
    Cached to avoid re-authenticating and rebuilding on every call.
    """
    creds = get_calendar_credentials()
    try:
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Failed to build Calendar service: {e}")
        raise

def get_upcoming_events(max_results: int = 10) -> list:
    """
    Fetches the upcoming events across all of the user's calendars.
    """
    service = get_calendar_service()
    try:
        now = datetime.now(timezone.utc).isoformat()
        logger.info(f"Fetching upcoming {max_results} events across all calendars...")
        
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get('items', [])
        
        all_events = []
        
        for cal in calendars:
            cal_id = cal['id']
            try:
                events_result = service.events().list(
                    calendarId=cal_id, timeMin=now,
                    maxResults=max_results, singleEvents=True,
                    orderBy='startTime'
                ).execute()
                all_events.extend(events_result.get('items', []))
            except HttpError as e:
                logger.warning(f"Could not fetch events for calendar {cal.get('summary', cal_id)}: {e}")
                
        # Sort combined events by start time (handles both date and dateTime strings)
        all_events.sort(key=lambda x: x['start'].get('dateTime', x['start'].get('date')))
        
        return all_events[:max_results]
        
    except HttpError as error:
        logger.error(f"An error occurred fetching events: {error}")
        return []

def insert_event(summary: str, start_time: datetime, end_time: datetime, description: str = "") -> dict:
    """
    Inserts a new event into the user's primary calendar.
    """
    if start_time.tzinfo is None or end_time.tzinfo is None:
        raise ValueError("start_time and end_time must be timezone-aware datetime objects.")

    service = get_calendar_service()
    try:
        event_body = {
            'summary': summary,
            'description': description,
            'start': {
                'dateTime': start_time.isoformat(),
            },
            'end': {
                'dateTime': end_time.isoformat(),
            },
        }
        logger.info(f"Inserting event: '{summary}'...")
        created_event = service.events().insert(calendarId='primary', body=event_body).execute()
        return created_event
    except HttpError as error:
        logger.error(f"An error occurred inserting the event: {error}")
        return {}
