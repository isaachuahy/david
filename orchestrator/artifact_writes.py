import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from observability.sentry import capture_exception as capture_sentry_exception
from persistence.artifact_writes import (
    find_artifact_write_by_source_sync,
    list_retryable_artifact_writes_sync,
    load_artifact_write_sync,
    mark_interrupted_artifact_writes_retryable_sync,
    save_artifact_write_sync,
)
from persistence.models import (
    ArtifactType,
    ArtifactWriteRecord,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
)
from persistence.database import get_db
from runtime_paths import get_context_dir


_MANAGED_ARTIFACT_FILENAMES: dict[ArtifactType, str] = {
    ArtifactType.DECISION_LOG: "decision_log.md",
    ArtifactType.WEEKLY_STATE: "weekly_state.md",
    ArtifactType.GOALS: "goals.md",
}


def _utc_now_iso() -> str:
    """Returns a timezone-aware UTC timestamp for artifact write records."""
    return datetime.now(timezone.utc).isoformat()


def _get_artifact_path(artifact_type: ArtifactType) -> Path:
    """Returns the managed markdown path for an artifact type."""
    return get_context_dir() / _MANAGED_ARTIFACT_FILENAMES[artifact_type]


def _artifact_content_matches(artifact_type: ArtifactType, content: str) -> bool:
    """
    Checks whether a confirmed write is already reflected on disk.

    This makes retries idempotent after a crash that happens after os.replace()
    succeeds but before the database row is marked EXECUTED.
    """
    artifact_path = _get_artifact_path(artifact_type)
    if not artifact_path.exists():
        return False
    return artifact_path.read_text(encoding="utf-8") == content


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


def _fsync_directory(path: Path) -> None:
    """
    Flushes directory metadata after an atomic rename when the OS supports it.

    `os.replace` gives atomic visibility, while fsyncing the parent directory
    improves crash durability for the new filename entry.
    """
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        logger.debug("Could not fsync artifact directory {}: {}", path, error)


def create_artifact_write(
    *,
    artifact_type: ArtifactType,
    content: str,
    source_type: ArtifactWriteSourceType,
    source_id: str | None = None,
    source_stage: str | None = None,
) -> ArtifactWriteRecord:
    """
    Creates a durable, retryable artifact write after user confirmation.

    Model-generated proposals should become artifact writes only once the user
    confirms them. This record is the stable executable side effect that can be
    retried without rerunning the LLM or changing the proposed content.
    """
    existing_record = find_artifact_write_by_source_sync(
        artifact_type=artifact_type,
        source_type=source_type,
        source_id=source_id,
        source_stage=source_stage,
    )
    if existing_record is not None:
        if existing_record.content != content:
            logger.warning(
                "Reusing artifact write [{}] for {}/{}/{} despite content mismatch.",
                existing_record.id,
                source_type.value,
                source_id,
                source_stage,
            )
        return existing_record

    timestamp = _utc_now_iso()
    record = ArtifactWriteRecord(
        id=f"awrite_{uuid.uuid4().hex[:8]}",
        artifact_type=artifact_type,
        content=content,
        source_type=source_type,
        source_id=source_id,
        source_stage=source_stage,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return save_artifact_write_sync(record)


def execute_artifact_replacement(artifact_type: ArtifactType, content: str) -> bool:
    """
    Backs up and replaces one managed context artifact with confirmed content.

    This is a deterministic side effect: the content has already been confirmed
    by the user and is written exactly as stored in the artifact write record.
    """
    temp_path: Path | None = None
    try:
        context_dir = get_context_dir()
        context_dir.mkdir(parents=True, exist_ok=True)
        filename = _MANAGED_ARTIFACT_FILENAMES[artifact_type]
        artifact_path = context_dir / filename

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=context_dir,
            prefix=f".{filename}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)

        if artifact_path.exists():
            backup_stem = filename.replace(".md", "")
            backup_filename = f"{backup_stem}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            backup_path = context_dir / backup_filename
            with artifact_path.open("r", encoding="utf-8") as src, backup_path.open("w", encoding="utf-8") as dst:
                dst.write(src.read())
            logger.info("Backed up {} to {}.", filename, backup_filename)

        os.replace(temp_path, artifact_path)
        _fsync_directory(context_dir)
        temp_path = None

        if artifact_type == ArtifactType.WEEKLY_STATE:
            _persist_weekly_snapshot(content)

        logger.success("Successfully updated {}.", filename)
        return True
    except Exception as error:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
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
    Executes one durable artifact write and records success or retryable failure.

    The workflow should advance only after this returns an EXECUTED record.
    Failed writes retain the confirmed content and can be retried by ID.
    """
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
            success = execute_artifact_replacement(
                record.artifact_type,
                record.content,
            )
            if not success:
                raise RuntimeError(f"Artifact replacement returned false for {record.artifact_type.value}.")

        record.status = ArtifactWriteStatus.EXECUTED
        record.last_error = None
        record.executed_at = _utc_now_iso()
        logger.info("Executed artifact write [{}] for {}.", record.id, record.artifact_type.value)
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
