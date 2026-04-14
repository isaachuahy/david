from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from orchestrator.confirmation_queue import add_pending_write
from orchestrator.confirmation_queue import confirm_write
from persistence.models import CalendarWriteStatus


@patch("orchestrator.confirmation_queue.get_db")
def test_add_pending_write_serializes_enum_status_before_insert(mock_get_db):
    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(hours=1)

    write_id = add_pending_write(
        summary="Deep Work",
        start_time=start_time,
        end_time=end_time,
        description="Focus block",
        calendar_id="team_calendar@example.com",
    )

    mock_get_db.return_value["calendar_writes"].insert.assert_called_once()
    inserted_row = mock_get_db.return_value["calendar_writes"].insert.call_args.args[0]
    assert inserted_row["status"] == CalendarWriteStatus.PENDING.value
    assert inserted_row["summary"] == "Deep Work"
    assert inserted_row["calendar_id"] == "team_calendar@example.com"
    assert write_id.startswith("cw_")


@patch("orchestrator.confirmation_queue.insert_event")
@patch("orchestrator.confirmation_queue.get_pending_write")
@patch("orchestrator.confirmation_queue.get_db")
def test_confirm_write_persists_created_event_identifiers(
    mock_get_db,
    mock_get_pending_write,
    mock_insert_event,
):
    now = datetime.now(timezone.utc)
    mock_get_pending_write.return_value = type("Record", (), {
        "status": CalendarWriteStatus.PENDING,
        "created_at": now.isoformat(),
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "summary": "Team Sync",
        "description": "Weekly sync",
        "calendar_id": "team_calendar@example.com",
    })()
    mock_insert_event.return_value = {
        "id": "evt_123",
        "calendar_id": "team_calendar@example.com",
    }

    created = confirm_write("cw_abc123")

    assert created["id"] == "evt_123"
    mock_insert_event.assert_called_once()
    update_payload = mock_get_db.return_value["calendar_writes"].update.call_args.args[1]
    assert update_payload["status"] == CalendarWriteStatus.EXECUTED.value
    assert update_payload["created_event_id"] == "evt_123"
    assert update_payload["created_event_calendar_id"] == "team_calendar@example.com"
