from telegram.ext import ContextTypes

from bot.keyboards import (
    build_artifact_write_retry_keyboard,
    build_review_stage_keyboard,
)
from bot.proposal_flow import send_weekly_review_scheduling_proposals
from orchestrator.artifact_writes import (
    create_artifact_write,
    execute_artifact_write,
)
from orchestrator.review_manager import (
    advance_review_from_current_stage,
    complete_review_after_event_feedback,
    load_review_workflow,
    revise_review_stage,
    transition_review_stage,
)
from persistence.artifact_writes import list_retryable_artifact_writes_sync
from persistence.models import (
    ArtifactType,
    ArtifactWriteSourceType,
    ArtifactWriteStatus,
    ReviewStage,
    ReviewWorkflowStatus,
    StageStatus,
)

ACTIVE_REVIEW_WORKFLOW_ID_KEY = "active_review_workflow_id"
ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY = "active_review_stage_confirmation"
ACTIVE_ARTIFACT_WRITE_RETRY_KEY = "active_artifact_write_retry"


def _get_review_stage_checkpoint(record, stage: ReviewStage):
    """Returns the checkpoint field that belongs to one review stage."""
    return {
        ReviewStage.WEEK_REVIEW: record.week_review,
        ReviewStage.GOALS_AUDIT: record.goals_audit,
        ReviewStage.MEMORY_AUDIT: record.memory_audit,
        ReviewStage.WEEKLY_PLAN: record.weekly_plan,
        ReviewStage.SCHEDULING_PASS: record.scheduling_pass,
        ReviewStage.FINAL_REVIEW: record.final_review,
    }.get(stage)


def _format_review_stage_summary(stage: ReviewStage, record) -> str:
    """
    Formats one review checkpoint for user confirmation.

    The text stays compact because stage revision is handled by the review
    workflow itself; this surface only needs to make confirmation legible.
    """
    checkpoint = _get_review_stage_checkpoint(record, stage)
    if checkpoint is None:
        raise ValueError(f"Review stage {stage.value} has no checkpoint to confirm.")

    lines = [
        f"*{stage.value.replace('_', ' ').title()} Ready*",
        "",
        checkpoint.summary or "No summary was provided.",
    ]
    if checkpoint.key_findings:
        lines.append("")
        lines.append("*Key findings:*")
        lines.extend(f"- {finding}" for finding in checkpoint.key_findings)
    if checkpoint.constraints:
        lines.append("")
        lines.append("*Constraints:*")
        lines.extend(f"- {constraint}" for constraint in checkpoint.constraints)
    return "\n".join(lines)


def _format_artifact_change_summary(changes) -> str:
    """
    Formats compact artifact-change proposals for confirmation messages.

    Sunday review stages store semantic diffs rather than raw line diffs. This
    keeps Telegram output readable while still showing what would change.
    """
    if changes is None:
        return "No artifact changes were proposed."

    lines: list[str] = []
    for label, values in (
        ("Additions", changes.additions),
        ("Deletions", changes.deletions),
        ("Modifications", changes.modifications),
    ):
        if values:
            lines.append(f"*{label}:*")
            lines.extend(f"- {value}" for value in values)

    return "\n".join(lines) if lines else "No artifact changes were proposed."


async def send_review_stage_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    record,
    stage: ReviewStage,
) -> None:
    """Presents one Sunday review stage checkpoint for confirm/revise feedback."""
    context.user_data[ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY] = {
        "review_id": record.id,
        "stage": stage.value,
    }
    await context.bot.send_message(
        chat_id=chat_id,
        text=_format_review_stage_summary(stage, record),
        reply_markup=build_review_stage_keyboard(stage.value),
        parse_mode="Markdown",
    )


async def send_memory_audit_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    record,
) -> None:
    """Presents memory-audit findings plus proposed decision-log changes."""
    context.user_data[ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY] = {
        "review_id": record.id,
        "stage": ReviewStage.MEMORY_AUDIT.value,
    }
    text = (
        f"{_format_review_stage_summary(ReviewStage.MEMORY_AUDIT, record)}\n\n"
        "*Proposed Decision Log Changes:*\n"
        f"{_format_artifact_change_summary(record.decision_log_changes)}"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=build_review_stage_keyboard(ReviewStage.MEMORY_AUDIT.value),
        parse_mode="Markdown",
    )


async def send_goals_audit_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    record,
) -> None:
    """Presents goals-audit findings plus optional proposed goals changes."""
    context.user_data[ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY] = {
        "review_id": record.id,
        "stage": ReviewStage.GOALS_AUDIT.value,
    }
    text = (
        f"{_format_review_stage_summary(ReviewStage.GOALS_AUDIT, record)}\n\n"
        "*Proposed Goals Changes:*\n"
        f"{_format_artifact_change_summary(record.goals_changes)}"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=build_review_stage_keyboard(ReviewStage.GOALS_AUDIT.value),
        parse_mode="Markdown",
    )


async def send_weekly_plan_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    record,
) -> None:
    """Presents the proposed weekly-state artifact for acceptance or revision."""
    if record.weekly_state_changes is None or not record.weekly_state_changes.proposed_markdown:
        raise ValueError("Sunday review has no proposed weekly-state markdown to confirm.")

    context.user_data[ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY] = {
        "review_id": record.id,
        "stage": ReviewStage.WEEKLY_PLAN.value,
    }
    text = (
        f"{_format_review_stage_summary(ReviewStage.WEEKLY_PLAN, record)}\n\n"
        "*Proposed Weekly State Changes:*\n"
        f"{_format_artifact_change_summary(record.weekly_state_changes)}"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=build_review_stage_keyboard(ReviewStage.WEEKLY_PLAN.value),
        parse_mode="Markdown",
    )


async def send_review_stage_gate(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    record,
) -> None:
    """
    Presents the correct user-facing gate for the workflow's current stage.

    The review manager owns stage transitions. This dispatcher only chooses the
    Telegram surface for the persisted stage, keeping stage-specific UI details
    out of callback and message handlers.
    """
    if record.current_stage == ReviewStage.WEEKLY_PLAN:
        await send_weekly_plan_confirmation(context, chat_id, record)
        return

    if record.current_stage == ReviewStage.MEMORY_AUDIT:
        await send_memory_audit_confirmation(context, chat_id, record)
        return

    if record.current_stage == ReviewStage.GOALS_AUDIT and record.goals_changes is not None:
        await send_goals_audit_confirmation(context, chat_id, record)
        return

    if record.current_stage in {
        ReviewStage.WEEK_REVIEW,
        ReviewStage.GOALS_AUDIT,
        ReviewStage.FINAL_REVIEW,
    }:
        await send_review_stage_confirmation(
            context,
            chat_id,
            record,
            record.current_stage,
        )
        return

    raise ValueError(f"Unsupported review-stage gate: {record.current_stage.value}.")


async def apply_confirmed_review_stage_artifacts(
    context: ContextTypes.DEFAULT_TYPE,
    stage: ReviewStage,
    record,
) -> bool:
    """
    Applies confirmed markdown artifacts that belong to a review-stage gate.

    Most checkpoint stages only advance. Memory audit and weekly plan also
    approve deterministic markdown replacements that downstream stages must see
    before the workflow can continue.
    """
    artifact_by_stage = {
        ReviewStage.GOALS_AUDIT: ArtifactType.GOALS,
        ReviewStage.MEMORY_AUDIT: ArtifactType.DECISION_LOG,
        ReviewStage.WEEKLY_PLAN: ArtifactType.WEEKLY_STATE,
    }
    changes_by_stage = {
        ReviewStage.GOALS_AUDIT: record.goals_changes,
        ReviewStage.MEMORY_AUDIT: record.decision_log_changes,
        ReviewStage.WEEKLY_PLAN: record.weekly_state_changes,
    }

    if stage not in artifact_by_stage:
        return True

    changes = changes_by_stage[stage]
    if changes is None or not changes.proposed_markdown:
        return True

    write = create_artifact_write(
        artifact_type=artifact_by_stage[stage],
        content=changes.proposed_markdown,
        source_type=ArtifactWriteSourceType.SUNDAY_REVIEW,
        source_id=record.id,
        source_stage=stage.value,
    )
    executed_write = execute_artifact_write(write)
    if executed_write.status == ArtifactWriteStatus.EXECUTED:
        context.user_data.pop(ACTIVE_ARTIFACT_WRITE_RETRY_KEY, None)
        return True

    context.user_data[ACTIVE_ARTIFACT_WRITE_RETRY_KEY] = {
        "write_id": executed_write.id,
        "review_id": record.id,
        "stage": stage.value,
    }
    return False


async def advance_review_after_confirmed_stage(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    record,
    stage: ReviewStage,
) -> None:
    """
    Marks a confirmed review stage complete and presents the next review gate.

    Normal confirmation and artifact-write retry both converge here once their
    required side effects have succeeded.
    """
    record = await transition_review_stage(
        record,
        workflow_status=ReviewWorkflowStatus.ACTIVE,
        stage=stage,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=stage,
    )
    record = await advance_review_from_current_stage(record)
    context.user_data[ACTIVE_REVIEW_WORKFLOW_ID_KEY] = record.id
    context.user_data.pop(ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY, None)

    if stage == ReviewStage.WEEKLY_PLAN:
        # Weekly-plan confirmation hands off from markdown artifact review into
        # normal proposal-thread confirmation. Proposal flow owns the item queue.
        proposals_sent = await send_weekly_review_scheduling_proposals(
            context,
            chat_id,
            record,
        )
        if not proposals_sent:
            record = await complete_review_after_event_feedback(
                record.id,
                has_pending_weekly_state_feedback=False,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text="No calendar events were proposed from this accepted weekly plan.",
            )
            if record is not None and record.workflow_status == ReviewWorkflowStatus.COMPLETED:
                context.user_data.pop(ACTIVE_REVIEW_WORKFLOW_ID_KEY, None)
        return

    await send_review_stage_gate(context, chat_id, record)


async def send_retryable_artifact_write_notice(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> bool:
    """
    Surfaces one confirmed artifact write that still needs explicit retry.

    Startup reconciliation may recover writes that were already confirmed but
    interrupted before completion. We keep this operational state out of model
    context and show one retry at a time so the user can resolve it deliberately.
    """
    retry_state = context.user_data.get(ACTIVE_ARTIFACT_WRITE_RETRY_KEY)
    write_id = retry_state.get("write_id") if isinstance(retry_state, dict) else None

    if not write_id:
        retryable_writes = list_retryable_artifact_writes_sync()
        if not retryable_writes:
            return False

        retryable_writes.sort(key=lambda write: write.created_at)
        write = retryable_writes[0]
        write_id = write.id
        context.user_data[ACTIVE_ARTIFACT_WRITE_RETRY_KEY] = {
            "write_id": write.id,
            "review_id": write.source_id,
            "stage": write.source_stage,
        }

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "⚠️ *A confirmed context update still needs to be applied.*\n\n"
            "Please retry this write before we continue, so downstream review "
            "steps do not use stale context."
        ),
        reply_markup=build_artifact_write_retry_keyboard(write_id),
        parse_mode="Markdown",
    )
    return True


async def revise_active_review_stage(
    context: ContextTypes.DEFAULT_TYPE,
    review_id: str,
    stage: ReviewStage,
    feedback: str,
) -> object | None:
    """
    Revises the active Sunday review stage from user feedback.

    The review manager owns stage-specific regeneration. This bot-layer helper
    only keeps the Telegram confirmation pointer active so the revised stage can
    be shown again and iterated until the user confirms it.
    """
    record = await load_review_workflow(review_id)
    if record is None:
        return None

    revised_record = await revise_review_stage(
        record,
        stage=stage,
        feedback=feedback,
    )
    context.user_data[ACTIVE_REVIEW_STAGE_CONFIRMATION_KEY] = {
        "review_id": revised_record.id,
        "stage": stage.value,
    }
    return revised_record
