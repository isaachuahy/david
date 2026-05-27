from unittest.mock import patch

from orchestrator.review_artifacts import get_effective_artifact_content
from persistence.models import (
    ArtifactType,
    ArtifactWriteRecord,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
    ReviewWorkflowRecord,
    SourceSnapshot,
)


def _review_record() -> ReviewWorkflowRecord:
    """Builds a minimal review workflow with frozen artifact baseline content."""
    return ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals\nFrozen goals",
            weekly_state_markdown="# Weekly State\nFrozen weekly state",
            decision_log_markdown="# Decision Log\nFrozen decision log",
        ),
    )


@patch("orchestrator.review_artifacts.find_artifact_write_by_source_sync")
def test_effective_artifact_content_falls_back_to_source_snapshot(
    mock_find_artifact_write_by_source_sync,
):
    mock_find_artifact_write_by_source_sync.return_value = None

    content = get_effective_artifact_content(
        _review_record(),
        ArtifactType.DECISION_LOG,
    )

    assert content == "# Decision Log\nFrozen decision log"


@patch("orchestrator.review_artifacts.find_artifact_write_by_source_sync")
def test_effective_artifact_content_uses_executed_artifact_write(
    mock_find_artifact_write_by_source_sync,
):
    mock_find_artifact_write_by_source_sync.return_value = ArtifactWriteRecord(
        id="awrite_executed",
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log\nConfirmed decision log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage="memory_audit",
        status=ArtifactWriteStatus.EXECUTED,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        executed_at="2026-04-29T00:01:00+00:00",
    )

    content = get_effective_artifact_content(
        _review_record(),
        ArtifactType.DECISION_LOG,
    )

    assert content == "# Decision Log\nConfirmed decision log"


@patch("orchestrator.review_artifacts.find_artifact_write_by_source_sync")
def test_effective_artifact_content_ignores_retryable_artifact_write(
    mock_find_artifact_write_by_source_sync,
):
    mock_find_artifact_write_by_source_sync.return_value = ArtifactWriteRecord(
        id="awrite_retryable",
        artifact_type=ArtifactType.DECISION_LOG,
        content="# Decision Log\nRetryable decision log",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_test",
        source_stage="memory_audit",
        status=ArtifactWriteStatus.FAILED_RETRYABLE,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
    )

    content = get_effective_artifact_content(
        _review_record(),
        ArtifactType.DECISION_LOG,
    )

    assert content == "# Decision Log\nFrozen decision log"
