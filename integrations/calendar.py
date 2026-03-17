from datetime import datetime, timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger

from integrations.auth import get_calendar_credentials

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

def get_upcoming_events(max_results: int = 10) -> list:
    """
    Fetches the upcoming events from the user's primary calendar.
    """
    service = get_calendar_service()
    try:
        # Call the Calendar API
        now = datetime.now(timezone.utc).isoformat()
        logger.info(f"Fetching the next {max_results} upcoming events...")
        
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=max_results, singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        return events
        
    except HttpError as error:
        logger.error(f"An error occurred fetching events: {error}")
        return []

def insert_event(summary: str, start_time: datetime, end_time: datetime, description: str = "") -> dict:
    """
    Inserts a new event into the user's primary calendar.
    """
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
