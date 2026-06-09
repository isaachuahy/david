from unittest.mock import patch

import pytest

from orchestrator.review_manager import (
    advance_review_from_current_stage,
    build_final_review_message,
    execute_weekly_state_update,
    run_goals_audit_stage,
    run_memory_audit_stage,
    run_scheduling_pass_stage,
    run_week_review_stage,
    run_weekly_plan_stage,
    revise_review_stage,
    start_weekly_review_workflow,
)
from persistence.models import (
    ArtifactChangeSummary,
    ReviewStage,
    ReviewWorkflowRecord,
    ReviewWorkflowStatus,
    SchedulingPassArtifact,
    SourceSnapshot,
    StageCheckpoint,
    StageStatus,
)
from reasoning.schemas import (
    DecisionLogChangeProposalResponse,
    GoalsAuditResponse,
    GoalsChangeProposalResponse,
    MemoryAuditResponse,
    ProposedEvent,
    SchedulingPassResponse,
    WeekReviewResponse,
    WeeklyPlanResponse,
)


VALID_WEEKLY_STATE_MARKDOWN = """# Weekly State

This file tracks this week's active priorities.

## This Week

### Top Priorities
- [ ] Finish review workflow implementation.

### Carryover
- [ ] Keep context cleanup moving.

### Constraints
- Avoid late-evening event proposals.

### Execution Focus
- Prefer fewer, higher-confidence commitments.
"""


VALID_DECISION_LOG_MARKDOWN = """# Decision Log

This file stores durable memory for David across weeks.

## Current Rolling Context
- David prefers fewer, higher-confidence commitments.
- Late-evening commitments are currently experimental.

## Recent Decisions (Appended Daily)
- Dentist appointment was missed in the original week review.
"""


VALID_GOALS_MARKDOWN = """# Goals

This file stores durable goals and operating principles for David.

## Long-Term
- Build durable technical and product judgment.

## Medium-Term
- Complete the David MVP.

## Operating Principles
- Protect morning deep work.
"""


@patch("orchestrator.artifact_writes.get_db")
@patch("orchestrator.artifact_writes.get_context_dir")
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
            decision_log_markdown=VALID_DECISION_LOG_MARKDOWN,
        ),
        week_review=StageCheckpoint(
            summary="The week progressed but left a prioritization question.",
            key_findings=["MVP work moved forward."],
            constraints=["Evenings were lower-energy."],
            carry_forward=["Clarify job-search emphasis."],
        ),
    )
    mock_generate_review_structured.side_effect = [
        GoalsAuditResponse(
            summary="The goals still hold, but job-search emphasis should be reconfirmed.",
            key_findings=["MVP and job-search goals both remain relevant."],
            constraints=["Weekly planning should not overload evenings."],
            carry_forward=["Ask whether job-search volume should increase next week."],
        ),
        GoalsChangeProposalResponse(
            proposed_change_summary="No durable goals change is justified yet.",
            proposed_markdown=None,
            requires_user_reconfirmation=False,
        ),
    ]

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
    assert updated_record.goals_changes is None
    assert updated_record.last_completed_stage == ReviewStage.GOALS_AUDIT
    assert mock_generate_review_structured.call_count == 2
    assert mock_save_review_workflow_sync.call_count == 2


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_run_goals_audit_stage_persists_valid_goals_change(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """Tests that a validated goals proposal is stored for later confirmation."""
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown=VALID_GOALS_MARKDOWN,
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown=VALID_DECISION_LOG_MARKDOWN,
        ),
        week_review=StageCheckpoint(
            summary="The week showed that morning deep work remains important.",
        ),
    )
    updated_goals = VALID_GOALS_MARKDOWN.replace(
        "- Complete the David MVP.",
        "- Complete the David MVP without overfilling evening recovery time.",
    )
    mock_generate_review_structured.side_effect = [
        GoalsAuditResponse(
            summary="The MVP goal still holds, with an added recovery constraint.",
            key_findings=["Evening overload is recurring enough to affect goals wording."],
        ),
        GoalsChangeProposalResponse(
            proposed_change_summary="Clarify that MVP work should not consume evening recovery time.",
            proposed_markdown=updated_goals,
            requires_user_reconfirmation=True,
        ),
    ]

    updated_record = await run_goals_audit_stage(record)

    assert updated_record.goals_changes is not None
    assert updated_record.goals_changes.modifications == [
        "Clarify that MVP work should not consume evening recovery time.",
    ]
    assert updated_record.goals_changes.proposed_markdown == updated_goals.strip()
    assert mock_generate_review_structured.call_count == 2
    mock_save_review_workflow_sync.assert_called()


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_run_goals_audit_stage_rejects_invalid_goals_markdown(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """Tests that malformed goals proposals fail before reaching confirmation."""
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown=VALID_GOALS_MARKDOWN,
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown=VALID_DECISION_LOG_MARKDOWN,
        ),
        week_review=StageCheckpoint(summary="The week progressed."),
    )
    mock_generate_review_structured.side_effect = [
        GoalsAuditResponse(summary="A goals update may be useful."),
        GoalsChangeProposalResponse(
            proposed_change_summary="Invalid proposal missing operating principles.",
            proposed_markdown="# Goals\n\n## Long-Term\n- Build David.\n",
        ),
    ]

    with pytest.raises(ValueError, match="missing required section"):
        await run_goals_audit_stage(record)

    assert mock_generate_review_structured.call_count == 2
    assert mock_save_review_workflow_sync.call_count == 1


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
            decision_log_markdown=VALID_DECISION_LOG_MARKDOWN,
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
    durable checkpoint plus compact proposed decision-log changes.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown="# Weekly State",
            decision_log_markdown=VALID_DECISION_LOG_MARKDOWN,
        ),
        week_review=StageCheckpoint(
            summary="The week surfaced a recurring evening-energy constraint.",
        ),
        goals_audit=StageCheckpoint(
            summary="The goals still hold, with job-search emphasis to reconfirm.",
        ),
    )
    mock_generate_review_structured.side_effect = [
        MemoryAuditResponse(
            summary="Rolling memory is mostly useful, but evening constraints should be made durable.",
            key_findings=["The decision log should preserve the recurring evening-energy pattern."],
            constraints=["Avoid treating one-off schedule details as durable memory."],
            carry_forward=["Consider adding a compact evening-energy preference to rolling context."],
        ),
        DecisionLogChangeProposalResponse(
            proposed_rolling_context_additions=[
                "- David works better when late-evening commitments are avoided.",
            ],
            proposed_rolling_context_deletions=[],
            proposed_rolling_context_modifications={
                "- Late-evening commitments are currently experimental.": (
                    "- Late-evening commitments should be avoided unless explicitly requested."
                ),
            },
            proposed_recent_decisions_reset=True,
            proposed_recent_decisions_carry_forward=[],
        ),
    ]

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
    assert updated_record.decision_log_changes is not None
    assert updated_record.decision_log_changes.additions == [
        "- David works better when late-evening commitments are avoided.",
    ]
    assert updated_record.decision_log_changes.deletions == []
    assert updated_record.decision_log_changes.modifications == [
        "- Late-evening commitments are currently experimental. -> "
        "- Late-evening commitments should be avoided unless explicitly requested.",
    ]
    assert updated_record.decision_log_changes.proposed_markdown is not None
    assert "- David works better when late-evening commitments are avoided." in (
        updated_record.decision_log_changes.proposed_markdown
    )
    assert "Dentist appointment was missed" not in updated_record.decision_log_changes.proposed_markdown
    assert updated_record.last_completed_stage == ReviewStage.MEMORY_AUDIT
    assert mock_generate_review_structured.call_count == 2
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
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_run_weekly_plan_stage_persists_checkpoint_and_weekly_state_artifact(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """
    Tests that weekly_plan converts prior checkpoints into both a durable
    checkpoint and the proposed markdown artifact for user confirmation.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown="# Decision Log",
        ),
        week_review=StageCheckpoint(summary="The week moved implementation forward."),
        goals_audit=StageCheckpoint(summary="Goals remain accurate."),
        memory_audit=StageCheckpoint(summary="Memory needs only light cleanup."),
    )
    mock_generate_review_structured.return_value = WeeklyPlanResponse(
        summary="The next week should focus on completing the staged review flow.",
        key_findings=["Weekly planning should stay implementation-focused."],
        constraints=["Avoid late-evening event proposals."],
        carry_forward=["Finish Sunday review orchestration."],
        state_change_summary="Tighten weekly priorities around the review workflow.",
        weekly_state_content=VALID_WEEKLY_STATE_MARKDOWN,
    )

    updated_record = await run_weekly_plan_stage(record)

    assert updated_record.weekly_plan is not None
    assert updated_record.weekly_plan.summary == (
        "The next week should focus on completing the staged review flow."
    )
    assert updated_record.weekly_plan.key_findings == [
        "Weekly planning should stay implementation-focused.",
    ]
    assert updated_record.weekly_state_changes is not None
    assert updated_record.weekly_state_changes.modifications == [
        "Tighten weekly priorities around the review workflow.",
    ]
    assert updated_record.weekly_state_changes.proposed_markdown == VALID_WEEKLY_STATE_MARKDOWN
    assert updated_record.last_completed_stage == ReviewStage.WEEKLY_PLAN
    mock_generate_review_structured.assert_called_once()
    mock_save_review_workflow_sync.assert_called_once()


@pytest.mark.asyncio
async def test_run_weekly_plan_stage_requires_prior_checkpoints():
    """Tests that weekly_plan waits for all upstream review checkpoints."""
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown="# Decision Log",
        ),
    )

    with pytest.raises(ValueError, match="week_review is checkpointed"):
        await run_weekly_plan_stage(record)

    record.week_review = StageCheckpoint(summary="Week review exists.")
    with pytest.raises(ValueError, match="goals_audit is checkpointed"):
        await run_weekly_plan_stage(record)

    record.goals_audit = StageCheckpoint(summary="Goals audit exists.")
    with pytest.raises(ValueError, match="memory_audit is checkpointed"):
        await run_weekly_plan_stage(record)


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_run_scheduling_pass_stage_persists_scheduling_checkpoint(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """
    Tests that scheduling_pass consumes the reviewed weekly plan and stores a
    durable checkpoint without writing calendar proposals yet.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown="# Decision Log",
            past_week_events=["[2026-04-27T09:00:00-04:00] Deep Work"],
            upcoming_events=["[2026-05-01T10:00:00-04:00] Existing Meeting"],
        ),
        week_review=StageCheckpoint(summary="The week moved implementation forward."),
        goals_audit=StageCheckpoint(summary="Goals remain accurate."),
        memory_audit=StageCheckpoint(summary="Memory needs only light cleanup."),
        weekly_plan=StageCheckpoint(summary="Next week should focus on finishing the workflow."),
        weekly_state_changes=ArtifactChangeSummary(
            proposed_markdown=VALID_WEEKLY_STATE_MARKDOWN,
        ),
    )
    proposed_event = ProposedEvent(
        summary="Workflow Implementation Block",
        start_time="2026-05-01T09:00:00-04:00",
        end_time="2026-05-01T11:00:00-04:00",
        description="Focused work on the staged Sunday review workflow.",
    )
    mock_generate_review_structured.return_value = SchedulingPassResponse(
        summary="One focus block would support the reviewed weekly plan.",
        key_findings=["A morning block best matches the stated constraints."],
        constraints=["Avoid late-evening event proposals."],
        carry_forward=[],
        proposed_events=[proposed_event],
        scheduling_rationale="A single focused implementation block is enough for now.",
    )

    updated_record = await run_scheduling_pass_stage(record)

    assert updated_record.scheduling_pass is not None
    assert updated_record.scheduling_pass.summary == (
        "One focus block would support the reviewed weekly plan."
    )
    assert updated_record.scheduling_pass.key_findings == [
        "A morning block best matches the stated constraints.",
    ]
    assert updated_record.scheduling_pass.constraints == [
        "Avoid late-evening event proposals.",
    ]
    assert updated_record.scheduling_proposals is not None
    assert updated_record.scheduling_proposals.scheduling_rationale == (
        "A single focused implementation block is enough for now."
    )
    assert updated_record.scheduling_proposals.proposed_events == [
        proposed_event.model_dump(mode="json"),
    ]
    assert updated_record.last_completed_stage == ReviewStage.SCHEDULING_PASS
    mock_generate_review_structured.assert_called_once()
    mock_save_review_workflow_sync.assert_called_once()


@pytest.mark.asyncio
async def test_run_scheduling_pass_stage_requires_reviewed_weekly_plan():
    """Tests that scheduling_pass waits for checkpoints and proposed weekly state."""
    record = ReviewWorkflowRecord(
        id="review_test",
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown="# Decision Log",
        ),
    )

    with pytest.raises(ValueError, match="week_review is checkpointed"):
        await run_scheduling_pass_stage(record)

    record.week_review = StageCheckpoint(summary="Week review exists.")
    with pytest.raises(ValueError, match="goals_audit is checkpointed"):
        await run_scheduling_pass_stage(record)

    record.goals_audit = StageCheckpoint(summary="Goals audit exists.")
    with pytest.raises(ValueError, match="memory_audit is checkpointed"):
        await run_scheduling_pass_stage(record)

    record.memory_audit = StageCheckpoint(summary="Memory audit exists.")
    with pytest.raises(ValueError, match="weekly_plan is checkpointed"):
        await run_scheduling_pass_stage(record)

    record.weekly_plan = StageCheckpoint(summary="Weekly plan exists.")
    with pytest.raises(ValueError, match="proposed weekly state is available"):
        await run_scheduling_pass_stage(record)


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_revise_review_stage_regenerates_stage_and_clears_downstream_outputs(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """
    Tests that revising an early review stage invalidates stale downstream work.

    This protects the staged Sunday review from context rot: if the factual week
    review changes, later audit, plan, scheduling, and final-review artifacts can
    no longer be treated as trustworthy.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
        current_stage=ReviewStage.WEEK_REVIEW,
        stage_status=StageStatus.AWAITING_FEEDBACK,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown="# Decision Log",
            past_week_events=["[2026-04-27T09:00:00-04:00] Deep Work"],
        ),
        week_review=StageCheckpoint(summary="The original week review missed one event."),
        goals_audit=StageCheckpoint(summary="Stale goals audit."),
        memory_audit=StageCheckpoint(summary="Stale memory audit."),
        weekly_plan=StageCheckpoint(summary="Stale weekly plan."),
        scheduling_pass=StageCheckpoint(summary="Stale scheduling pass."),
        final_review=StageCheckpoint(summary="Stale final review."),
        weekly_state_changes=ArtifactChangeSummary(
            modifications=["Stale weekly-state change."],
            proposed_markdown=VALID_WEEKLY_STATE_MARKDOWN,
        ),
        scheduling_proposals=SchedulingPassArtifact(
            proposed_events=[
                {
                    "summary": "Stale Event",
                    "start_time": "2026-05-01T09:00:00-04:00",
                    "end_time": "2026-05-01T10:00:00-04:00",
                    "description": "No longer trustworthy.",
                }
            ],
            scheduling_rationale="Stale rationale.",
        ),
    )
    mock_generate_review_structured.return_value = WeekReviewResponse(
        summary="The revised week review includes the missing dentist appointment.",
        key_findings=["A missed appointment changed the factual week review."],
        constraints=[],
        carry_forward=["Account for the appointment in later stages."],
    )

    updated_record = await revise_review_stage(
        record,
        stage=ReviewStage.WEEK_REVIEW,
        feedback="The week review missed the dentist appointment.",
    )

    assert updated_record.week_review is not None
    assert updated_record.week_review.summary == (
        "The revised week review includes the missing dentist appointment."
    )
    assert updated_record.goals_audit is None
    assert updated_record.memory_audit is None
    assert updated_record.weekly_plan is None
    assert updated_record.scheduling_pass is None
    assert updated_record.final_review is None
    assert updated_record.weekly_state_changes is None
    assert updated_record.scheduling_proposals is None
    assert updated_record.feedback_history == [
        "week_review: The week review missed the dentist appointment.",
    ]
    assert updated_record.workflow_status == ReviewWorkflowStatus.AWAITING_FEEDBACK
    assert updated_record.current_stage == ReviewStage.WEEK_REVIEW
    assert updated_record.stage_status == StageStatus.AWAITING_FEEDBACK
    assert updated_record.last_completed_stage == ReviewStage.WEEK_REVIEW
    assert "dentist appointment" in mock_generate_review_structured.call_args.kwargs["prompt"]
    assert mock_save_review_workflow_sync.call_count >= 3


@pytest.mark.asyncio
@patch("orchestrator.review_manager._generate_review_structured")
@patch("orchestrator.review_manager.build_review_source_snapshot")
@patch("orchestrator.review_manager.save_review_workflow_sync")
async def test_start_weekly_review_workflow_pauses_at_week_review_feedback(
    mock_save_review_workflow_sync,
    mock_build_review_source_snapshot,
    mock_generate_review_structured,
):
    """
    Tests that the initial workflow pauses at factual week-review confirmation
    before downstream audit and planning stages are allowed to run.
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
    ]
    record = await start_weekly_review_workflow()

    assert record.week_review is not None
    assert record.week_review.summary == "The week had useful progress."
    assert record.goals_audit is None
    assert record.memory_audit is None
    assert record.weekly_plan is None
    assert record.weekly_state_changes is None
    assert record.scheduling_pass is None
    assert record.scheduling_proposals is None
    assert record.final_review is None
    assert record.workflow_status == ReviewWorkflowStatus.AWAITING_FEEDBACK
    assert record.current_stage == ReviewStage.WEEK_REVIEW
    assert record.stage_status == StageStatus.AWAITING_FEEDBACK
    assert record.last_completed_stage == ReviewStage.WEEK_REVIEW
    assert mock_generate_review_structured.call_count == 1
    assert mock_save_review_workflow_sync.call_count >= 4


@pytest.mark.asyncio
@patch("orchestrator.review_manager.save_review_workflow_sync")
@patch("orchestrator.review_manager._generate_review_structured")
async def test_advance_review_from_completed_weekly_plan_runs_downstream_stages(
    mock_generate_review_structured,
    mock_save_review_workflow_sync,
):
    """
    Tests that accepted weekly-plan feedback is the gate before scheduling and
    final-review assembly can run.
    """
    record = ReviewWorkflowRecord(
        id="review_test",
        workflow_status=ReviewWorkflowStatus.ACTIVE,
        current_stage=ReviewStage.WEEKLY_PLAN,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=ReviewStage.WEEKLY_PLAN,
        created_at="2026-04-29T00:00:00+00:00",
        updated_at="2026-04-29T00:00:00+00:00",
        source_snapshot=SourceSnapshot(
            goals_markdown="# Goals",
            weekly_state_markdown=VALID_WEEKLY_STATE_MARKDOWN,
            decision_log_markdown="# Decision Log",
            past_week_events=["[2026-04-27T09:00:00-04:00] Deep Work"],
        ),
        week_review=StageCheckpoint(
            summary="The week had useful progress.",
            key_findings=["Context routing advanced."],
        ),
        goals_audit=StageCheckpoint(
            summary="The durable goals still look accurate.",
            key_findings=["MVP goal remains active."],
        ),
        memory_audit=StageCheckpoint(
            summary="Rolling memory remains useful.",
            key_findings=["Keep the current durable preference signal."],
        ),
        weekly_plan=StageCheckpoint(
            summary="The next week should focus on review-workflow implementation.",
            key_findings=["Staged review work is the highest-leverage priority."],
            constraints=["Do not overfill evenings."],
        ),
        weekly_state_changes=ArtifactChangeSummary(
            modifications=["Updated weekly state around staged review work."],
            proposed_markdown=VALID_WEEKLY_STATE_MARKDOWN,
        ),
    )
    mock_generate_review_structured.return_value = SchedulingPassResponse(
        summary="One focus block would support the staged review work.",
        key_findings=["A morning implementation block fits the constraints."],
        constraints=["Do not overfill evenings."],
        carry_forward=[],
        proposed_events=[
            ProposedEvent(
                summary="Review Workflow Focus Block",
                start_time="2026-05-01T09:00:00-04:00",
                end_time="2026-05-01T11:00:00-04:00",
                description="Implement the staged Sunday review workflow.",
            ),
        ],
        scheduling_rationale="A single focus block supports the weekly plan without overcommitting.",
    )

    updated_record = await advance_review_from_current_stage(record)

    assert updated_record.scheduling_pass is not None
    assert updated_record.scheduling_pass.summary == "One focus block would support the staged review work."
    assert updated_record.scheduling_proposals is not None
    assert len(updated_record.scheduling_proposals.proposed_events) == 1
    assert updated_record.final_review is not None
    assert updated_record.final_review.summary == build_final_review_message(updated_record)
    assert "*Scheduling Pass:* One focus block would support the staged review work." in updated_record.final_review.summary
    assert updated_record.final_review.key_findings == [
        "Context routing advanced.",
        "MVP goal remains active.",
        "Keep the current durable preference signal.",
        "Staged review work is the highest-leverage priority.",
        "A morning implementation block fits the constraints.",
    ]
    assert updated_record.final_review.carry_forward == [
        "Weekly state proposal is awaiting confirmation.",
        "1 scheduling-pass calendar proposal candidate(s) will be handed off for confirmation.",
    ]
    assert updated_record.workflow_status == ReviewWorkflowStatus.AWAITING_FEEDBACK
    assert updated_record.current_stage == ReviewStage.FINAL_REVIEW
    assert updated_record.stage_status == StageStatus.AWAITING_FEEDBACK
    assert updated_record.last_completed_stage == ReviewStage.FINAL_REVIEW


@patch("orchestrator.artifact_writes.capture_sentry_exception")
@patch("orchestrator.artifact_writes.get_context_dir", side_effect=OSError("disk error"))
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
        component="artifact_writes",
        operation="execute_artifact_replacement",
        tags={"artifact_type": "weekly_state"},
    )
