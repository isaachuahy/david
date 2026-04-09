from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from orchestrator.confirmation_queue import add_pending_write
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
    )

    mock_get_db.return_value["calendar_writes"].insert.assert_called_once()
    inserted_row = mock_get_db.return_value["calendar_writes"].insert.call_args.args[0]
    assert inserted_row["status"] == CalendarWriteStatus.PENDING.value
    assert inserted_row["summary"] == "Deep Work"
    assert write_id.startswith("cw_")
