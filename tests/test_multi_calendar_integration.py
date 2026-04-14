from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator.confirmation_queue import add_pending_write, confirm_write, get_pending_write
from persistence.database import init_db
from persistence.models import CalendarWriteStatus


def test_pending_write_round_trip_persists_calendar_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DAVID_DB_PATH", str(tmp_path / "assistant.db"))
    init_db()

    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=30)
    write_id = add_pending_write(
        summary="Integration Test Event",
        start_time=start,
        end_time=end,
        description="validate persistence",
        calendar_id="team_calendar@example.com",
    )

    record = get_pending_write(write_id)
    assert record is not None
    assert record.calendar_id == "team_calendar@example.com"
    assert record.status == CalendarWriteStatus.PENDING


@patch("orchestrator.confirmation_queue.insert_event")
def test_confirm_write_persists_event_and_calendar_mapping(mock_insert_event, tmp_path, monkeypatch):
    monkeypatch.setenv("DAVID_DB_PATH", str(tmp_path / "assistant.db"))
    init_db()

    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=45)
    write_id = add_pending_write(
        summary="Team Sync",
        start_time=start,
        end_time=end,
        description="validate executed mapping",
        calendar_id="team_calendar@example.com",
    )
    mock_insert_event.return_value = {
        "id": "evt_123",
        "calendar_id": "team_calendar@example.com",
    }

    created_event = confirm_write(write_id)
    record = get_pending_write(write_id)

    assert created_event is not None
    assert created_event["id"] == "evt_123"
    assert record is not None
    assert record.status == CalendarWriteStatus.EXECUTED
    assert record.created_event_id == "evt_123"
    assert record.created_event_calendar_id == "team_calendar@example.com"
