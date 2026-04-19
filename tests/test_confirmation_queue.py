from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from orchestrator.confirmation_queue import add_pending_write
from orchestrator.confirmation_queue import confirm_write
from orchestrator.confirmation_queue import get_pending_write
from orchestrator.confirmation_queue import reject_write
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
    assert inserted_row["action_type"] == "schedule"
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
        "action_type": "schedule",
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


@patch("orchestrator.confirmation_queue.capture_sentry_exception")
@patch("orchestrator.confirmation_queue.get_db")
def test_get_pending_write_reports_lookup_failures(
    mock_get_db,
    mock_capture_exception,
):
    db_error = RuntimeError("db unavailable")
    mock_get_db.return_value["calendar_writes"].get.side_effect = db_error

    record = get_pending_write("cw_lookup")

    assert record is None
    mock_capture_exception.assert_called_once_with(
        db_error,
        component="confirmation_queue",
        operation="get_pending_write",
        tags={"write_id": "cw_lookup"},
    )


@patch("orchestrator.confirmation_queue.capture_sentry_exception")
@patch("orchestrator.confirmation_queue.insert_event", side_effect=RuntimeError("calendar insert failed"))
@patch("orchestrator.confirmation_queue.get_pending_write")
@patch("orchestrator.confirmation_queue.get_db")
def test_confirm_write_reports_insert_event_failures(
    mock_get_db,
    mock_get_pending_write,
    mock_insert_event,
    mock_capture_exception,
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
        "action_type": "schedule",
    })()

    with pytest.raises(RuntimeError, match="calendar insert failed"):
        confirm_write("cw_insert")

    mock_capture_exception.assert_called_once_with(
        mock_insert_event.side_effect,
        component="confirmation_queue",
        operation="confirm_write_execute",
        tags={"write_id": "cw_insert", "action_type": "schedule"},
    )


@patch("orchestrator.confirmation_queue.capture_sentry_exception")
@patch("orchestrator.confirmation_queue.insert_event")
@patch("orchestrator.confirmation_queue.get_pending_write")
@patch("orchestrator.confirmation_queue.get_db")
def test_confirm_write_reports_execution_persist_failures(
    mock_get_db,
    mock_get_pending_write,
    mock_insert_event,
    mock_capture_exception,
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
        "action_type": "schedule",
    })()
    mock_insert_event.return_value = {
        "id": "evt_123",
        "calendar_id": "team_calendar@example.com",
    }
    persist_error = RuntimeError("db update failed")
    mock_get_db.return_value["calendar_writes"].update.side_effect = persist_error

    with pytest.raises(RuntimeError, match="db update failed"):
        confirm_write("cw_persist")

    mock_capture_exception.assert_called_once_with(
        persist_error,
        component="confirmation_queue",
        operation="confirm_write_persist_execution",
        tags={"write_id": "cw_persist"},
    )


@patch("orchestrator.confirmation_queue.capture_sentry_exception")
@patch("orchestrator.confirmation_queue.get_pending_write")
@patch("orchestrator.confirmation_queue.get_db")
def test_reject_write_reports_failures(
    mock_get_db,
    mock_get_pending_write,
    mock_capture_exception,
):
    mock_get_pending_write.return_value = type("Record", (), {
        "status": CalendarWriteStatus.PENDING,
    })()
    reject_error = RuntimeError("db reject failed")
    mock_get_db.return_value["calendar_writes"].update.side_effect = reject_error

    with pytest.raises(RuntimeError, match="db reject failed"):
        reject_write("cw_reject")

    mock_capture_exception.assert_called_once_with(
        reject_error,
        component="confirmation_queue",
        operation="reject_write",
        tags={"write_id": "cw_reject"},
    )
