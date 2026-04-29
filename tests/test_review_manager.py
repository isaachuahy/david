from unittest.mock import patch

import pytest

from orchestrator.review_manager import (
    execute_weekly_state_update,
    run_goals_audit_stage,
    run_memory_audit_stage,
    run_sunday_review,
    run_week_review_stage,
    start_weekly_review_workflow,
)
from persistence.models import (
    ReviewStage,
    ReviewWorkflowRecord,
    ReviewWorkflowStatus,
    SourceSnapshot,
    StageCheckpoint,
    StageStatus,
)
from reasoning.pro_client import SundayReviewResponse
from reasoning.schemas import GoalsAuditResponse, MemoryAuditResponse, WeekReviewResponse


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


@patch("orchestrator.review_manager.capture_sentry_exception")
@patch("orchestrator.review_manager.generate_sunday_review", side_effect=RuntimeError("Sunday review failed"))
@patch("orchestrator.review_manager.get_past_events", return_value=[])
@patch("orchestrator.review_manager.build_context", return_value="<CONTEXT>")
def test_run_sunday_review_reports_failures(
    mock_build_context,
    mock_get_past_events,
    mock_generate_sunday_review,
    mock_capture_exception,
):
    with pytest.raises(RuntimeError, match="Sunday review failed"):
        run_sunday_review()

    mock_capture_exception.assert_called_once_with(
        mock_generate_sunday_review.side_effect,
        component="review_manager",
        operation="run_sunday_review",
    )


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_run_week_review_stage_persists_week_review_checkpoint(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """
    Tests that the real week-review stage maps the LLM response into the
    durable review workflow checkpoint without touching Gemini or SQLite.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
            past_week_events=["[2026-04-27T09:00:00-04:00] Deep Work"],
        ),
    )
    mock_generate_review_structured.return_value = WeekReviewResponse(
        summary="The week moved core planning forward but left one open loop.",
        key_findings=["Planning work progressed.", "One follow-up stayed unresolved."],
        constraints=["Late-evening blocks were not ideal."],
        carry_forward=["Revisit the unresolved follow-up next week."],
    )

    updated_record = await run_week_review_stage(record)

    assert updated_record.week_review is not None
    assert updated_record.week_review.summary == "The week moved core planning forward but left one open loop."
    assert updated_record.week_review.key_findings == [
        "Planning work progressed.",
        "One follow-up stayed unresolved.",
    ]
    assert updated_record.week_review.constraints == ["Late-evening blocks were not ideal."]
    assert updated_record.week_review.carry_forward == ["Revisit the unresolved follow-up next week."]
    assert updated_record.last_completed_stage == ReviewStage.WEEK_REVIEW
    mock_generate_review_structured.assert_called_once()
    mock_save_review_workflow_sync.assert_called_once()


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_run_goals_audit_stage_persists_goals_audit_checkpoint(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """
    Tests that goals_audit consumes an existing week_review checkpoint and
    persists its own durable checkpoint.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
        week_review=StageCheckpoint(
            summary="The week progressed but left a prioritization question.",
            key_findings=["MVP work moved forward."],
            constraints=["Evenings were lower-energy."],
            carry_forward=["Clarify job-search emphasis."],
        ),
    )
    mock_generate_review_structured.return_value = GoalsAuditResponse(
        summary="The goals still hold, but job-search emphasis should be reconfirmed.",
        key_findings=["MVP and job-search goals both remain relevant."],
        constraints=["Weekly planning should not overload evenings."],
        carry_forward=["Ask whether job-search volume should increase next week."],
    )

    updated_record = await run_goals_audit_stage(record)

    assert updated_record.goals_audit is not None
    assert updated_record.goals_audit.summary == (
        "The goals still hold, but job-search emphasis should be reconfirmed."
    )
    assert updated_record.goals_audit.key_findings == [
        "MVP and job-search goals both remain relevant.",
    ]
    assert updated_record.goals_audit.constraints == [
        "Weekly planning should not overload evenings.",
    ]
    assert updated_record.goals_audit.carry_forward == [
        "Ask whether job-search volume should increase next week.",
    ]
    assert updated_record.last_completed_stage == ReviewStage.GOALS_AUDIT
    mock_generate_review_structured.assert_called_once()
    mock_save_review_workflow_sync.assert_called_once()


@pytest.mark.asyncio
async def test_run_goals_audit_stage_requires_week_review_checkpoint():
    """Tests that goals_audit cannot run before week_review has produced evidence."""
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
    )

    with pytest.raises(ValueError, match="week_review is checkpointed"):
        await run_goals_audit_stage(record)


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_run_memory_audit_stage_persists_memory_audit_checkpoint(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """
    Tests that memory_audit consumes prior checkpoints and persists its own
    durable checkpoint without rewriting memory yet.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
        week_review=StageCheckpoint(
            summary="The week surfaced a recurring evening-energy constraint.",
        ),
        goals_audit=StageCheckpoint(
            summary="The goals still hold, with job-search emphasis to reconfirm.",
        ),
    )
    mock_generate_review_structured.return_value = MemoryAuditResponse(
        summary="Rolling memory is mostly useful, but evening constraints should be made durable.",
        key_findings=["The decision log should preserve the recurring evening-energy pattern."],
        constraints=["Avoid treating one-off schedule details as durable memory."],
        carry_forward=["Consider adding a compact evening-energy preference to rolling context."],
    )

    updated_record = await run_memory_audit_stage(record)

    assert updated_record.memory_audit is not None
    assert updated_record.memory_audit.summary == (
        "Rolling memory is mostly useful, but evening constraints should be made durable."
    )
    assert updated_record.memory_audit.key_findings == [
        "The decision log should preserve the recurring evening-energy pattern.",
    ]
    assert updated_record.memory_audit.constraints == [
        "Avoid treating one-off schedule details as durable memory.",
    ]
    assert updated_record.memory_audit.carry_forward == [
        "Consider adding a compact evening-energy preference to rolling context.",
    ]
    assert updated_record.last_completed_stage == ReviewStage.MEMORY_AUDIT
    mock_generate_review_structured.assert_called_once()
    mock_save_review_workflow_sync.assert_called_once()


@pytest.mark.asyncio
async def test_run_memory_audit_stage_requires_prior_checkpoints():
    """Tests that memory_audit waits for both week_review and goals_audit."""
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown="# Decision Log",
        ),
    )

    with pytest.raises(ValueError, match="week_review is checkpointed"):
        await run_memory_audit_stage(record)

    record.week_review = StageCheckpoint(summary="Week review exists.")
    with pytest.raises(ValueError, match="goals_audit is checkpointed"):
        await run_memory_audit_stage(record)


@pytest.mark.asyncio
@patch("orchestrator.review_manager.run_sunday_review")
@patch("orchestrator.review_manager._generate_review_structured")
@patch("orchestrator.review_manager.build_review_source_snapshot")
@patch("orchestrator.review_manager.save_review_workflow_sync")
async def test_start_weekly_review_workflow_checkpoints_week_review_before_bridge(
    mock_save_review_workflow_sync,
    mock_build_review_source_snapshot,
    mock_generate_review_structured,
    mock_run_sunday_review,
):
    """
    Tests the bridge workflow: week_review is checkpointed first, then the
    legacy one-shot Sunday review produces the user-facing final-review state.
    """
    mock_build_review_source_snapshot.return_value = SourceSnapshot(
        goals_markdown="# Goals",
        weekly_state_markdown="# Weekly State",
        decision_log_markdown="# Decision Log",
        past_week_events=["[2026-04-27T09:00:00-04:00] Deep Work"],
    )
    mock_generate_review_structured.side_effect = [
        WeekReviewResponse(
            summary="The week had useful progress.",
            key_findings=["Context routing advanced."],
            constraints=[],
            carry_forward=["Carry context cleanup forward."],
        ),
        GoalsAuditResponse(
            summary="The durable goals still look accurate.",
            key_findings=["MVP goal remains active."],
            constraints=[],
            carry_forward=["Keep goal emphasis unchanged."],
        ),
        MemoryAuditResponse(
            summary="Rolling memory remains useful.",
            key_findings=["Keep the current durable preference signal."],
            constraints=[],
            carry_forward=["No major memory changes needed."],
        ),
    ]
    sunday_review = SundayReviewResponse(
        message="Sunday review summary.",
        state_change_summary="Weekly state was updated.",
        weekly_state_content="# Weekly State\n\nUpdated.",
        proposed_events=[],
    )
    mock_run_sunday_review.return_value = sunday_review

    record, review = await start_weekly_review_workflow()

    assert review == sunday_review
    assert record.week_review is not None
    assert record.week_review.summary == "The week had useful progress."
    assert record.goals_audit is not None
    assert record.goals_audit.summary == "The durable goals still look accurate."
    assert record.memory_audit is not None
    assert record.memory_audit.summary == "Rolling memory remains useful."
    assert record.final_review is not None
    assert record.final_review.summary == "Sunday review summary."
    assert record.final_review.key_findings == ["Weekly state was updated."]
    assert record.workflow_status == ReviewWorkflowStatus.AWAITING_FEEDBACK
    assert record.current_stage == ReviewStage.FINAL_REVIEW
    assert record.stage_status == StageStatus.AWAITING_FEEDBACK
    assert record.last_completed_stage == ReviewStage.FINAL_REVIEW
    assert mock_generate_review_structured.call_count == 3
    assert mock_save_review_workflow_sync.call_count >= 9


@patch("orchestrator.review_manager.capture_sentry_exception")
@patch("orchestrator.review_manager.get_context_dir", side_effect=OSError("disk error"))
def test_execute_weekly_state_update_reports_failures(
    mock_get_context_dir,
    mock_capture_exception,
):
    success = execute_weekly_state_update("# Updated Weekly State")

    assert success is False
    error = mock_capture_exception.call_args.args[0]
    assert "disk error" in str(error)
    mock_capture_exception.assert_called_once_with(
        error,
        component="review_manager",
        operation="execute_weekly_state_update",
    )
