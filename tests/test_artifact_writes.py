from unittest.mock import patch

import pytest

from orchestrator.artifact_writes import (
    create_artifact_write,
    execute_artifact_write,
    reconcile_artifact_writes,
    retry_artifact_write,
)
from persistence.artifact_writes import (
    load_artifact_write_sync,
    save_artifact_write_sync,
)
from persistence.context_files import (
    append_to_decision_log,
    list_context_versions,
    read_context_file,
    replace_context_file,
)
from persistence.database import get_db, init_db
from persistence.models import (
    ArtifactType,
    ArtifactWriteRecord,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
)


@pytest.fixture(autouse=True)
def artifact_runtime(tmp_path, monkeypatch):
    """Keep every write test away from live context, SQLite, and telemetry."""
    context_dir = tmp_path / "context"
    monkeypatch.setenv("DAVID_CONTEXT_DIR", str(context_dir))
    monkeypatch.setenv("DAVID_DB_PATH", str(tmp_path / "assistant.db"))
    monkeypatch.setattr(
        "orchestrator.artifact_writes.capture_sentry_exception",
        lambda *args, **kwargs: None,
    )
    init_db()
    return context_dir


@pytest.fixture
def pending_goal_write(artifact_runtime):
    """Create the persisted operation an editor submits against a loaded file."""
    base = replace_context_file(
        ArtifactType.GOALS,
        "# Goals\n- Original goal\n",
        expected_revision="missing",
    )
    return create_artifact_write(
        artifact_type=ArtifactType.GOALS,
        content="# Goals\n- Confirmed goal\n",
        source_type=ArtifactWriteSourceType.MANUAL_EDIT,
        source_id="manual_revision_check",
        expected_revision=base.revision,
    )


@patch("orchestrator.artifact_writes.find_artifact_write_by_source_sync")
@patch("orchestrator.artifact_writes.save_artifact_write_sync")
def test_create_artifact_write_persists_retryable_record(
    mock_save_artifact_write_sync,
    mock_find_artifact_write_by_source_sync,
):
    """A newly confirmed operation captures and persists its current file base."""
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
    assert record.expected_revision == "missing"
    mock_save_artifact_write_sync.assert_called_once_with(record)


@patch("orchestrator.artifact_writes.find_artifact_write_by_source_sync")
@patch("orchestrator.artifact_writes.save_artifact_write_sync")
def test_create_artifact_write_reuses_existing_source_record(
    mock_save_artifact_write_sync,
    mock_find_artifact_write_by_source_sync,
):
    """Repeating confirmation reuses its record instead of creating a new write."""
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
    """Successful execution passes the stored revision to the file replacement."""
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
        expected_revision=record.expected_revision,
    )


@patch("orchestrator.artifact_writes.find_artifact_write_by_source_sync")
@patch("orchestrator.artifact_writes.save_artifact_write_sync")
@patch("orchestrator.artifact_writes.execute_artifact_replacement")
def test_execute_artifact_write_marks_retryable_failure(
    mock_execute_artifact_replacement,
    mock_save_artifact_write_sync,
    mock_find_artifact_write_by_source_sync,
):
    """A failed replacement keeps the confirmed payload available for retry."""
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
    """Startup exposes interrupted operations without silently executing them."""
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
    """Retry completes an already-applied payload without replacing it again."""
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


def test_stale_write_preserves_newer_context(pending_goal_write):
    current = read_context_file(ArtifactType.GOALS)
    newer = replace_context_file(
        ArtifactType.GOALS,
        "# Goals\n- Newer manual edit\n",
        expected_revision=current.revision,
    )

    with patch("orchestrator.artifact_writes.capture_sentry_exception") as capture:
        executed = execute_artifact_write(pending_goal_write)

    assert executed.status == ArtifactWriteStatus.FAILED_TERMINAL
    assert executed.executed_at is None
    assert "Create a fresh proposal" in executed.last_error
    assert read_context_file(ArtifactType.GOALS) == newer
    assert load_artifact_write_sync(executed.id) == executed
    capture.assert_not_called()

    retried = retry_artifact_write(executed.id)
    assert retried.status == ArtifactWriteStatus.FAILED_TERMINAL
    assert retried.attempts == executed.attempts
    assert read_context_file(ArtifactType.GOALS) == newer


def test_disk_failure_retry_preserves_original_revision(pending_goal_write, artifact_runtime):
    original = read_context_file(ArtifactType.GOALS)
    with patch("persistence.context_files.os.replace", side_effect=OSError("disk full")):
        failed = execute_artifact_write(pending_goal_write)

    assert failed.status == ArtifactWriteStatus.FAILED_RETRYABLE
    assert failed.executed_at is None
    assert read_context_file(ArtifactType.GOALS) == original
    assert load_artifact_write_sync(failed.id).expected_revision == original.revision
    assert not list(artifact_runtime.glob(".*.tmp"))

    retried = retry_artifact_write(failed.id)
    assert retried.status == ArtifactWriteStatus.EXECUTED
    assert retried.attempts == 2
    assert retried.expected_revision == original.revision
    assert retried.last_error is None
    assert (artifact_runtime / "goals.md").read_bytes() == retried.content.encode("utf-8")
    assert load_artifact_write_sync(retried.id) == retried


def test_retry_refuses_edits_made_after_disk_failure(pending_goal_write):
    with patch("persistence.context_files.os.replace", side_effect=OSError("disk full")):
        failed = execute_artifact_write(pending_goal_write)

    current = read_context_file(ArtifactType.GOALS)
    newer = replace_context_file(
        ArtifactType.GOALS,
        "# Goals\n- Edit made after the failure\n",
        expected_revision=current.revision,
    )
    retried = retry_artifact_write(failed.id)

    assert retried.status == ArtifactWriteStatus.FAILED_TERMINAL
    assert retried.expected_revision == current.revision
    assert retried.attempts == 2
    assert read_context_file(ArtifactType.GOALS) == newer
    assert load_artifact_write_sync(retried.id) == retried


def test_completed_write_does_not_replay_over_later_edits(pending_goal_write):
    executed = execute_artifact_write(pending_goal_write)
    current = read_context_file(ArtifactType.GOALS)
    newer = replace_context_file(
        ArtifactType.GOALS,
        "# Goals\n- Subsequent goal\n",
        expected_revision=current.revision,
    )
    attempts = executed.attempts

    assert execute_artifact_write(executed).status == ArtifactWriteStatus.EXECUTED
    assert retry_artifact_write(executed.id).status == ArtifactWriteStatus.EXECUTED
    assert load_artifact_write_sync(executed.id).attempts == attempts
    assert read_context_file(ArtifactType.GOALS) == newer


@pytest.mark.parametrize("already_applied", [False, True], ids=["newer_context", "applied"])
def test_legacy_write_requires_base_unless_already_applied(artifact_runtime, already_applied):
    """Older persisted proposals cannot infer a new base during operational retry."""
    legacy = ArtifactWriteRecord(
        id="awrite_legacy",
        artifact_type=ArtifactType.GOALS,
        content="# Goals\n- Legacy proposal\n",
        source_type=ArtifactWriteSourceType.MANUAL_EDIT,
        status=ArtifactWriteStatus.FAILED_RETRYABLE,
        created_at="2026-10-08T00:00:00+00:00",
        updated_at="2026-10-08T00:00:00+00:00",
    )
    save_artifact_write_sync(legacy)
    current = replace_context_file(
        ArtifactType.GOALS,
        legacy.content if already_applied else "# Goals\n- Newer context\n",
        expected_revision="missing",
    )

    retried = retry_artifact_write(legacy.id)

    assert retried.expected_revision is None
    expected_status = (
        ArtifactWriteStatus.EXECUTED if already_applied else ArtifactWriteStatus.FAILED_TERMINAL
    )
    assert retried.status == expected_status
    assert read_context_file(ArtifactType.GOALS) == current
    assert list_context_versions(ArtifactType.GOALS) == []
    assert load_artifact_write_sync(legacy.id) == retried


@pytest.mark.parametrize(
    "override",
    [{"content": "# Goals\n- Different proposal\n"}, {"expected_revision": "missing"}],
    ids=["different_content", "different_base"],
)
def test_operation_reuse_rejects_different_payload_or_base(pending_goal_write, override):
    arguments = {
        "artifact_type": pending_goal_write.artifact_type,
        "content": pending_goal_write.content,
        "source_type": pending_goal_write.source_type,
        "source_id": pending_goal_write.source_id,
        "expected_revision": pending_goal_write.expected_revision,
    }
    arguments.update(override)
    original = read_context_file(ArtifactType.GOALS)

    with pytest.raises(ValueError, match="different content or revision"):
        create_artifact_write(**arguments)

    assert get_db()["artifact_writes"].count == 1
    assert load_artifact_write_sync(pending_goal_write.id) == pending_goal_write
    assert read_context_file(ArtifactType.GOALS) == original


def test_duplicate_save_reuses_original_operation(pending_goal_write):
    executed = execute_artifact_write(pending_goal_write)
    duplicate = create_artifact_write(
        artifact_type=executed.artifact_type,
        content=executed.content,
        source_type=executed.source_type,
        source_id=executed.source_id,
        expected_revision=executed.expected_revision,
    )

    assert duplicate.id == executed.id
    assert execute_artifact_write(duplicate).status == ArtifactWriteStatus.EXECUTED
    assert load_artifact_write_sync(executed.id).attempts == 1
    assert get_db()["artifact_writes"].count == 1
    assert read_context_file(ArtifactType.GOALS).content == executed.content
    assert len(list_context_versions(ArtifactType.GOALS)) == 1


def test_weekly_snapshot_retry_does_not_replace_applied_markdown(artifact_runtime):
    """A snapshot failure after saving can recover without another file replacement."""
    base = replace_context_file(
        ArtifactType.WEEKLY_STATE, "# Previous week\n", expected_revision="missing",
    )
    record = create_artifact_write(
        artifact_type=ArtifactType.WEEKLY_STATE,
        content="# Confirmed week\n",
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id="review_snapshot_check",
        source_stage="weekly_plan",
        expected_revision=base.revision,
    )

    with patch(
        "orchestrator.artifact_writes._persist_weekly_snapshot",
        side_effect=OSError("snapshot failed"),
    ):
        failed = execute_artifact_write(record)

    assert failed.status == ArtifactWriteStatus.FAILED_RETRYABLE
    applied = read_context_file(ArtifactType.WEEKLY_STATE)
    assert applied.content == record.content
    backups = list_context_versions(ArtifactType.WEEKLY_STATE)
    assert len(backups) == 1
    assert get_db()["weekly_snapshots"].count == 0

    retried = retry_artifact_write(record.id)

    assert retried.status == ArtifactWriteStatus.EXECUTED
    assert retried.attempts == 2
    assert retried.expected_revision == base.revision
    assert read_context_file(ArtifactType.WEEKLY_STATE) == applied
    assert list_context_versions(ArtifactType.WEEKLY_STATE) == backups
    snapshots = list(get_db()["weekly_snapshots"].rows)
    assert len(snapshots) == 1
    assert snapshots[0]["weekly_state_content"] == record.content
    retry_artifact_write(record.id)
    assert get_db()["weekly_snapshots"].count == 1


def test_restart_recovery_does_not_replace_already_applied_content(pending_goal_write):
    """A restart between file commit and record completion preserves the original base."""
    applied = replace_context_file(
        ArtifactType.GOALS,
        pending_goal_write.content,
        expected_revision=pending_goal_write.expected_revision,
    )
    # Reproduce a crash after committing Markdown but before recording success;
    # startup should expose a retry rather than regenerate a proposal.
    pending_goal_write.status = ArtifactWriteStatus.EXECUTING
    pending_goal_write.attempts = 1
    save_artifact_write_sync(pending_goal_write)
    backups = list_context_versions(ArtifactType.GOALS)

    recovered = reconcile_artifact_writes()

    assert len(recovered) == 1
    assert recovered[0].id == pending_goal_write.id
    assert recovered[0].status == ArtifactWriteStatus.FAILED_RETRYABLE
    assert recovered[0].expected_revision == pending_goal_write.expected_revision
    retried = retry_artifact_write(pending_goal_write.id)
    assert retried.status == ArtifactWriteStatus.EXECUTED
    assert retried.attempts == 2
    assert read_context_file(ArtifactType.GOALS) == applied
    assert list_context_versions(ArtifactType.GOALS) == backups


def test_concurrent_identical_payload_does_not_replace_twice(pending_goal_write):
    """Two identical file-save attempts can complete without creating another backup."""
    def apply_competing_attempt(artifact_type, content, *, expected_revision):
        """Commit an identical save immediately before this attempt's revision check."""
        replace_context_file(artifact_type, content, expected_revision=expected_revision)
        return replace_context_file(artifact_type, content, expected_revision=expected_revision)

    with patch(
        "orchestrator.artifact_writes.replace_context_file",
        side_effect=apply_competing_attempt,
    ):
        executed = execute_artifact_write(pending_goal_write)

    assert executed.status == ArtifactWriteStatus.EXECUTED
    assert read_context_file(ArtifactType.GOALS).content == executed.content
    assert len(list_context_versions(ArtifactType.GOALS)) == 1


def test_session_append_invalidates_older_editor_draft(artifact_runtime):
    base = append_to_decision_log("## Recent Decisions (Appended Daily)\n- Earlier note")
    record = create_artifact_write(
        artifact_type=ArtifactType.DECISION_LOG,
        content="## Recent Decisions (Appended Daily)\n- Editor draft\n",
        source_type=ArtifactWriteSourceType.MANUAL_EDIT,
        source_id="manual_memory_check",
        expected_revision=base.revision,
    )
    with_note = append_to_decision_log("- Newly synthesized note")

    executed = execute_artifact_write(record)

    assert executed.status == ArtifactWriteStatus.FAILED_TERMINAL
    assert read_context_file(ArtifactType.DECISION_LOG) == with_note
    assert "- Earlier note" in with_note.content
    assert "- Newly synthesized note" in with_note.content
