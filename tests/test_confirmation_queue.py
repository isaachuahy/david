from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from orchestrator.confirmation_queue import activate_next_proposal_item
from orchestrator.confirmation_queue import add_pending_write
from orchestrator.confirmation_queue import confirm_write
from orchestrator.confirmation_queue import get_pending_write
from orchestrator.confirmation_queue import reject_write
from persistence.models import CalendarWriteStatus
from persistence.models import ProposalItemRecord
from persistence.models import ProposalItemStatus
from persistence.models import ProposalThreadRecord
from persistence.models import ProposalThreadStatus


def make_proposal_thread(status=ProposalThreadStatus.ACTIVE):
    return ProposalThreadRecord(
        id="pt_123",
        source_type="conversation",
        title="Calendar changes",
        status=status,
        created_at="2026-03-31T00:00:00+00:00",
        updated_at="2026-03-31T00:00:00+00:00",
    )


def make_proposal_item(status=ProposalItemStatus.QUEUED, sequence_index=0):
    return ProposalItemRecord(
        id=f"pi_{sequence_index}",
        thread_id="pt_123",
        status=status,
        sequence_index=sequence_index,
        action_type="schedule",
        summary=f"Proposal {sequence_index}",
        start_time="2026-03-31T09:00:00-04:00",
        end_time="2026-03-31T10:00:00-04:00",
        description="Draft event.",
        calendar_id="primary",
        created_at="2026-03-31T00:00:00+00:00",
        updated_at="2026-03-31T00:00:00+00:00",
    )


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


@patch("orchestrator.confirmation_queue._update_proposal_item")
@patch("orchestrator.confirmation_queue._update_proposal_thread")
@patch("orchestrator.confirmation_queue.list_proposal_items")
@patch("orchestrator.confirmation_queue.get_proposal_thread")
def test_activate_next_proposal_item_activates_first_queued_item(
    mock_get_proposal_thread,
    mock_list_proposal_items,
    mock_update_proposal_thread,
    mock_update_proposal_item,
):
    thread = make_proposal_thread()
    queued_item = make_proposal_item(ProposalItemStatus.QUEUED, sequence_index=0)
    unresolved_item = make_proposal_item(ProposalItemStatus.IN_REVISION, sequence_index=1)
    mock_get_proposal_thread.return_value = thread
    mock_list_proposal_items.return_value = [queued_item, unresolved_item]

    item = activate_next_proposal_item("pt_123")

    assert item is queued_item
    assert queued_item.status == ProposalItemStatus.ACTIVE
    assert thread.active_item_id == queued_item.id
    mock_update_proposal_item.assert_called_once_with(queued_item)
    mock_update_proposal_thread.assert_called_once_with(thread)


@patch("orchestrator.confirmation_queue._update_proposal_item")
@patch("orchestrator.confirmation_queue._update_proposal_thread")
@patch("orchestrator.confirmation_queue.list_proposal_items")
@patch("orchestrator.confirmation_queue.get_proposal_thread")
def test_activate_next_proposal_item_blocks_on_unresolved_item_before_later_queue(
    mock_get_proposal_thread,
    mock_list_proposal_items,
    mock_update_proposal_thread,
    mock_update_proposal_item,
):
    thread = make_proposal_thread()
    unresolved_item = make_proposal_item(ProposalItemStatus.IN_REVISION, sequence_index=0)
    later_queued_item = make_proposal_item(ProposalItemStatus.QUEUED, sequence_index=1)
    mock_get_proposal_thread.return_value = thread
    mock_list_proposal_items.return_value = [unresolved_item, later_queued_item]

    item = activate_next_proposal_item("pt_123")

    assert item is unresolved_item
    assert unresolved_item.status == ProposalItemStatus.IN_REVISION
    assert thread.active_item_id == unresolved_item.id
    mock_update_proposal_item.assert_not_called()
    mock_update_proposal_thread.assert_called_once_with(thread)


@patch("orchestrator.confirmation_queue._update_proposal_thread")
@patch("orchestrator.confirmation_queue.list_proposal_items")
@patch("orchestrator.confirmation_queue.get_proposal_thread")
def test_activate_next_proposal_item_completes_only_after_actionable_items_are_done(
    mock_get_proposal_thread,
    mock_list_proposal_items,
    mock_update_proposal_thread,
):
    thread = make_proposal_thread()
    mock_get_proposal_thread.return_value = thread
    mock_list_proposal_items.return_value = [
        make_proposal_item(ProposalItemStatus.ACCEPTED, sequence_index=0),
        make_proposal_item(ProposalItemStatus.REJECTED, sequence_index=1),
    ]

    item = activate_next_proposal_item("pt_123")

    assert item is None
    assert thread.active_item_id is None
    assert thread.status == ProposalThreadStatus.COMPLETED
    mock_update_proposal_thread.assert_called_once_with(thread)


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
