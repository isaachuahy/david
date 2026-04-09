from unittest.mock import patch

from orchestrator.review_manager import execute_weekly_state_update


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
