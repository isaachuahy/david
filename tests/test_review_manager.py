from unittest.mock import patch

import pytest

from orchestrator.review_manager import execute_weekly_state_update, run_sunday_review


@patch("orchestrator.review_manager.get_db")
@patch("orchestrator.review_manager.get_context_dir")
def test_execute_weekly_state_update_persists_snapshot_and_writes_file(
    mock_get_context_dir,
    mock_get_db,
    tmp_path,
):
    mock_get_context_dir.return_value = tmp_path
    weekly_state_path = tmp_path / "weekly_state.md"
    weekly_state_path.write_text("# Previous Weekly State", encoding="utf-8")

    success = execute_weekly_state_update("# Updated Weekly State")

    assert success is True
    mock_get_db.return_value["weekly_snapshots"].insert.assert_called_once()
    snapshot_row = mock_get_db.return_value["weekly_snapshots"].insert.call_args.args[0]
    assert snapshot_row["weekly_state_content"] == "# Updated Weekly State"
    assert snapshot_row["id"].startswith("wsnap_")


@patch("orchestrator.review_manager.capture_sentry_exception")
@patch("orchestrator.review_manager.generate_sunday_review", side_effect=RuntimeError("Sunday review failed"))
@patch("orchestrator.review_manager.get_past_events", return_value=[])
@patch("orchestrator.review_manager.build_context", return_value="<CONTEXT>")
def test_run_sunday_review_reports_failures(
    mock_build_context,
    mock_get_past_events,
    mock_generate_sunday_review,
    mock_capture_exception,
):
    with pytest.raises(RuntimeError, match="Sunday review failed"):
        run_sunday_review()

    mock_capture_exception.assert_called_once_with(
        mock_generate_sunday_review.side_effect,
        component="review_manager",
        operation="run_sunday_review",
    )


@patch("orchestrator.review_manager.capture_sentry_exception")
@patch("orchestrator.review_manager.get_context_dir", side_effect=OSError("disk error"))
def test_execute_weekly_state_update_reports_failures(
    mock_get_context_dir,
    mock_capture_exception,
):
    success = execute_weekly_state_update("# Updated Weekly State")

    assert success is False
    error = mock_capture_exception.call_args.args[0]
    assert "disk error" in str(error)
    mock_capture_exception.assert_called_once_with(
        error,
        component="review_manager",
        operation="execute_weekly_state_update",
    )
