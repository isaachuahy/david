from datetime import datetime, timezone

from persistence.database import get_db
from persistence.models import (
    ArtifactType,
    ArtifactWriteRecord,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
)


ARTIFACT_WRITES_TABLE = "artifact_writes"
RETRYABLE_ARTIFACT_WRITE_STATUSES = (
    ArtifactWriteStatus.PENDING.value,
    ArtifactWriteStatus.FAILED_RETRYABLE.value,
)


def _utc_now_iso() -> str:
    """Returns a stable UTC timestamp for operational retry bookkeeping."""
    return datetime.now(timezone.utc).isoformat()


def serialize_artifact_write(record: ArtifactWriteRecord) -> dict:
    """
    Flattens an artifact write record for SQLite storage.

    Enums are serialized through Pydantic's JSON mode so persisted rows stay
    plain strings and are easy to inspect during operational recovery.
    """
    return record.model_dump(mode="json")


def deserialize_artifact_write(row: dict) -> ArtifactWriteRecord:
    """Reconstructs a typed artifact write record from a database row."""
    return ArtifactWriteRecord(**row)


def save_artifact_write_sync(record: ArtifactWriteRecord) -> ArtifactWriteRecord:
    """
    Inserts or updates a confirmed artifact write operation.

    The operation itself is durable so failed side effects can be retried
    without rerunning the review stage or asking the model to regenerate output.
    """
    db = get_db()
    if ARTIFACT_WRITES_TABLE not in db.table_names():
        raise RuntimeError(
            "artifact_writes table is missing. Initialize the database schema before saving artifact writes."
        )

    table = db[ARTIFACT_WRITES_TABLE]
    payload = serialize_artifact_write(record)
    existing_rows = list(table.rows_where("id = ?", [record.id]))  # type: ignore

    if existing_rows:
        table.update(record.id, payload)  # type: ignore
    else:
        table.insert(payload)  # type: ignore

    return record


def load_artifact_write_sync(write_id: str) -> ArtifactWriteRecord | None:
    """Loads one artifact write operation by ID."""
    db = get_db()
    if ARTIFACT_WRITES_TABLE not in db.table_names():
        return None

    rows = list(db[ARTIFACT_WRITES_TABLE].rows_where("id = ?", [write_id]))  # type: ignore
    if not rows:
        return None
    return deserialize_artifact_write(rows[0])


def find_artifact_write_by_source_sync(
    *,
    artifact_type: ArtifactType,
    source_type: ArtifactWriteSourceType,
    source_id: str | None,
    source_stage: str | None,
) -> ArtifactWriteRecord | None:
    """
    Finds an artifact write for a source/stage/artifact combination.

    Confirmation handlers use this to make repeated Confirm clicks idempotent:
    a confirmed stage should retry or continue the existing write instead of
    creating duplicate side-effect records.
    """
    db = get_db()
    if ARTIFACT_WRITES_TABLE not in db.table_names():
        return None

    rows = list(
        db[ARTIFACT_WRITES_TABLE].rows_where(
            (
                "artifact_type = ? AND source_type = ? "
                "AND source_id = ? AND source_stage = ? "
                "ORDER BY created_at DESC"
            ),
            [
                artifact_type.value,
                source_type.value,
                source_id or "",
                source_stage or "",
            ],
        )
    )  # type: ignore
    if not rows:
        return None
    return deserialize_artifact_write(rows[0])


def list_retryable_artifact_writes_sync() -> list[ArtifactWriteRecord]:
    """
    Returns artifact writes that can be retried after process failure or I/O errors.

    Startup reconciliation should first call
    `mark_interrupted_artifact_writes_retryable_sync` so interrupted EXECUTING
    rows are made visible here without automatically replaying file writes.
    """
    db = get_db()
    if ARTIFACT_WRITES_TABLE not in db.table_names():
        return []

    rows = list(
        db[ARTIFACT_WRITES_TABLE].rows_where(
            "status IN (?, ?)",
            list(RETRYABLE_ARTIFACT_WRITE_STATUSES),
        )
    )  # type: ignore
    return [deserialize_artifact_write(row) for row in rows]


def mark_interrupted_artifact_writes_retryable_sync(
    *,
    last_error: str = "Artifact write was interrupted by process restart.",
) -> list[ArtifactWriteRecord]:
    """
    Marks in-flight artifact writes as retryable after startup recovery.

    An EXECUTING row means the process may have stopped during a side effect.
    Rather than guessing whether the file write completed, reconciliation makes
    the write visible for explicit retry while preserving the confirmed content.
    """
    db = get_db()
    if ARTIFACT_WRITES_TABLE not in db.table_names():
        return []

    rows = list(
        db[ARTIFACT_WRITES_TABLE].rows_where(
            "status = ?",
            [ArtifactWriteStatus.EXECUTING.value],
        )
    )  # type: ignore
    if not rows:
        return []

    table = db[ARTIFACT_WRITES_TABLE]
    updated_at = _utc_now_iso()
    updated_records: list[ArtifactWriteRecord] = []
    for row in rows:
        # Keep the confirmed artifact payload intact; only the operational
        # lifecycle changes so the retry UI can safely resume the write.
        record = deserialize_artifact_write(row).model_copy(
            update={
                "status": ArtifactWriteStatus.FAILED_RETRYABLE,
                "last_error": last_error,
                "updated_at": updated_at,
            }
        )
        table.update(record.id, serialize_artifact_write(record))  # type: ignore
        updated_records.append(record)

    return updated_records
