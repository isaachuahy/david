from persistence.artifact_writes import find_artifact_write_by_source_sync
from persistence.models import (
    ArtifactType,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
    ReviewStage,
    ReviewWorkflowRecord,
)


def get_effective_artifact_content(
    record: ReviewWorkflowRecord,
    artifact_type: ArtifactType,
) -> str:
    """
    Returns the artifact content downstream Sunday-review stages should read.

    The review's source snapshot stays frozen as the reproducible baseline.
    Once a stage confirms and executes an artifact write, later stages should
    use that confirmed content instead of stale snapshot markdown.
    """
    source_snapshot_by_artifact = {
        ArtifactType.DECISION_LOG: record.source_snapshot.decision_log_markdown,
        ArtifactType.WEEKLY_STATE: record.source_snapshot.weekly_state_markdown,
        ArtifactType.GOALS: record.source_snapshot.goals_markdown,
    }
    source_stage_by_artifact = {
        ArtifactType.DECISION_LOG: ReviewStage.MEMORY_AUDIT,
        ArtifactType.WEEKLY_STATE: ReviewStage.WEEKLY_PLAN,
        ArtifactType.GOALS: ReviewStage.GOALS_AUDIT,
    }

    source_stage = source_stage_by_artifact[artifact_type]
    write = find_artifact_write_by_source_sync(
        artifact_type=artifact_type,
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id=record.id,
        source_stage=source_stage.value,
    )
    if write is not None and write.status == ArtifactWriteStatus.EXECUTED:
        return write.content

    return source_snapshot_by_artifact[artifact_type]
