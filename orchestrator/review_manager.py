import asyncio
import uuid
from datetime import datetime, timezone
from string import Template
from typing import Iterable, Optional, Type, TypeVar
from google import genai
from loguru import logger
from pydantic import BaseModel

from observability.sentry import capture_exception as capture_sentry_exception
from integrations.calendar import get_past_events
from orchestrator.artifact_writes import execute_artifact_replacement
from orchestrator.review_artifacts import get_effective_artifact_content
from persistence.models import (
    ArtifactChangeSummary,
    ArtifactType,
    ReviewStage,
    ReviewWorkflowRecord,
    ReviewWorkflowStatus,
    SchedulingPassArtifact,
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
from reasoning.schemas import (
    DecisionLogChangeProposalResponse,
    GoalsAuditResponse,
    GoalsChangeProposalResponse,
    MemoryAuditResponse,
    SchedulingPassResponse,
    SchedulingProposalResponse,
    WeekReviewResponse,
    WeeklyPlanResponse,
)
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
_REVIEW_STAGE_INVALIDATION_ORDER = [
    ReviewStage.WEEK_REVIEW,
    ReviewStage.GOALS_AUDIT,
    ReviewStage.MEMORY_AUDIT,
    ReviewStage.WEEKLY_PLAN,
    ReviewStage.SCHEDULING_PASS,
    ReviewStage.FINAL_REVIEW,
]

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
_GOALS_REQUIRED_MARKERS = (
    "# Goals",
    "## Long-Term",
    "## Medium-Term",
    "## Operating Principles",
)
_GOALS_FORBIDDEN_MARKERS = (
    "# Weekly State",
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


def _format_artifact_change_for_revision(changes: ArtifactChangeSummary) -> str:
    """
    Formats the latest visible artifact proposal for revision prompts.

    Review-stage feedback usually refers to the proposal the user just saw in
    Telegram. Including only that latest proposal keeps revisions focused and
    avoids dragging older attempts into the model context.
    """
    lines: list[str] = []
    for label, values in (
        ("Additions", changes.additions),
        ("Deletions", changes.deletions),
        ("Modifications", changes.modifications),
    ):
        if values:
            lines.append(f"{label}:")
            lines.extend(f"- {value}" for value in values)

    if changes.proposed_markdown:
        lines.append("")
        lines.append("Full proposed markdown:")
        lines.append(changes.proposed_markdown)

    return "\n".join(lines) if lines else "No concrete artifact changes were proposed."


def _get_review_stage_checkpoint(
    record: ReviewWorkflowRecord,
    stage: ReviewStage,
) -> Optional[StageCheckpoint]:
    """Returns the persisted checkpoint for a review stage, if one exists."""
    return getattr(record, _REVIEW_STAGE_FIELD_BY_STAGE[stage])


def _format_revision_prompt_context(
    record: ReviewWorkflowRecord,
    stage: ReviewStage,
    feedback: str,
) -> str:
    """
    Builds the small revision-only context appended to a normal stage prompt.

    The base prompt still owns the task and schema. This appendix only tells the
    model what the user corrected and what previous output should be revised,
    which keeps revision behavior general without duplicating every stage prompt.
    """
    lines = [
        "",
        "---",
        "Revision context:",
        f"- Stage being revised: {stage.value}",
        f"- User feedback to incorporate: {feedback.strip()}",
    ]

    previous_checkpoint = _get_review_stage_checkpoint(record, stage)
    if previous_checkpoint is not None:
        lines.append("")
        lines.append("Previous stage output to revise:")
        lines.append(_format_checkpoint_for_prompt(previous_checkpoint))

    if stage == ReviewStage.GOALS_AUDIT and record.goals_changes:
        lines.append("")
        lines.append("Latest visible goals proposal to revise:")
        lines.append(_format_artifact_change_for_revision(record.goals_changes))

    if stage == ReviewStage.MEMORY_AUDIT and record.decision_log_changes:
        lines.append("")
        lines.append("Latest visible decision-log proposal to revise:")
        lines.append(_format_artifact_change_for_revision(record.decision_log_changes))

    if stage == ReviewStage.WEEKLY_PLAN and record.weekly_state_changes:
        lines.append("")
        lines.append("Latest visible weekly-state proposal to revise:")
        lines.append(_format_artifact_change_for_revision(record.weekly_state_changes))

    if stage == ReviewStage.SCHEDULING_PASS and record.scheduling_proposals:
        lines.append("")
        lines.append("Previous scheduling rationale:")
        lines.append(record.scheduling_proposals.scheduling_rationale or "No rationale recorded.")
        if record.scheduling_proposals.proposed_events:
            lines.append("")
            lines.append("Previous scheduling proposal summaries:")
            for event in record.scheduling_proposals.proposed_events:
                summary = str(event.get("summary", "Untitled proposal"))
                start_time = str(event.get("start_time", "unknown start"))
                end_time = str(event.get("end_time", "unknown end"))
                lines.append(f"- {summary}: {start_time} to {end_time}")

    lines.append("")
    lines.append(
        "Revise this stage only. Use the latest feedback as authoritative and do not infer extra changes."
    )
    return "\n".join(lines)


def _with_revision_context(
    prompt: str,
    *,
    record: ReviewWorkflowRecord,
    stage: ReviewStage,
    revision_feedback: Optional[str],
) -> str:
    """Appends revision instructions only when a stage is being regenerated."""
    if not revision_feedback:
        return prompt
    return prompt + _format_revision_prompt_context(record, stage, revision_feedback)


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


def _render_goals_change_prompt(record: ReviewWorkflowRecord) -> str:
    """
    Renders the optional goals-change proposal pass from the audit checkpoint.

    The audit pass decides whether goals need attention. This second pass is
    only responsible for proposing concrete markdown when the evidence supports
    a durable change.
    """
    if record.week_review is None:
        raise ValueError("Cannot propose goals changes before week_review is checkpointed.")
    if record.goals_audit is None:
        raise ValueError("Cannot propose goals changes before goals_audit is checkpointed.")

    return _render_review_prompt(
        "goals_change.txt",
        goals_markdown=record.source_snapshot.goals_markdown,
        week_review_checkpoint=_format_checkpoint_for_prompt(record.week_review),
        goals_audit_checkpoint=_format_checkpoint_for_prompt(record.goals_audit),
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


def _render_decision_log_change_prompt(record: ReviewWorkflowRecord) -> str:
    """
    Renders the second memory-audit pass for deterministic decision-log changes.

    The first memory-audit pass must already be checkpointed. This proposal pass
    receives that checkpoint and outputs exact operations that application code
    can validate and materialize into proposed markdown.
    """
    if record.memory_audit is None:
        raise ValueError("Cannot propose decision_log changes before memory_audit is checkpointed.")

    return _render_review_prompt(
        "decision_log_change.txt",
        decision_log_markdown=record.source_snapshot.decision_log_markdown,
        memory_audit_checkpoint=_format_checkpoint_for_prompt(record.memory_audit),
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
        goals_markdown=get_effective_artifact_content(record, ArtifactType.GOALS),
        week_review_checkpoint=_format_checkpoint_for_prompt(record.week_review),
        goals_audit_checkpoint=_format_checkpoint_for_prompt(record.goals_audit),
        memory_audit_checkpoint=_format_checkpoint_for_prompt(record.memory_audit),
        decision_log_markdown=get_effective_artifact_content(record, ArtifactType.DECISION_LOG),
    )


def _render_scheduling_pass_prompt(record: ReviewWorkflowRecord) -> str:
    """
    Renders the scheduling-pass prompt from completed review checkpoints.

    This stage decides scheduling intention from the reviewed weekly plan. It
    deliberately does not generate concrete events until the user confirms the
    direction.
    """
    if record.week_review is None:
        raise ValueError("Cannot run scheduling_pass before week_review is checkpointed.")
    if record.goals_audit is None:
        raise ValueError("Cannot run scheduling_pass before goals_audit is checkpointed.")
    if record.memory_audit is None:
        raise ValueError("Cannot run scheduling_pass before memory_audit is checkpointed.")
    if record.weekly_plan is None:
        raise ValueError("Cannot run scheduling_pass before weekly_plan is checkpointed.")
    if record.weekly_state_changes is None or not record.weekly_state_changes.proposed_markdown:
        raise ValueError("Cannot run scheduling_pass before proposed weekly state is available.")

    upcoming_events_block = _format_snapshot_events_for_prompt(record.source_snapshot.upcoming_events)

    return _render_review_prompt(
        "scheduling_pass.txt",
        weekly_state_markdown=get_effective_artifact_content(record, ArtifactType.WEEKLY_STATE),
        proposed_weekly_state_markdown=record.weekly_state_changes.proposed_markdown,
        goals_markdown=get_effective_artifact_content(record, ArtifactType.GOALS),
        decision_log_markdown=get_effective_artifact_content(record, ArtifactType.DECISION_LOG),
        week_review_checkpoint=_format_checkpoint_for_prompt(record.week_review),
        goals_audit_checkpoint=_format_checkpoint_for_prompt(record.goals_audit),
        memory_audit_checkpoint=_format_checkpoint_for_prompt(record.memory_audit),
        weekly_plan_checkpoint=_format_checkpoint_for_prompt(record.weekly_plan),
        past_events_block=_format_snapshot_events_for_prompt(record.source_snapshot.past_week_events),
        upcoming_events_block=upcoming_events_block,
    )


def _render_scheduling_proposals_prompt(record: ReviewWorkflowRecord) -> str:
    """
    Renders concrete calendar proposals from confirmed scheduling intention.

    Keeping this separate from scheduling_pass prevents one model call from
    stating a strategy while generating events that drift away from it.
    """
    if record.scheduling_pass is None:
        raise ValueError("Cannot generate scheduling proposals before scheduling_pass is checkpointed.")

    return _render_review_prompt(
        "scheduling_proposals.txt",
        scheduling_pass_checkpoint=_format_checkpoint_for_prompt(record.scheduling_pass),
        weekly_state_markdown=get_effective_artifact_content(record, ArtifactType.WEEKLY_STATE),
        goals_markdown=get_effective_artifact_content(record, ArtifactType.GOALS),
        decision_log_markdown=get_effective_artifact_content(record, ArtifactType.DECISION_LOG),
        upcoming_events_block=_format_snapshot_events_for_prompt(record.source_snapshot.upcoming_events),
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


def validate_goals_markdown(content: str) -> None:
    """
    Performs deterministic sanity checks before confirming proposed goals.

    Goals are David's durable compass, so the model may propose content, but it
    must preserve the recognizable artifact shape and avoid cross-file leakage.
    """
    stripped = content.strip()
    if not stripped:
        raise ValueError("Goals markdown cannot be empty.")

    missing_markers = [
        marker for marker in _GOALS_REQUIRED_MARKERS
        if marker not in stripped
    ]
    if missing_markers:
        raise ValueError(
            "Goals markdown is missing required section(s): "
            + ", ".join(missing_markers)
        )

    forbidden_markers = [
        marker for marker in _GOALS_FORBIDDEN_MARKERS
        if marker in stripped
    ]
    if forbidden_markers:
        raise ValueError(
            "Goals markdown appears to include another artifact: "
            + ", ".join(forbidden_markers)
        )


def _normalize_decision_log_bullet(value: str) -> str:
    """Normalizes proposed rolling-context entries to complete markdown bullets."""
    stripped = value.strip()
    if not stripped:
        return ""
    return stripped if stripped.startswith("- ") else f"- {stripped}"


def _extract_decision_log_sections(content: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """
    Splits decision_log.md into prefix, rolling-context bullets, recent lines, suffix.

    The deterministic materializer only edits known sections from
    decision_log.example.md. If those section headers are missing, it fails
    instead of making a best-effort edit in the wrong place.
    """
    lines = content.splitlines()
    rolling_header = "## Current Rolling Context"
    recent_header = "## Recent Decisions (Appended Daily)"

    try:
        rolling_index = lines.index(rolling_header)
        recent_index = lines.index(recent_header)
    except ValueError as error:
        raise ValueError("decision_log.md is missing required review section headers.") from error

    if recent_index <= rolling_index:
        raise ValueError("decision_log.md sections are in an unsupported order.")

    prefix = lines[: rolling_index + 1]
    rolling_body = lines[rolling_index + 1:recent_index]
    recent_and_suffix = lines[recent_index:]

    next_section_index = next(
        (
            index
            for index, line in enumerate(recent_and_suffix[1:], start=1)
            if line.startswith("## ")
        ),
        len(recent_and_suffix),
    )
    recent_section = recent_and_suffix[:next_section_index]
    suffix = recent_and_suffix[next_section_index:]
    rolling_bullets = [line.strip() for line in rolling_body if line.strip().startswith("- ")]

    return prefix, rolling_bullets, recent_section, suffix


def _materialize_decision_log_change_proposal(
    decision_log_markdown: str,
    proposal: DecisionLogChangeProposalResponse,
) -> ArtifactChangeSummary:
    """
    Applies proposed decision-log operations into deterministic markdown.

    Deletions and modifications require exact Current Rolling Context bullet
    matches. This keeps the LLM in a proposal role and lets application code
    refuse ambiguous edits instead of silently mutating durable memory.
    """
    prefix, rolling_bullets, recent_section, suffix = _extract_decision_log_sections(
        decision_log_markdown
    )
    updated_bullets = list(rolling_bullets)

    deletions = [
        _normalize_decision_log_bullet(item)
        for item in proposal.proposed_rolling_context_deletions
        if _normalize_decision_log_bullet(item)
    ]
    modifications = {
        _normalize_decision_log_bullet(item.old_bullet): _normalize_decision_log_bullet(item.new_bullet)
        for item in proposal.proposed_rolling_context_modifications
        if _normalize_decision_log_bullet(item.old_bullet)
        and _normalize_decision_log_bullet(item.new_bullet)
    }
    additions = [
        _normalize_decision_log_bullet(item)
        for item in proposal.proposed_rolling_context_additions
        if _normalize_decision_log_bullet(item)
    ]

    for bullet in deletions:
        if bullet not in updated_bullets:
            raise ValueError(f"Decision-log deletion anchor was not found: {bullet}")
        updated_bullets.remove(bullet)

    for old_bullet, new_bullet in modifications.items():
        if old_bullet not in updated_bullets:
            raise ValueError(f"Decision-log modification anchor was not found: {old_bullet}")
        updated_bullets[updated_bullets.index(old_bullet)] = new_bullet

    for bullet in additions:
        if bullet not in updated_bullets:
            updated_bullets.append(bullet)

    if proposal.proposed_recent_decisions_reset:
        recent_section = [
            "## Recent Decisions (Appended Daily)",
            "*(New session notes will be appended here throughout the week.)*",
        ]
    elif proposal.proposed_recent_decisions_carry_forward:
        recent_section = ["## Recent Decisions (Appended Daily)", ""]
        recent_section.extend(
            _normalize_decision_log_bullet(item)
            for item in proposal.proposed_recent_decisions_carry_forward
            if _normalize_decision_log_bullet(item)
        )

    proposed_lines = [*prefix, *updated_bullets, "", *recent_section, *suffix]
    proposed_markdown = "\n".join(proposed_lines).strip() + "\n"

    return ArtifactChangeSummary(
        additions=additions,
        deletions=deletions,
        modifications=[
            f"{old_bullet} -> {new_bullet}"
            for old_bullet, new_bullet in modifications.items()
        ],
        proposed_markdown=proposed_markdown,
    )


def _select_review_model(
    stage: ReviewStage,
    *,
    context_chars: int,
    retry_count: int = 0,
) -> str:
    """
    Chooses the review model for a stage using a small, explicit heuristic.

    Flash is the default for every stage. Pro is reserved only for explicit
    retry paths, which keeps routine Sunday review behavior predictable while
    preserving a higher-capacity fallback for transient structured-output
    failures.
    """
    if retry_count > 0:
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


def _is_non_retryable_review_generation_error(error: Exception) -> bool:
    """
    Identifies request/schema errors that should not be retried on Pro.

    Pro fallback is useful for retryable generation or parsing failures, but an
    unsupported schema/configuration will fail on every model. Treating those as
    terminal keeps Sunday review failures honest and avoids surprise Pro calls.
    """
    current_error: BaseException | None = error
    while current_error is not None:
        message = str(current_error).lower()
        if "additionalproperties is not supported" in message:
            return True

        current_error = current_error.__cause__ or current_error.__context__

    return False


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
    except Exception as error:
        if model == GEMINI_PRO_MODEL or _is_non_retryable_review_generation_error(error):
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


async def run_week_review_stage(
    record: ReviewWorkflowRecord,
    *,
    revision_feedback: Optional[str] = None,
) -> ReviewWorkflowRecord:
    """
    Runs and checkpoints the week-review stage from the frozen source snapshot.

    This is the first staged review step. It persists a compact checkpoint that
    downstream stages use without rereading a drifting weekly context.
    """
    prompt = _with_revision_context(
        _render_week_review_prompt(record.source_snapshot),
        record=record,
        stage=ReviewStage.WEEK_REVIEW,
        revision_feedback=revision_feedback,
    )
    operation = "week_review_revision" if revision_feedback else "week_review"

    return await _run_checkpoint_stage(
        record=record,
        stage=ReviewStage.WEEK_REVIEW,
        prompt=prompt,
        response_schema=WeekReviewResponse,
        operation=operation,
    )


async def run_goals_audit_stage(
    record: ReviewWorkflowRecord,
    *,
    revision_feedback: Optional[str] = None,
) -> ReviewWorkflowRecord:
    """
    Runs and checkpoints the goals-audit stage using the completed week review.

    This stage evaluates durable goals and principles without rewriting them;
    later workflow steps can use its checkpoint when planning or asking for
    explicit confirmation.
    """
    prompt = _with_revision_context(
        _render_goals_audit_prompt(record),
        record=record,
        stage=ReviewStage.GOALS_AUDIT,
        revision_feedback=revision_feedback,
    )
    operation = "goals_audit_revision" if revision_feedback else "goals_audit"

    record = await _run_checkpoint_stage(
        record=record,
        stage=ReviewStage.GOALS_AUDIT,
        prompt=prompt,
        response_schema=GoalsAuditResponse,
        operation=operation,
    )
    proposal_prompt = _with_revision_context(
        _render_goals_change_prompt(record),
        record=record,
        stage=ReviewStage.GOALS_AUDIT,
        revision_feedback=revision_feedback,
    )
    proposal_model = _select_review_model(
        ReviewStage.GOALS_AUDIT,
        context_chars=len(proposal_prompt),
    )
    proposal_operation = "goals_change_revision" if revision_feedback else "goals_change"

    try:
        proposal = await asyncio.to_thread(
            _generate_review_structured,
            prompt=proposal_prompt,
            response_schema=GoalsChangeProposalResponse,
            model=proposal_model,
            operation=proposal_operation,
        )
    except Exception as error:
        if proposal_model == GEMINI_PRO_MODEL or _is_non_retryable_review_generation_error(error):
            raise

        retry_operation = f"{proposal_operation}_retry"
        logger.warning(
            "Retrying {} with {} after {} failed.",
            proposal_operation,
            GEMINI_PRO_MODEL,
            proposal_model,
        )
        proposal = await asyncio.to_thread(
            _generate_review_structured,
            prompt=proposal_prompt,
            response_schema=GoalsChangeProposalResponse,
            model=GEMINI_PRO_MODEL,
            operation=retry_operation,
        )

    proposed_markdown = (
        proposal.proposed_markdown.strip()
        if proposal.proposed_markdown and proposal.proposed_markdown.strip()
        else None
    )
    if proposed_markdown:
        validate_goals_markdown(proposed_markdown)
        record.goals_changes = ArtifactChangeSummary(
            modifications=[proposal.proposed_change_summary]
            if proposal.proposed_change_summary
            else [],
            proposed_markdown=proposed_markdown,
        )
    else:
        record.goals_changes = None

    record.last_completed_stage = ReviewStage.GOALS_AUDIT
    return await save_review_workflow(record)


async def run_memory_audit_stage(
    record: ReviewWorkflowRecord,
    *,
    revision_feedback: Optional[str] = None,
) -> ReviewWorkflowRecord:
    """
    Runs and checkpoints the memory-audit stage using prior review evidence.

    This stage uses two internal passes: first it checkpoints memory-quality
    findings, then it proposes exact decision-log operations from that
    checkpoint and materializes proposed markdown for user confirmation.
    """
    prompt = _with_revision_context(
        _render_memory_audit_prompt(record),
        record=record,
        stage=ReviewStage.MEMORY_AUDIT,
        revision_feedback=revision_feedback,
    )
    model = _select_review_model(ReviewStage.MEMORY_AUDIT, context_chars=len(prompt))
    operation = "memory_audit_revision" if revision_feedback else "memory_audit"

    try:
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=MemoryAuditResponse,
            model=model,
            operation=operation,
        )
    except Exception as error:
        if model == GEMINI_PRO_MODEL or _is_non_retryable_review_generation_error(error):
            raise

        retry_operation = f"{operation}_retry"
        logger.warning("Retrying {} with {} after {} failed.", operation, GEMINI_PRO_MODEL, model)
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=MemoryAuditResponse,
            model=GEMINI_PRO_MODEL,
            operation=retry_operation,
        )

    record.memory_audit = _response_to_stage_checkpoint(response)
    proposal_prompt = _with_revision_context(
        _render_decision_log_change_prompt(record),
        record=record,
        stage=ReviewStage.MEMORY_AUDIT,
        revision_feedback=revision_feedback,
    )
    proposal_model = _select_review_model(
        ReviewStage.MEMORY_AUDIT,
        context_chars=len(proposal_prompt),
    )
    proposal_operation = (
        "decision_log_change_revision"
        if revision_feedback
        else "decision_log_change"
    )

    try:
        proposal = await asyncio.to_thread(
            _generate_review_structured,
            prompt=proposal_prompt,
            response_schema=DecisionLogChangeProposalResponse,
            model=proposal_model,
            operation=proposal_operation,
        )
    except Exception as error:
        if proposal_model == GEMINI_PRO_MODEL or _is_non_retryable_review_generation_error(error):
            raise

        retry_operation = f"{proposal_operation}_retry"
        logger.warning(
            "Retrying {} with {} after {} failed.",
            proposal_operation,
            GEMINI_PRO_MODEL,
            proposal_model,
        )
        proposal = await asyncio.to_thread(
            _generate_review_structured,
            prompt=proposal_prompt,
            response_schema=DecisionLogChangeProposalResponse,
            model=GEMINI_PRO_MODEL,
            operation=retry_operation,
        )

    record.decision_log_changes = _materialize_decision_log_change_proposal(
        record.source_snapshot.decision_log_markdown,
        proposal,
    )
    record.last_completed_stage = ReviewStage.MEMORY_AUDIT
    return await save_review_workflow(record)


async def run_weekly_plan_stage(
    record: ReviewWorkflowRecord,
    *,
    revision_feedback: Optional[str] = None,
) -> ReviewWorkflowRecord:
    """
    Runs the weekly-plan stage and stores the proposed weekly-state artifact.

    Unlike checkpoint-only stages, this preserves both a compact checkpoint and
    the full proposed markdown that can later be shown to the user or written
    after confirmation.
    """
    prompt = _with_revision_context(
        _render_weekly_plan_prompt(record),
        record=record,
        stage=ReviewStage.WEEKLY_PLAN,
        revision_feedback=revision_feedback,
    )
    model = _select_review_model(ReviewStage.WEEKLY_PLAN, context_chars=len(prompt))
    operation = "weekly_plan_revision" if revision_feedback else "weekly_plan"

    try:
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=WeeklyPlanResponse,
            model=model,
            operation=operation,
        )
    except Exception as error:
        if model == GEMINI_PRO_MODEL or _is_non_retryable_review_generation_error(error):
            raise

        retry_operation = f"{operation}_retry"
        logger.warning("Retrying {} with {} after {} failed.", operation, GEMINI_PRO_MODEL, model)
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=WeeklyPlanResponse,
            model=GEMINI_PRO_MODEL,
            operation=retry_operation,
        )

    validate_weekly_state_markdown(response.weekly_state_content)

    record.weekly_plan = _response_to_stage_checkpoint(response)
    record.weekly_state_changes = ArtifactChangeSummary(
        modifications=[response.state_change_summary] if response.state_change_summary else [],
        proposed_markdown=response.weekly_state_content,
    )
    record.last_completed_stage = ReviewStage.WEEKLY_PLAN
    return await save_review_workflow(record)


async def run_scheduling_pass_stage(
    record: ReviewWorkflowRecord,
    *,
    revision_feedback: Optional[str] = None,
) -> ReviewWorkflowRecord:
    """
    Runs and checkpoints scheduling recommendations for the reviewed week.

    This stage stores the compact scheduling-intent checkpoint. Concrete event
    proposals are generated only after the user confirms this direction.
    """
    prompt = _with_revision_context(
        _render_scheduling_pass_prompt(record),
        record=record,
        stage=ReviewStage.SCHEDULING_PASS,
        revision_feedback=revision_feedback,
    )
    model = _select_review_model(ReviewStage.SCHEDULING_PASS, context_chars=len(prompt))
    operation = "scheduling_pass_revision" if revision_feedback else "scheduling_pass"

    try:
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=SchedulingPassResponse,
            model=model,
            operation=operation,
        )
    except Exception as error:
        if model == GEMINI_PRO_MODEL or _is_non_retryable_review_generation_error(error):
            raise

        retry_operation = f"{operation}_retry"
        logger.warning("Retrying {} with {} after {} failed.", operation, GEMINI_PRO_MODEL, model)
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=SchedulingPassResponse,
            model=GEMINI_PRO_MODEL,
            operation=retry_operation,
        )

    record.scheduling_pass = _response_to_stage_checkpoint(response)
    record.scheduling_proposals = None
    record.last_completed_stage = ReviewStage.SCHEDULING_PASS
    return await save_review_workflow(record)


async def generate_scheduling_proposals(
    record: ReviewWorkflowRecord,
) -> ReviewWorkflowRecord:
    """
    Generates concrete calendar proposal candidates from confirmed scheduling intent.

    This runs after the scheduling_pass gate is accepted, so the model's task is
    narrower: instantiate the confirmed direction without re-deciding strategy.
    """
    prompt = _render_scheduling_proposals_prompt(record)
    model = _select_review_model(ReviewStage.SCHEDULING_PASS, context_chars=len(prompt))
    operation = "scheduling_proposals"

    try:
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=SchedulingProposalResponse,
            model=model,
            operation=operation,
        )
    except Exception as error:
        if model == GEMINI_PRO_MODEL or _is_non_retryable_review_generation_error(error):
            raise

        retry_operation = f"{operation}_retry"
        logger.warning("Retrying {} with {} after {} failed.", operation, GEMINI_PRO_MODEL, model)
        response = await asyncio.to_thread(
            _generate_review_structured,
            prompt=prompt,
            response_schema=SchedulingProposalResponse,
            model=GEMINI_PRO_MODEL,
            operation=retry_operation,
        )

    record.scheduling_proposals = SchedulingPassArtifact(
        proposed_events=[
            event.model_dump(mode="json") for event in response.proposed_events
        ],
        scheduling_rationale=response.proposal_rationale,
    )
    return await save_review_workflow(record)


def _clear_outputs_after_stage(record: ReviewWorkflowRecord, stage: ReviewStage) -> None:
    """
    Removes downstream outputs that are no longer trustworthy after a revision.

    A revised early-stage checkpoint can change the meaning of every later
    stage. Clearing those fields before regeneration prevents stale weekly
    plans, scheduling artifacts, or final-review summaries from surviving after
    their evidence changed.
    """
    stage_index = _REVIEW_STAGE_INVALIDATION_ORDER.index(stage)

    for downstream_stage in _REVIEW_STAGE_INVALIDATION_ORDER[stage_index + 1:]:
        setattr(record, _REVIEW_STAGE_FIELD_BY_STAGE[downstream_stage], None)

    if stage_index < _REVIEW_STAGE_INVALIDATION_ORDER.index(ReviewStage.GOALS_AUDIT):
        record.goals_changes = None

    if stage_index < _REVIEW_STAGE_INVALIDATION_ORDER.index(ReviewStage.MEMORY_AUDIT):
        record.decision_log_changes = None

    if stage_index < _REVIEW_STAGE_INVALIDATION_ORDER.index(ReviewStage.WEEKLY_PLAN):
        record.weekly_state_changes = None

    if stage_index <= _REVIEW_STAGE_INVALIDATION_ORDER.index(ReviewStage.SCHEDULING_PASS):
        record.scheduling_proposals = None


async def revise_review_stage(
    record: ReviewWorkflowRecord,
    *,
    stage: ReviewStage,
    feedback: str,
) -> ReviewWorkflowRecord:
    """
    Regenerates one Sunday-review stage from user feedback and prior context.

    The workflow remains inside the same stage: feedback moves it into
    IN_REVISION, the stage runner regenerates the checkpoint/artifact, and the
    result is returned to AWAITING_FEEDBACK for another explicit user gate.
    """
    stripped_feedback = feedback.strip()
    if not stripped_feedback:
        raise ValueError("Review-stage revision feedback cannot be empty.")
    if stage == ReviewStage.FINAL_REVIEW:
        raise ValueError("Final review is assembled deterministically and cannot be LLM-revised.")

    record.feedback_history.append(f"{stage.value}: {stripped_feedback}")
    _clear_outputs_after_stage(record, stage)
    record = await transition_review_stage(
        record,
        stage=stage,
        stage_status=StageStatus.IN_REVISION,
    )

    if stage == ReviewStage.WEEK_REVIEW:
        record = await run_week_review_stage(record, revision_feedback=stripped_feedback)
    elif stage == ReviewStage.GOALS_AUDIT:
        record = await run_goals_audit_stage(record, revision_feedback=stripped_feedback)
    elif stage == ReviewStage.MEMORY_AUDIT:
        record = await run_memory_audit_stage(record, revision_feedback=stripped_feedback)
    elif stage == ReviewStage.WEEKLY_PLAN:
        record = await run_weekly_plan_stage(record, revision_feedback=stripped_feedback)
    elif stage == ReviewStage.SCHEDULING_PASS:
        record = await run_scheduling_pass_stage(record, revision_feedback=stripped_feedback)
    else:
        raise ValueError(f"Unsupported review-stage revision: {stage.value}.")

    return await transition_review_stage(
        record,
        workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
        stage=stage,
        stage_status=StageStatus.AWAITING_FEEDBACK,
    )


def _merge_unique(items: Iterable[str]) -> list[str]:
    """
    Preserves first-seen order while removing repeated review-stage bullets.

    Final review assembly combines several checkpoints, so de-duplicating here
    keeps the user-facing result compact without hiding any new information.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def _completed_review_checkpoints(record: ReviewWorkflowRecord) -> list[tuple[str, StageCheckpoint]]:
    """Returns completed staged checkpoints in the order they should be shown."""
    checkpoints = [
        ("Week Review", record.week_review),
        ("Goals Audit", record.goals_audit),
        ("Memory Audit", record.memory_audit),
        ("Weekly Plan", record.weekly_plan),
        ("Scheduling Pass", record.scheduling_pass),
    ]
    return [
        (label, checkpoint)
        for label, checkpoint in checkpoints
        if checkpoint is not None
    ]


def build_final_review_message(record: ReviewWorkflowRecord) -> str:
    """
    Assembles the Sunday review summary from completed staged checkpoints.

    No model is called here. The final review is a deterministic handoff summary
    that tells the user what the staged review already concluded.
    """
    sections = [
        f"*{label}:* {checkpoint.summary}"
        for label, checkpoint in _completed_review_checkpoints(record)
        if checkpoint.summary
    ]
    if not sections:
        return "The staged Sunday review completed, but no checkpoint summaries were available."
    return "\n\n".join(sections)


def build_final_review_checkpoint(record: ReviewWorkflowRecord) -> StageCheckpoint:
    """
    Creates the final-review checkpoint by assembling prior stage outputs.

    This marks that staged outputs are ready for user confirmation surfaces:
    weekly-state markdown confirmation and calendar proposal-thread review.
    """
    checkpoints = [checkpoint for _, checkpoint in _completed_review_checkpoints(record)]
    carry_forward = ["Confirmed review artifacts have been handled before final review."]
    scheduling_proposal_count = (
        len(record.scheduling_proposals.proposed_events)
        if record.scheduling_proposals
        else 0
    )
    if scheduling_proposal_count:
        carry_forward.append(
            f"{scheduling_proposal_count} scheduling-pass calendar proposal candidate(s) will be handed off for confirmation."
        )

    return StageCheckpoint(
        summary=build_final_review_message(record),
        key_findings=_merge_unique(
            finding
            for checkpoint in checkpoints
            for finding in checkpoint.key_findings
        ),
        constraints=_merge_unique(
            constraint
            for checkpoint in checkpoints
            for constraint in checkpoint.constraints
        ),
        carry_forward=carry_forward,
    )


async def prepare_final_review_stage(record: ReviewWorkflowRecord) -> ReviewWorkflowRecord:
    """
    Assembles the deterministic final-review checkpoint and pauses for confirmation.

    This runs after scheduling intent has been confirmed and concrete proposal
    candidates have been generated, so the final gate summarizes the workflow
    without invoking another model pass.
    """
    record = await transition_review_stage(
        record,
        stage=ReviewStage.FINAL_REVIEW,
        stage_status=StageStatus.RUNNING,
    )
    record = await save_stage_checkpoint(
        record,
        ReviewStage.FINAL_REVIEW,
        build_final_review_checkpoint(record),
    )
    return await transition_review_stage(
        record,
        workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
        stage=ReviewStage.FINAL_REVIEW,
        stage_status=StageStatus.AWAITING_FEEDBACK,
    )


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


async def start_weekly_review_workflow() -> ReviewWorkflowRecord:
    """
    Starts the current Sunday review flow behind one orchestration boundary.

    The Telegram layer only asks to start the review. This function handles
    durable workflow creation, staged reasoning, state transitions, and the
    first user-facing gate at the factual week review.
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
            workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
            stage=ReviewStage.WEEK_REVIEW,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

        return review_workflow
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


async def advance_review_from_current_stage(
    record: ReviewWorkflowRecord,
) -> ReviewWorkflowRecord:
    """
    Advances the Sunday review from the current completed user-gated stage.

    Supported advancements are explicit so workflow wiring mistakes surface
    quickly instead of silently no-oping.
    """
    if (
        record.current_stage == ReviewStage.WEEK_REVIEW
        and record.stage_status == StageStatus.COMPLETED
    ):
        record = await transition_review_stage(
            record,
            stage=ReviewStage.GOALS_AUDIT,
            stage_status=StageStatus.RUNNING,
        )
        record = await run_goals_audit_stage(record)
        return await transition_review_stage(
            record,
            workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
            stage=ReviewStage.GOALS_AUDIT,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

    if (
        record.current_stage == ReviewStage.GOALS_AUDIT
        and record.stage_status == StageStatus.COMPLETED
    ):
        record = await transition_review_stage(
            record,
            stage=ReviewStage.MEMORY_AUDIT,
            stage_status=StageStatus.RUNNING,
        )
        record = await run_memory_audit_stage(record)
        return await transition_review_stage(
            record,
            workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
            stage=ReviewStage.MEMORY_AUDIT,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

    if (
        record.current_stage == ReviewStage.MEMORY_AUDIT
        and record.stage_status == StageStatus.COMPLETED
    ):
        record = await transition_review_stage(
            record,
            stage=ReviewStage.WEEKLY_PLAN,
            stage_status=StageStatus.RUNNING,
        )
        record = await run_weekly_plan_stage(record)
        return await transition_review_stage(
            record,
            workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
            stage=ReviewStage.WEEKLY_PLAN,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

    if (
        record.current_stage == ReviewStage.WEEKLY_PLAN
        and record.stage_status == StageStatus.COMPLETED
    ):
        record = await transition_review_stage(
            record,
            stage=ReviewStage.SCHEDULING_PASS,
            stage_status=StageStatus.RUNNING,
        )
        record = await run_scheduling_pass_stage(record)
        return await transition_review_stage(
            record,
            workflow_status=ReviewWorkflowStatus.AWAITING_FEEDBACK,
            stage=ReviewStage.SCHEDULING_PASS,
            stage_status=StageStatus.AWAITING_FEEDBACK,
        )

    raise ValueError(
        "Cannot advance Sunday review from "
        f"{record.current_stage.value}/{record.stage_status.value}."
    )


def execute_weekly_state_update(content: str) -> bool:
    """
    Replaces weekly_state.md with confirmed markdown and snapshots the result.

    Kept as a named wrapper because weekly-state writes are a common Sunday
    review path and existing handlers/tests call this specific operation.
    """
    return execute_artifact_replacement(ArtifactType.WEEKLY_STATE, content)
