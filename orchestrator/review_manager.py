import asyncio
import uuid
from datetime import datetime, timezone
from string import Template
from typing import Optional, Type, TypeVar
from google import genai
from loguru import logger
from pydantic import BaseModel
from telegram.ext import ContextTypes

from observability.sentry import capture_exception as capture_sentry_exception
from orchestrator.context_builder import build_context
from integrations.calendar import get_past_events
from persistence.database import get_db
from persistence.models import (
    ArtifactChangeSummary,
    ReviewStage,
    ReviewWorkflowRecord,
    ReviewWorkflowStatus,
    SourceSnapshot,
    StageCheckpoint,
    StageStatus,
)
from persistence.review_workflows import (
    load_resumable_review_workflows_sync,
    load_review_workflow_sync,
    save_review_workflow_sync,
)
from reasoning.parser import parse_model_response
from reasoning.pro_client import generate_sunday_review, SundayReviewResponse
from reasoning.schemas import GoalsAuditResponse, MemoryAuditResponse, WeekReviewResponse, WeeklyPlanResponse
from runtime_paths import get_context_dir, get_prompt_path


GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
GEMINI_PRO_MODEL = "gemini-3-pro-preview"
REVIEW_SYSTEM_INSTRUCTION = (
    "You are a precise structured reasoning engine for David's weekly review workflow. "
    "Follow the stage prompt and populate the response schema faithfully."
)
StructuredResponseT = TypeVar("StructuredResponseT", bound=BaseModel)

_REVIEW_STAGE_FIELD_BY_STAGE = {
    ReviewStage.WEEK_REVIEW: "week_review",
    ReviewStage.GOALS_AUDIT: "goals_audit",
    ReviewStage.MEMORY_AUDIT: "memory_audit",
    ReviewStage.WEEKLY_PLAN: "weekly_plan",
    ReviewStage.SCHEDULING_PASS: "scheduling_pass",
    ReviewStage.FINAL_REVIEW: "final_review",
}

_WEEKLY_STATE_REQUIRED_MARKERS = (
    "# Weekly State",
    "## This Week",
    "### Top Priorities",
    "### Carryover",
    "### Constraints",
    "### Execution Focus",
)
_WEEKLY_STATE_FORBIDDEN_MARKERS = (
    "# Goals",
    "# Decision Log",
    "## Current Rolling Context",
    "## Recent Decisions",
)


def _utc_now_iso() -> str:
    """Returns a timezone-aware UTC timestamp for durable workflow records."""
    return datetime.now(timezone.utc).isoformat()


def _read_context_markdown(filename: str) -> str:
    """
    Reads one context markdown file from disk.

    Missing files are treated as empty strings so a review can still be
    initialized during partial migrations or early setup.
    """
    path = get_context_dir() / filename
    if not path.exists():
        logger.warning("Context file {} was missing while building a review snapshot.", filename)
        return ""
    return path.read_text(encoding="utf-8").strip()


def _format_past_event_lines(past_events_raw: list[dict]) -> list[str]:
    """
    Normalizes past-week calendar events into compact durable strings.

    The review snapshot stores these lines rather than raw event payloads so
    later stages can reason from a stable, compact baseline across restarts.
    """
    lines: list[str] = []

    for event in past_events_raw:
        # Keep each event compact and human-readable because the snapshot is
        # meant to anchor review reasoning, not preserve the full API payload.
        start_time = event["start"].get("dateTime", event["start"].get("date"))
        summary = event.get("summary", "Busy / No Title")
        lines.append(f"[{start_time}] {summary}")

    return lines


def _format_snapshot_events_for_prompt(event_lines: list[str]) -> str:
    """Formats frozen snapshot events for prompt injection."""
    if not event_lines:
        return "No events found in the past week."
    return "\n".join(f"- {line}" for line in event_lines)


def _format_checkpoint_for_prompt(checkpoint: StageCheckpoint) -> str:
    """Formats a persisted checkpoint for use as compact downstream context."""
    lines = [f"Summary: {checkpoint.summary}"]
    if checkpoint.key_findings:
        lines.append("Key findings:")
        lines.extend(f"- {finding}" for finding in checkpoint.key_findings)
    if checkpoint.constraints:
        lines.append("Constraints:")
        lines.extend(f"- {constraint}" for constraint in checkpoint.constraints)
    if checkpoint.carry_forward:
        lines.append("Carry forward:")
        lines.extend(f"- {item}" for item in checkpoint.carry_forward)
    return "\n".join(lines)


def _render_review_prompt(filename: str, **values: str) -> str:
    """Renders a Sunday-review stage prompt template with explicit values."""
    path = get_prompt_path(filename)
    template = Template(path.read_text(encoding="utf-8"))
    return template.safe_substitute(**values)


def _render_week_review_prompt(snapshot: SourceSnapshot) -> str:
    """
    Renders the week-review stage prompt from the frozen source snapshot.

    The snapshot, not live files, anchors this stage so restarts and retries
    reason from the same baseline instead of drifting across workflow turns.
    """
    return _render_review_prompt(
        "week_review.txt",
        goals_markdown=snapshot.goals_markdown,
        weekly_state_markdown=snapshot.weekly_state_markdown,
        decision_log_markdown=snapshot.decision_log_markdown,
        past_events_block=_format_snapshot_events_for_prompt(snapshot.past_week_events),
    )


def _render_goals_audit_prompt(record: ReviewWorkflowRecord) -> str:
    """
    Renders the goals-audit stage prompt from the frozen snapshot and week review.

    The goals audit consumes the week-review checkpoint as evidence, rather
    than independently re-summarizing the week from scratch.
    """
    if record.week_review is None:
        raise ValueError("Cannot run goals_audit before week_review is checkpointed.")

    return _render_review_prompt(
        "goals_audit.txt",
        goals_markdown=record.source_snapshot.goals_markdown,
        week_review_checkpoint=_format_checkpoint_for_prompt(record.week_review),
        weekly_state_markdown=record.source_snapshot.weekly_state_markdown,
        decision_log_markdown=record.source_snapshot.decision_log_markdown,
    )


def _render_memory_audit_prompt(record: ReviewWorkflowRecord) -> str:
    """
    Renders the memory-audit stage prompt from prior checkpoints and memory.

    The memory audit uses week-review evidence and goals-audit interpretation
    to assess rolling memory quality without rewriting the decision log yet.
    """
    if record.week_review is None:
        raise ValueError("Cannot run memory_audit before week_review is checkpointed.")
    if record.goals_audit is None:
        raise ValueError("Cannot run memory_audit before goals_audit is checkpointed.")

    return _render_review_prompt(
        "memory_audit.txt",
        decision_log_markdown=record.source_snapshot.decision_log_markdown,
        week_review_checkpoint=_format_checkpoint_for_prompt(record.week_review),
        goals_audit_checkpoint=_format_checkpoint_for_prompt(record.goals_audit),
        weekly_state_markdown=record.source_snapshot.weekly_state_markdown,
    )


def _render_weekly_plan_prompt(record: ReviewWorkflowRecord) -> str:
    """
    Renders the weekly-plan prompt from the frozen snapshot and checkpoints.

    This stage turns review evidence into a proposed `weekly_state.md` artifact,
    so it depends on the three upstream checkpoints being durable first.
    """
    if record.week_review is None:
        raise ValueError("Cannot run weekly_plan before week_review is checkpointed.")
    if record.goals_audit is None:
        raise ValueError("Cannot run weekly_plan before goals_audit is checkpointed.")
    if record.memory_audit is None:
        raise ValueError("Cannot run weekly_plan before memory_audit is checkpointed.")

    return _render_review_prompt(
        "weekly_plan.txt",
        weekly_state_markdown=record.source_snapshot.weekly_state_markdown,
        goals_markdown=record.source_snapshot.goals_markdown,
        week_review_checkpoint=_format_checkpoint_for_prompt(record.week_review),
        goals_audit_checkpoint=_format_checkpoint_for_prompt(record.goals_audit),
        memory_audit_checkpoint=_format_checkpoint_for_prompt(record.memory_audit),
        decision_log_markdown=record.source_snapshot.decision_log_markdown,
    )


def validate_weekly_state_markdown(content: str) -> None:
    """
    Performs deterministic sanity checks before persisting proposed weekly state.

    The schema captures the model contract; this validator catches artifact-level
    mistakes like empty markdown, missing weekly-state shape, or cross-file
    leakage before the proposal reaches user confirmation.
    """
    stripped = content.strip()
    if not stripped:
        raise ValueError("Weekly state markdown cannot be empty.")

    missing_markers = [
        marker for marker in _WEEKLY_STATE_REQUIRED_MARKERS
        if marker not in stripped
    ]
    if missing_markers:
        raise ValueError(
            "Weekly state markdown is missing required section(s): "
            + ", ".join(missing_markers)
        )

    forbidden_markers = [
        marker for marker in _WEEKLY_STATE_FORBIDDEN_MARKERS
        if marker in stripped
    ]
    if forbidden_markers:
        raise ValueError(
            "Weekly state markdown appears to include another artifact: "
            + ", ".join(forbidden_markers)
        )


def _select_review_model(
    stage: ReviewStage,
    *,
    context_chars: int,
    retry_count: int = 0,
) -> str:
    """
    Chooses the review model for a stage using a small, explicit heuristic.

    Flash is the default because stage prompts are narrow. Pro is reserved for
    retries or larger synthesis-heavy inputs where the extra reasoning budget
    is more likely to matter.
    """
    if retry_count > 0:
        return GEMINI_PRO_MODEL

    if stage == ReviewStage.WEEK_REVIEW and context_chars > 20_000:
        return GEMINI_PRO_MODEL

    if stage in {ReviewStage.MEMORY_AUDIT, ReviewStage.WEEKLY_PLAN} and context_chars > 12_000:
        return GEMINI_PRO_MODEL

    return GEMINI_FLASH_MODEL


def _generate_review_structured(
    *,
    prompt: str,
    response_schema: Type[StructuredResponseT],
    model: str,
    operation: str,
    system_instruction: str = REVIEW_SYSTEM_INSTRUCTION,
) -> StructuredResponseT:
    """
    Sends one rendered review-stage prompt to Gemini and validates the schema.

    This helper is intentionally local to the review manager for now: the model
    choice and prompt composition are review-workflow concerns, not generic
    reasoning-client behavior yet.
    """
    logger.info("Sending {} review-stage request to {}.", operation, model)
    client = genai.Client()
    config = {
        "response_mime_type": "application/json",
        "response_schema": response_schema,
        "temperature": 1.0,
    }
    if system_instruction:
        config["system_instruction"] = system_instruction

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return parse_model_response(response, response_schema)
    except Exception as error:
        logger.error("{} review-stage request failed: {}", operation, error)
        capture_sentry_exception(
            error,
            component="review_manager",
            operation=operation,
            message="Gemini review-stage structured generation failed.",
            tags={"model": model},
        )
        raise


def _response_to_stage_checkpoint(response: BaseModel) -> StageCheckpoint:
    """
    Maps a checkpoint-shaped LLM response into the durable checkpoint shape.

    Stage-specific schemas may evolve independently, but any stage that exposes
    these four fields can persist through the same compact ReviewWorkflowRecord
    checkpoint model.
    """
    return StageCheckpoint(
        summary=getattr(response, "summary"),
        key_findings=getattr(response, "key_findings"),
        constraints=getattr(response, "constraints"),
        carry_forward=getattr(response, "carry_forward"),
    )


async def _run_checkpoint_stage(
    *,
    record: ReviewWorkflowRecord,
    stage: ReviewStage,
    prompt: str,
    response_schema: Type[StructuredResponseT],
    operation: str,
) -> ReviewWorkflowRecord:
    """
    Runs one checkpoint-shaped review stage and persists its durable output.

    Most review stages share the same operational pattern: choose a model,
    request structured output, retry with Pro when Flash fails, then store the
    parsed response in the stage-specific checkpoint field.
    """
    model = _select_review_model(stage, context_chars=len(prompt))

    try:
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=response_schema,
            model=model,
            operation=operation,
        )
    except Exception:
        if model == GEMINI_PRO_MODEL:
            raise

        retry_operation = f"{operation}_retry"
        logger.warning("Retrying {} with {} after {} failed.", operation, GEMINI_PRO_MODEL, model)
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=response_schema,
            model=GEMINI_PRO_MODEL,
            operation=retry_operation,
        )

    return await save_stage_checkpoint(
        record,
        stage,
        _response_to_stage_checkpoint(response),
    )


async def build_review_source_snapshot() -> SourceSnapshot:
    """
    Builds the frozen source snapshot for a Sunday review workflow.

    The snapshot is captured once at review creation time so the multi-stage
    review can recover from process failures without re-reading a drifting
    working set from disk on every resume.
    """
    try:
        goals_markdown, weekly_state_markdown, decision_log_markdown, past_events_raw = await asyncio.gather(
            asyncio.to_thread(_read_context_markdown, "goals.md"),
            asyncio.to_thread(_read_context_markdown, "weekly_state.md"),
            asyncio.to_thread(_read_context_markdown, "decision_log.md"),
            asyncio.to_thread(get_past_events, days=7),
        )

        return SourceSnapshot(
            goals_markdown=goals_markdown,
            weekly_state_markdown=weekly_state_markdown,
            decision_log_markdown=decision_log_markdown,
            past_week_events=_format_past_event_lines(past_events_raw or []),
        )
    except Exception as error:
        capture_sentry_exception(
            error,
            component="review_manager",
            operation="build_review_source_snapshot",
            message="Failed to build the Sunday review source snapshot.",
        )
        raise


async def save_review_workflow(record: ReviewWorkflowRecord) -> ReviewWorkflowRecord:
    """
    Saves the full review workflow snapshot after a meaningful state transition.

    The entire record is rewritten on each checkpoint so the persisted state is
    always self-contained and resumable after a restart.
    """
    try:
        record.updated_at = _utc_now_iso()
        await asyncio.to_thread(save_review_workflow_sync, record)
        return record
    except Exception as error:
        capture_sentry_exception(
            error,
            component="review_manager",
            operation="save_review_workflow",
            message="Failed to persist Sunday review workflow state.",
            tags={
                "review_id": record.id,
                "current_stage": record.current_stage.value,
                "stage_status": record.stage_status.value,
            },
        )
        raise


async def load_review_workflow(review_id: str) -> Optional[ReviewWorkflowRecord]:
    """Loads a review workflow asynchronously from SQLite."""
    try:
        return await asyncio.to_thread(load_review_workflow_sync, review_id)
    except Exception as error:
        capture_sentry_exception(
            error,
            component="review_manager",
            operation="load_review_workflow",
            message="Failed to load Sunday review workflow state.",
            tags={"review_id": review_id},
        )
        raise


async def create_review_workflow(snapshot: Optional[SourceSnapshot] = None) -> ReviewWorkflowRecord:
    """
    Creates and persists a new Sunday review workflow record.

    Callers may provide a prebuilt snapshot for testing or for future staged
    orchestration; otherwise the snapshot is built from the current durable
    markdown artifacts and past-week calendar history.
    """
    try:
        timestamp = _utc_now_iso()
        review_snapshot = snapshot or await build_review_source_snapshot()
        record = ReviewWorkflowRecord(
            id=f"review_{uuid.uuid4().hex[:8]}",
            created_at=timestamp,
            updated_at=timestamp,
            source_snapshot=review_snapshot,
        )
        return await save_review_workflow(record)
    except Exception as error:
        capture_sentry_exception(
            error,
            component="review_manager",
            operation="create_review_workflow",
            message="Failed to create a new Sunday review workflow.",
        )
        raise


async def _update_review_workflow_state(
    record: ReviewWorkflowRecord,
    *,
    workflow_status: Optional[ReviewWorkflowStatus] = None,
    current_stage: Optional[ReviewStage] = None,
    stage_status: Optional[StageStatus] = None,
    last_completed_stage: Optional[ReviewStage] = None,
) -> ReviewWorkflowRecord:
    """
    Applies explicit workflow-state field updates and persists the record.

    This is the low-level mutation primitive. It intentionally avoids transition
    policy so higher-level helpers can make stage semantics explicit.
    """
    if workflow_status is not None:
        record.workflow_status = workflow_status
    if current_stage is not None:
        record.current_stage = current_stage
    if stage_status is not None:
        record.stage_status = stage_status
    if last_completed_stage is not None:
        record.last_completed_stage = last_completed_stage

    return await save_review_workflow(record)


async def transition_review_stage(
    record: ReviewWorkflowRecord,
    *,
    stage: ReviewStage,
    stage_status: StageStatus,
    workflow_status: Optional[ReviewWorkflowStatus] = None,
    last_completed_stage: Optional[ReviewStage] = None,
) -> ReviewWorkflowRecord:
    """
    Moves the workflow into a target stage/status using normal review semantics.

    ReviewWorkflowStatus is the outer lifecycle, ReviewStage is the pipeline
    location, and StageStatus is the local state inside that stage. `stage` is
    the target stage the workflow should enter or remain in after this
    transition.
    """
    if workflow_status is None:
        if stage_status == StageStatus.AWAITING_FEEDBACK:
            workflow_status = ReviewWorkflowStatus.AWAITING_FEEDBACK
        elif stage_status in {
            StageStatus.NOT_STARTED,
            StageStatus.RUNNING,
            StageStatus.IN_REVISION,
        }:
            workflow_status = ReviewWorkflowStatus.ACTIVE
        else:
            raise ValueError(
                "workflow_status is required when completing a review stage."
            )

    return await _update_review_workflow_state(
        record,
        workflow_status=workflow_status,
        current_stage=stage,
        stage_status=stage_status,
        last_completed_stage=last_completed_stage,
    )


async def save_stage_checkpoint(
    record: ReviewWorkflowRecord,
    stage: ReviewStage,
    checkpoint: StageCheckpoint,
) -> ReviewWorkflowRecord:
    """
    Stores the distilled result for one review stage and persists the workflow.

    The stage-to-field mapping is centralized here so later orchestration code
    can checkpoint stages without scattering attribute-name logic.
    """
    setattr(record, _REVIEW_STAGE_FIELD_BY_STAGE[stage], checkpoint)
    record.last_completed_stage = stage
    return await save_review_workflow(record)


async def run_week_review_stage(record: ReviewWorkflowRecord) -> ReviewWorkflowRecord:
    """
    Runs and checkpoints the week-review stage from the frozen source snapshot.

    This is the first real staged review step. It persists a compact
    StageCheckpoint before the current bridge continues into the older one-shot
    Sunday review flow.
    """
    prompt = _render_week_review_prompt(record.source_snapshot)

    return await _run_checkpoint_stage(
        record=record,
        stage=ReviewStage.WEEK_REVIEW,
        prompt=prompt,
        response_schema=WeekReviewResponse,
        operation="week_review",
    )


async def run_goals_audit_stage(record: ReviewWorkflowRecord) -> ReviewWorkflowRecord:
    """
    Runs and checkpoints the goals-audit stage using the completed week review.

    This stage evaluates durable goals and principles without rewriting them;
    later workflow steps can use its checkpoint when planning or asking for
    explicit confirmation.
    """
    prompt = _render_goals_audit_prompt(record)

    return await _run_checkpoint_stage(
        record=record,
        stage=ReviewStage.GOALS_AUDIT,
        prompt=prompt,
        response_schema=GoalsAuditResponse,
        operation="goals_audit",
    )


async def run_memory_audit_stage(record: ReviewWorkflowRecord) -> ReviewWorkflowRecord:
    """
    Runs and checkpoints the memory-audit stage using prior review evidence.

    This stage surfaces memory quality and compaction signals while leaving
    concrete decision-log edits for a later user-facing artifact step.
    """
    prompt = _render_memory_audit_prompt(record)

    return await _run_checkpoint_stage(
        record=record,
        stage=ReviewStage.MEMORY_AUDIT,
        prompt=prompt,
        response_schema=MemoryAuditResponse,
        operation="memory_audit",
    )


async def run_weekly_plan_stage(record: ReviewWorkflowRecord) -> ReviewWorkflowRecord:
    """
    Runs the weekly-plan stage and stores the proposed weekly-state artifact.

    Unlike checkpoint-only stages, this preserves both a compact checkpoint and
    the full proposed markdown that can later be shown to the user or written
    after confirmation.
    """
    prompt = _render_weekly_plan_prompt(record)
    model = _select_review_model(ReviewStage.WEEKLY_PLAN, context_chars=len(prompt))

    try:
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=WeeklyPlanResponse,
            model=model,
            operation="weekly_plan",
        )
    except Exception:
        if model == GEMINI_PRO_MODEL:
            raise

        logger.warning("Retrying weekly_plan with {} after {} failed.", GEMINI_PRO_MODEL, model)
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=WeeklyPlanResponse,
            model=GEMINI_PRO_MODEL,
            operation="weekly_plan_retry",
        )

    validate_weekly_state_markdown(response.weekly_state_content)

    record.weekly_plan = _response_to_stage_checkpoint(response)
    record.weekly_state_changes = ArtifactChangeSummary(
        modifications=[response.state_change_summary] if response.state_change_summary else [],
        proposed_markdown=response.weekly_state_content,
    )
    record.last_completed_stage = ReviewStage.WEEKLY_PLAN
    return await save_review_workflow(record)


async def reconcile_review_workflows() -> list[ReviewWorkflowRecord]:
    """
    Returns persisted reviews that should survive startup reconciliation.

    This does not attempt to auto-run the next stage yet; it simply restores
    the durable records that must not be treated like disposable chat sessions.
    """
    try:
        records = await asyncio.to_thread(load_resumable_review_workflows_sync)
        if not records:
            logger.info("No active Sunday review workflows found during startup reconciliation.")
            return []

        logger.info(
            "Restored {} resumable Sunday review workflow(s) from persistence.",
            len(records),
        )
        return records
    except Exception as error:
        capture_sentry_exception(
            error,
            component="review_manager",
            operation="reconcile_review_workflows",
            message="Failed to reconcile persisted Sunday review workflows at startup.",
        )
        raise


async def start_weekly_review_workflow(
    tg_context: ContextTypes.DEFAULT_TYPE | None = None,
) -> tuple[ReviewWorkflowRecord, SundayReviewResponse]:
    """
    Starts the current Sunday review flow behind one orchestration boundary.

    This is the bridge away from handler-owned workflow logic: the Telegram
    layer should only ask to start the review, while this function handles
    durable workflow creation, state transitions, checkpointing, and the
    temporary one-shot review call that still powers the analysis today.
    """
    review_workflow: ReviewWorkflowRecord | None = None

    try:
        review_workflow = await create_review_workflow()
        review_workflow = await transition_review_stage(
            review_workflow,
            stage=ReviewStage.WEEK_REVIEW,
            stage_status=StageStatus.RUNNING,
        )
        review_workflow = await run_week_review_stage(review_workflow)
        review_workflow = await transition_review_stage(
            review_workflow,
            stage=ReviewStage.GOALS_AUDIT,
            stage_status=StageStatus.RUNNING,
        )
        review_workflow = await run_goals_audit_stage(review_workflow)
        review_workflow = await transition_review_stage(
            review_workflow,
            stage=ReviewStage.MEMORY_AUDIT,
            stage_status=StageStatus.RUNNING,
        )
        review_workflow = await run_memory_audit_stage(review_workflow)
        review_workflow = await transition_review_stage(
            review_workflow,
            stage=ReviewStage.WEEKLY_PLAN,
            stage_status=StageStatus.RUNNING,
        )
        review_workflow = await run_weekly_plan_stage(review_workflow)

        # Bridge: the downstream review still uses the legacy one-shot call
        # for user-facing confirmation and scheduling until those handlers
        # consume staged artifacts directly.
        review_workflow = await transition_review_stage(
            review_workflow,
            stage=ReviewStage.FINAL_REVIEW,
            stage_status=StageStatus.RUNNING,
        )
        review = await asyncio.to_thread(run_sunday_review, tg_context)

        carry_forward = ["Weekly state proposal is awaiting confirmation."]
        if review.proposed_events:
            carry_forward.append(
                f"{len(review.proposed_events)} calendar proposal(s) are queued for confirmation."
            )

        review_workflow = await save_stage_checkpoint(
            review_workflow,
            ReviewStage.FINAL_REVIEW,
            StageCheckpoint(
                summary=review.message,
                key_findings=[review.state_change_summary] if review.state_change_summary else [],
                carry_forward=carry_forward,
            ),
        )
        review_workflow = await transition_review_stage(
            review_workflow,
            workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
            stage=ReviewStage.FINAL_REVIEW,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

        return review_workflow, review
    except Exception as error:
        if review_workflow is not None:
            try:
                await _update_review_workflow_state(
                    review_workflow,
                    workflow_status=ReviewWorkflowStatus.FAILED,
                )
            except Exception as update_error:
                capture_sentry_exception(
                    update_error,
                    component="review_manager",
                    operation="start_weekly_review_workflow_mark_failed",
                    message="Failed to mark a Sunday review workflow as failed after start-up review execution broke.",
                    tags={"review_id": review_workflow.id},
                )

        capture_sentry_exception(
            error,
            component="review_manager",
            operation="start_weekly_review_workflow",
            message="Failed to start the Sunday review workflow.",
            tags={"review_id": review_workflow.id if review_workflow is not None else "unknown"},
        )
        raise


async def apply_bridge_weekly_state_feedback(
    review_id: str,
    *,
    accepted: bool,
    proposal_expired: bool = False,
    has_pending_event_feedback: bool = False,
) -> Optional[ReviewWorkflowRecord]:
    """
    Applies the current bridge-era weekly-state feedback to the review workflow.

    Weekly-state confirmation is only one feedback surface inside the Sunday
    review. The workflow should remain open if the user rejected the state,
    the proposal expired, or weekly-review event confirmations are still
    outstanding.
    """
    record = await load_review_workflow(review_id)
    if record is None:
        return None

    if proposal_expired or not accepted:
        return await transition_review_stage(
            record,
            stage=ReviewStage.FINAL_REVIEW,
            stage_status=StageStatus.IN_REVISION,
        )

    if has_pending_event_feedback:
        return await transition_review_stage(
            record,
            stage=ReviewStage.FINAL_REVIEW,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

    return await transition_review_stage(
        record,
        workflow_status=ReviewWorkflowStatus.COMPLETED,
        stage=ReviewStage.FINAL_REVIEW,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=ReviewStage.FINAL_REVIEW,
    )


async def apply_bridge_event_feedback(
    review_id: str,
    *,
    has_pending_weekly_state_feedback: bool,
) -> Optional[ReviewWorkflowRecord]:
    """
    Applies the last-event feedback transition for the current Sunday review.

    This helper assumes it is called after the final queued weekly-review
    event proposal has been resolved. If weekly-state feedback is still open,
    the review remains in `AWAITING_FEEDBACK`; otherwise it is complete.
    """
    record = await load_review_workflow(review_id)
    if record is None:
        return None

    if has_pending_weekly_state_feedback:
        return await transition_review_stage(
            record,
            stage=ReviewStage.FINAL_REVIEW,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

    return await transition_review_stage(
        record,
        workflow_status=ReviewWorkflowStatus.COMPLETED,
        stage=ReviewStage.FINAL_REVIEW,
        stage_status=StageStatus.COMPLETED,
        last_completed_stage=ReviewStage.FINAL_REVIEW,
    )


def run_sunday_review(tg_context: ContextTypes.DEFAULT_TYPE = None) -> SundayReviewResponse:
    """
    Generates the Sunday Review analysis by fetching context and past events,
    then calling the reasoning layer. Returns a structured response object.
    This is a pure business logic function with no side effects.
    """
    try:
        context_block = build_context(tg_context)

        # Fetch and format past events
        past_events_raw = get_past_events(days=7)
        if not past_events_raw:
            past_events_block = "No events found in the past week."
        else:
            lines = _format_past_event_lines(past_events_raw)
            past_events_block = "\n".join(f"- {line}" for line in lines)

        review = generate_sunday_review(context_block, past_events_block)
        return review
    except Exception as error:
        logger.error(f"Failed to run Sunday review: {error}")
        capture_sentry_exception(error, component="review_manager", operation="run_sunday_review")
        raise

def execute_weekly_state_update(content: str) -> bool:
    """
    Backs up the current weekly_state.md and overwrites it with new content.
    Returns True on success, False on failure.
    This is a pure file I/O function.
    """
    try:
        context_dir = get_context_dir()
        context_dir.mkdir(parents=True, exist_ok=True)
        weekly_state_path = context_dir / "weekly_state.md"
        
        if weekly_state_path.exists():
            backup_filename = f"weekly_state_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            backup_path = context_dir / backup_filename
            with weekly_state_path.open("r", encoding="utf-8") as src, backup_path.open("w", encoding="utf-8") as dst:
                dst.write(src.read())
            logger.info(f"Backed up weekly state to {backup_filename}")
                
        with weekly_state_path.open("w", encoding="utf-8") as f:
            f.write(content)

        snapshot_id = f"wsnap_{uuid.uuid4().hex[:8]}"
        logger.info(f"Persisting weekly snapshot {snapshot_id}...")
        db = get_db()
        db["weekly_snapshots"].insert({  # type: ignore
            "id": snapshot_id,
            "timestamp": datetime.now().isoformat(),
            "weekly_state_content": content,
        })
        logger.success(f"Persisted weekly snapshot {snapshot_id}.")
        
        logger.success("Successfully updated weekly_state.md")
        return True
    except Exception as e:
        logger.error(f"Failed to execute weekly state update: {e}")
        capture_sentry_exception(e, component="review_manager", operation="execute_weekly_state_update")
        return False
