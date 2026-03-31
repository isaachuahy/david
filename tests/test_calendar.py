from datetime import datetime
from unittest.mock import MagicMock, patch

from integrations.calendar import get_past_events, get_upcoming_events, insert_event


@patch("integrations.calendar.get_calendar_service")
def test_insert_event_sends_toronto_datetime_and_timezone(mock_get_calendar_service):
    mock_service = MagicMock()
    mock_get_calendar_service.return_value = mock_service
    mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt_123"}

    created_event = insert_event(
        summary="Deep Work",
        start_time=datetime.fromisoformat("2026-03-31T13:00:00+00:00"),
        end_time=datetime.fromisoformat("2026-03-31T14:30:00+00:00"),
        description="Focus block."
    )

    assert created_event == {"id": "evt_123"}
    insert_kwargs = mock_service.events.return_value.insert.call_args.kwargs
    assert insert_kwargs["calendarId"] == "primary"
    assert insert_kwargs["body"]["start"] == {
        "dateTime": "2026-03-31T09:00:00-04:00",
        "timeZone": "America/Toronto",
    }
    assert insert_kwargs["body"]["end"] == {
        "dateTime": "2026-03-31T10:30:00-04:00",
        "timeZone": "America/Toronto",
    }


@patch("integrations.calendar.get_calendar_service")
def test_get_upcoming_events_returns_earliest_first_across_calendars(mock_get_calendar_service):
    mock_service = MagicMock()
    mock_get_calendar_service.return_value = mock_service
    mock_service.calendarList.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "cal_1"}, {"id": "cal_2"}]
    }
    mock_service.events.return_value.list.return_value.execute.side_effect = [
        {
            "items": [
                {"summary": "Later Event", "start": {"dateTime": "2026-03-31T15:00:00Z"}},
            ]
        },
        {
            "items": [
                {"summary": "All Day Event", "start": {"date": "2026-03-31"}},
                {"summary": "Earlier Event", "start": {"dateTime": "2026-03-31T13:00:00Z"}},
            ]
        },
    ]

    events = get_upcoming_events(days=7)

    assert [event["summary"] for event in events] == [
        "All Day Event",
        "Earlier Event",
        "Later Event",
    ]


@patch("integrations.calendar.get_calendar_service")
def test_get_past_events_returns_earliest_first_across_calendars(mock_get_calendar_service):
    mock_service = MagicMock()
    mock_get_calendar_service.return_value = mock_service
    mock_service.calendarList.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "cal_1"}, {"id": "cal_2"}]
    }
    mock_service.events.return_value.list.return_value.execute.side_effect = [
        {
            "items": [
                {"summary": "Tuesday Event", "start": {"dateTime": "2026-03-31T15:00:00Z"}},
            ]
        },
        {
            "items": [
                {"summary": "Monday Event", "start": {"dateTime": "2026-03-30T13:00:00Z"}},
            ]
        },
    ]

    events = get_past_events(days=7)

    assert [event["summary"] for event in events] == [
        "Monday Event",
        "Tuesday Event",
    ]
