import uuid
from datetime import datetime, timezone

from loguru import logger

from observability.sentry import capture_exception as capture_sentry_exception
from persistence.artifact_writes import (
    find_artifact_write_by_source_sync,
    list_retryable_artifact_writes_sync,
    load_artifact_write_sync,
    mark_interrupted_artifact_writes_retryable_sync,
    save_artifact_write_sync,
)
from persistence.context_files import (
    ContextConflictError,
    read_context_file,
    replace_context_file,
)
from persistence.models import (
    ArtifactType,
    ArtifactWriteRecord,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
)
from persistence.database import get_db


def _utc_now_iso() -> str:
    """Returns a timezone-aware UTC timestamp for artifact write records."""
    return datetime.now(timezone.utc).isoformat()


def _artifact_content_matches(artifact_type: ArtifactType, content: str) -> bool:
    """
    Checks whether a confirmed write is already reflected on disk.

    This makes retries idempotent after a crash that happens after os.replace()
    succeeds but before the database row is marked EXECUTED.
    """
    current = read_context_file(artifact_type)
    return current.exists and current.content == content


def _persist_weekly_snapshot(content: str) -> None:
    """
    Records the confirmed weekly-state artifact in the append-only snapshot log.

    This can run during an idempotent retry if the file was already replaced
    before a crash. A duplicate snapshot is safer than marking the write
    executed while silently missing the downstream recovery record.
    """
    snapshot_id = f"wsnap_{uuid.uuid4().hex[:8]}"
    logger.info("Persisting weekly snapshot {}...", snapshot_id)
    db = get_db()
    db["weekly_snapshots"].insert({  # type: ignore
        "id": snapshot_id,
        "timestamp": datetime.now().isoformat(),
        "weekly_state_content": content,
    })
    logger.success("Persisted weekly snapshot {}.", snapshot_id)


def create_artifact_write(
    *,
    artifact_type: ArtifactType,
    content: str,
    source_type: ArtifactWriteSourceType,
    source_id: str | None = None,
    source_stage: str | None = None,
    expected_revision: str | None = None,
) -> ArtifactWriteRecord:
    """
    Creates a durable, retryable artifact write after user confirmation.

    Model-generated proposals should become artifact writes only once the user
    confirms them. This record is the stable executable side effect that can be
    retried without rerunning the LLM or changing the proposed content.
    Editors and review flows should supply the revision used to build the
    proposal. Existing callers capture the live revision at confirmation.
    """
    existing_record = find_artifact_write_by_source_sync(
        artifact_type=artifact_type,
        source_type=source_type,
        source_id=source_id,
        source_stage=source_stage,
    )
    if existing_record is not None:
        if existing_record.content != content or (
            expected_revision is not None
            and existing_record.expected_revision != expected_revision
        ):
            # An operation ID identifies one confirmed payload and base;
            # repeated requests must not replace either with a different draft.
            raise ValueError("An existing artifact write has different content or revision.")
        return existing_record

    if expected_revision is None:
        expected_revision = read_context_file(artifact_type).revision
    timestamp = _utc_now_iso()
    record = ArtifactWriteRecord(
        id=f"awrite_{uuid.uuid4().hex[:8]}",
        artifact_type=artifact_type,
        content=content,
        expected_revision=expected_revision,
        source_type=source_type,
        # Persistence looks up absent source fields as empty strings. Store
        # the same values so repeated manual-save requests find their record.
        source_id=source_id or "",
        source_stage=source_stage or "",
        created_at=timestamp,
        updated_at=timestamp,
    )
    return save_artifact_write_sync(record)


def execute_artifact_replacement(
    artifact_type: ArtifactType,
    content: str,
    *,
    expected_revision: str | None = None,
) -> bool:
    """
    Backs up and replaces one managed context artifact with confirmed content.

    This is a deterministic side effect: the content has already been confirmed
    by the user and is written exactly as stored in the artifact write record.
    Revision conflicts propagate to the caller; I/O failures return false for
    the existing retry workflow. Direct callers without a proposal revision
    check against the live revision read immediately before replacement.
    """
    try:
        if expected_revision is None:
            expected_revision = read_context_file(artifact_type).revision
        try:
            replace_context_file(artifact_type, content, expected_revision=expected_revision)
        except ContextConflictError as error:
            # Another attempt may have applied this exact payload after the
            # caller's read. It is safe to complete without replacing it again.
            if not error.current.exists or error.current.content != content:
                raise

        if artifact_type == ArtifactType.WEEKLY_STATE:
            _persist_weekly_snapshot(content)

        logger.success("Successfully updated {}.md.", artifact_type.value)
        return True
    except ContextConflictError:
        # A stale proposal requires fresh confirmation, not repeated attempts
        # or exception telemetry intended for unexpected infrastructure errors.
        raise
    except Exception as error:
        logger.error("Failed to execute {} artifact replacement: {}", artifact_type.value, error)
        capture_sentry_exception(
            error,
            component="artifact_writes",
            operation="execute_artifact_replacement",
            tags={"artifact_type": artifact_type.value},
        )
        return False


def execute_artifact_write(record: ArtifactWriteRecord) -> ArtifactWriteRecord:
    """
    Executes a confirmed write, distinguishing stale proposals from I/O failure.

    The workflow should advance only after this returns an EXECUTED record.
    I/O failures retain the original revision for retry. Conflicts and legacy
    writes without a base require a new proposal; completed writes never replay.
    """
    if record.status not in {
        ArtifactWriteStatus.PENDING,
        ArtifactWriteStatus.FAILED_RETRYABLE,
    }:
        return record

    record.status = ArtifactWriteStatus.EXECUTING
    record.attempts += 1
    record.updated_at = _utc_now_iso()
    save_artifact_write_sync(record)

    try:
        if _artifact_content_matches(record.artifact_type, record.content):
            logger.info(
                "Artifact write [{}] for {} was already applied; marking executed.",
                record.id,
                record.artifact_type.value,
            )
            if record.artifact_type == ArtifactType.WEEKLY_STATE:
                _persist_weekly_snapshot(record.content)
        else:
            if record.expected_revision is None:
                # Never infer a base during retry: today's file may include
                # edits made after this older proposal was confirmed.
                record.status = ArtifactWriteStatus.FAILED_TERMINAL
                record.last_error = (
                    "This write has no recorded base revision. "
                    "Create a fresh proposal before saving."
                )
                record.updated_at = _utc_now_iso()
                return save_artifact_write_sync(record)
            success = execute_artifact_replacement(
                record.artifact_type,
                record.content,
                expected_revision=record.expected_revision,
            )
            if not success:
                raise RuntimeError(f"Artifact replacement returned false for {record.artifact_type.value}.")

        record.status = ArtifactWriteStatus.EXECUTED
        record.last_error = None
        record.executed_at = _utc_now_iso()
        logger.info("Executed artifact write [{}] for {}.", record.id, record.artifact_type.value)
    except ContextConflictError as error:
        record.status = ArtifactWriteStatus.FAILED_TERMINAL
        record.last_error = f"{error} Create a fresh proposal before saving."
        logger.warning("Artifact write [{}] has an outdated base revision.", record.id)
    except Exception as error:
        record.status = ArtifactWriteStatus.FAILED_RETRYABLE
        record.last_error = str(error)
        capture_sentry_exception(
            error,
            component="artifact_writes",
            operation="execute_artifact_write",
            message="Failed to execute confirmed artifact write.",
            tags={
                "write_id": record.id,
                "artifact_type": record.artifact_type.value,
                "source_type": record.source_type,
                "source_id": record.source_id or "",
                "source_stage": record.source_stage or "",
            },
        )

    record.updated_at = _utc_now_iso()
    return save_artifact_write_sync(record)


def reconcile_artifact_writes() -> list[ArtifactWriteRecord]:
    """
    Makes confirmed-but-unfinished artifact writes visible after process restart.

    Startup recovery deliberately does not execute file writes. It only converts
    interrupted EXECUTING rows into retryable records so a handler or operator
    can resume the exact confirmed content without rerunning a review stage.
    """
    interrupted_records = mark_interrupted_artifact_writes_retryable_sync()
    if interrupted_records:
        interrupted_ids = [record.id for record in interrupted_records]
        logger.warning(
            "Marked {} interrupted artifact write(s) retryable after startup: {}",
            len(interrupted_records),
            interrupted_ids,
        )
        capture_sentry_exception(
            RuntimeError("Interrupted artifact writes were recovered as retryable."),
            component="artifact_writes",
            operation="reconcile_artifact_writes",
            message="Marked interrupted artifact writes retryable during startup reconciliation.",
            tags={"interrupted_write_count": str(len(interrupted_records))},
        )

    retryable_records = list_retryable_artifact_writes_sync()
    if retryable_records:
        logger.info(
            "{} artifact write(s) are visible for retry after reconciliation.",
            len(retryable_records),
        )
    return retryable_records


def retry_artifact_write(write_id: str) -> ArtifactWriteRecord | None:
    """
    Retries a previously persisted artifact write by ID.

    Retry uses the stored content exactly as confirmed. It does not regenerate
    proposals or mutate the surrounding review workflow.
    """
    record = load_artifact_write_sync(write_id)
    if record is None:
        return None
    if record.status not in {
        ArtifactWriteStatus.PENDING,
        ArtifactWriteStatus.FAILED_RETRYABLE,
    }:
        return record
    return execute_artifact_write(record)
