from unittest.mock import mock_open, patch

from orchestrator.review_manager import execute_weekly_state_update


@patch("orchestrator.review_manager.get_db")
@patch("orchestrator.review_manager.os.path.exists", return_value=True)
@patch("builtins.open", new_callable=mock_open, read_data="# Previous Weekly State")
def test_execute_weekly_state_update_persists_snapshot_and_writes_file(
    mock_file,
    mock_exists,
    mock_get_db,
):
    success = execute_weekly_state_update("# Updated Weekly State")

    assert success is True
    mock_get_db.return_value["weekly_snapshots"].insert.assert_called_once()
    snapshot_row = mock_get_db.return_value["weekly_snapshots"].insert.call_args.args[0]
    assert snapshot_row["weekly_state_content"] == "# Updated Weekly State"
    assert snapshot_row["id"].startswith("wsnap_")
