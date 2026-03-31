from datetime import datetime
from unittest.mock import MagicMock, patch

from integrations.calendar import insert_event


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
