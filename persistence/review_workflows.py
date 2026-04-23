from persistence.database import get_db
from persistence.models import ReviewWorkflowRecord, ReviewWorkflowStatus


REVIEW_WORKFLOWS_TABLE = "review_workflows"
RESUMABLE_REVIEW_STATUSES = (
    ReviewWorkflowStatus.ACTIVE.value,
    ReviewWorkflowStatus.AWAITING_FEEDBACK.value,
)


def serialize_review_workflow(record: ReviewWorkflowRecord) -> dict:
    """
    Flattens a review workflow into the lean database row shape.

    `state_json` is the source of truth. The extra top-level fields exist only
    to make startup lookups for active workflows cheap and easy to inspect.
    """
    return {
        "id": record.id,
        "workflow_status": record.workflow_status.value,
        "updated_at": record.updated_at,
        "state_json": record.model_dump_json(),
    }


def deserialize_review_workflow(row: dict) -> ReviewWorkflowRecord:
    """Reconstructs a typed review workflow record from a database row."""
    return ReviewWorkflowRecord.model_validate_json(row["state_json"])


def save_review_workflow_sync(record: ReviewWorkflowRecord) -> None:
    """
    Persists a full Sunday review workflow snapshot to SQLite.

    The full record is rewritten on each checkpoint so restart recovery always
    reads one self-contained durable state object.
    """
    db = get_db()
    if REVIEW_WORKFLOWS_TABLE not in db.table_names():
        raise RuntimeError(
            "review_workflows table is missing. Initialize the database schema before saving review workflows."
        )

    table = db[REVIEW_WORKFLOWS_TABLE]
    payload = serialize_review_workflow(record)
    existing_rows = list(table.rows_where("id = ?", [record.id]))  # type: ignore

    if existing_rows:
        table.update(record.id, payload)  # type: ignore
    else:
        table.insert(payload)  # type: ignore


def load_review_workflow_sync(review_id: str) -> ReviewWorkflowRecord | None:
    """Loads one persisted Sunday review workflow from SQLite."""
    db = get_db()
    if REVIEW_WORKFLOWS_TABLE not in db.table_names():
        return None

    table = db[REVIEW_WORKFLOWS_TABLE]
    rows = list(table.rows_where("id = ?", [review_id]))  # type: ignore
    if not rows:
        return None
    return deserialize_review_workflow(rows[0])


def load_resumable_review_workflows_sync() -> list[ReviewWorkflowRecord]:
    """
    Returns review workflows that should survive process restarts.

    This query is intentionally narrow so ordinary expired history does not
    get reloaded during startup reconciliation.
    """
    db = get_db()
    if REVIEW_WORKFLOWS_TABLE not in db.table_names():
        return []

    table = db[REVIEW_WORKFLOWS_TABLE]
    rows = list(
        table.rows_where(
            "workflow_status IN (?, ?)",
            list(RESUMABLE_REVIEW_STATUSES),
        )
    )  # type: ignore

    records: list[ReviewWorkflowRecord] = []
    for row in rows:
        records.append(deserialize_review_workflow(row))
    return records
