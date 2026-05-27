from unittest.mock import patch

from orchestrator.artifact_writes import (
    create_artifact_write,
    execute_artifact_write,
    reconcile_artifact_writes,
    retry_artifact_write,
)
from persistence.models import (
    ArtifactType,
    ArtifactWriteRecord,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
)


@patch("orchestrator.artifact_writes.find_artifact_write_by_source_sync")
@patch("orchestrator.artifact_writes.save_artifact_write_sync")
def test_create_artifact_write_persists_retryable_record(
    mock_save_artifact_write_sync,
    mock_find_artifact_write_by_source_sync,
):
    mock_find_artifact_write_by_source_sync.return_value = None
    mock_save_artifact_write_sync.side_effect = lambda record: record

    record = create_artifact_write(
        artifact_type=ArtifactType.GOALS,
        content="# Goals",
        source_type=ArtifactWriteSourceType.MANUAL_EDIT,
        source_id="manual_123",
    )

    assert record.id.startswith("awrite_")
    assert record.artifact_type == ArtifactType.GOALS
    assert record.source_type == ArtifactWriteSourceType.MANUAL_EDIT
    assert record.status == ArtifactWriteStatus.PENDING
    mock_save_artifact_write_sync.assert_called_once_with(record)


@patch("orchestrator.artifact_writes.find_artifact_write_by_source_sync")
@patch("orchestrator.artifact_writes.save_artifact_write_sync")
def test_create_artifact_write_reuses_existing_source_record(
    mock_save_artifact_write_sync,
    mock_find_artifact_write_by_source_sync,
):
    existing_record = ArtifactWriteRecord(
        id="awrite_existing",
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_123",
        source_stage="memory_audit",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    mock_find_artifact_write_by_source_sync.return_value = existing_record

    record = create_artifact_write(
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_123",
        source_stage="memory_audit",
    )

    assert record == existing_record
    mock_save_artifact_write_sync.assert_not_called()


@patch("orchestrator.artifact_writes.find_artifact_write_by_source_sync")
@patch("orchestrator.artifact_writes.save_artifact_write_sync")
@patch("orchestrator.artifact_writes.execute_artifact_replacement")
def test_execute_artifact_write_marks_success(
    mock_execute_artifact_replacement,
    mock_save_artifact_write_sync,
    mock_find_artifact_write_by_source_sync,
):
    mock_find_artifact_write_by_source_sync.return_value = None
    mock_save_artifact_write_sync.side_effect = lambda record: record
    mock_execute_artifact_replacement.return_value = True
    record = create_artifact_write(
        artifact_type=ArtifactType.WEEKLY_STATE,
        content="# Weekly State",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_123",
        source_stage="weekly_plan",
    )

    executed = execute_artifact_write(record)

    assert executed.status == ArtifactWriteStatus.EXECUTED
    assert executed.attempts == 1
    assert executed.last_error is None
    assert executed.executed_at is not None
    mock_execute_artifact_replacement.assert_called_once_with(
        ArtifactType.WEEKLY_STATE,
        "# Weekly State",
    )


@patch("orchestrator.artifact_writes.find_artifact_write_by_source_sync")
@patch("orchestrator.artifact_writes.save_artifact_write_sync")
@patch("orchestrator.artifact_writes.execute_artifact_replacement")
def test_execute_artifact_write_marks_retryable_failure(
    mock_execute_artifact_replacement,
    mock_save_artifact_write_sync,
    mock_find_artifact_write_by_source_sync,
):
    mock_find_artifact_write_by_source_sync.return_value = None
    mock_save_artifact_write_sync.side_effect = lambda record: record
    mock_execute_artifact_replacement.return_value = False
    record = create_artifact_write(
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_123",
        source_stage="memory_audit",
    )

    executed = execute_artifact_write(record)

    assert executed.status == ArtifactWriteStatus.FAILED_RETRYABLE
    assert executed.attempts == 1
    assert "Artifact replacement returned false" in executed.last_error
    assert executed.executed_at is None


@patch("orchestrator.artifact_writes.list_retryable_artifact_writes_sync")
@patch("orchestrator.artifact_writes.mark_interrupted_artifact_writes_retryable_sync")
def test_reconcile_artifact_writes_marks_interrupted_writes_visible(
    mock_mark_interrupted_artifact_writes_retryable_sync,
    mock_list_retryable_artifact_writes_sync,
):
    interrupted_record = ArtifactWriteRecord(
        id="awrite_interrupted",
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_123",
        source_stage="memory_audit",
        status=ArtifactWriteStatus.FAILED_RETRYABLE,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    mock_mark_interrupted_artifact_writes_retryable_sync.return_value = [interrupted_record]
    mock_list_retryable_artifact_writes_sync.return_value = [interrupted_record]

    retryable_records = reconcile_artifact_writes()

    assert retryable_records == [interrupted_record]
    mock_mark_interrupted_artifact_writes_retryable_sync.assert_called_once()
    mock_list_retryable_artifact_writes_sync.assert_called_once()


@patch("orchestrator.artifact_writes.save_artifact_write_sync")
@patch("orchestrator.artifact_writes.execute_artifact_replacement")
@patch("orchestrator.artifact_writes._artifact_content_matches")
@patch("orchestrator.artifact_writes.load_artifact_write_sync")
def test_retry_artifact_write_marks_already_applied_content_executed(
    mock_load_artifact_write_sync,
    mock_artifact_content_matches,
    mock_execute_artifact_replacement,
    mock_save_artifact_write_sync,
):
    retryable_record = ArtifactWriteRecord(
        id="awrite_retry",
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_123",
        source_stage="memory_audit",
        status=ArtifactWriteStatus.FAILED_RETRYABLE,
        attempts=1,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )
    mock_load_artifact_write_sync.return_value = retryable_record
    mock_artifact_content_matches.return_value = True
    mock_save_artifact_write_sync.side_effect = lambda record: record

    executed = retry_artifact_write("awrite_retry")

    assert executed is not None
    assert executed.status == ArtifactWriteStatus.EXECUTED
    assert executed.attempts == 2
    assert executed.last_error is None
    assert executed.executed_at is not None
    mock_execute_artifact_replacement.assert_not_called()
