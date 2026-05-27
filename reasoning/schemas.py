from typing import Literal, Optional
from pydantic import BaseModel, Field

CalendarActionType = Literal["schedule", "reschedule", "cancel"]
CalendarPlanningMode = Literal["none", "discuss", "propose"]


class ProposedEvent(BaseModel):
    """
    Structured calendar action proposed by the reasoning layer.

    The model may infer the user's intended calendar via `requested_calendar_text`,
    but application code is responsible for resolving that reference into canonical
    calendar metadata before any write occurs.
    """


    action_type: CalendarActionType = Field(
        default="schedule",
        description="Calendar action intent: schedule, reschedule, or cancel.",
    )
    summary: str = Field(description="The title of the calendar event.")
    start_time: str = Field(description="The event start time in timezone-aware ISO 8601 format, including a UTC offset, e.g., 2026-03-22T09:00:00-04:00")
    end_time: str = Field(description="The event end time in timezone-aware ISO 8601 format, including a UTC offset, e.g., 2026-03-22T11:00:00-04:00")
    description: str = Field(description="A brief description of the event's purpose.")
    requested_calendar_text: str = Field(
        default="primary",
        description=(
            "The calendar reference implied by the user request, such as "
            "'primary' or 'entertainment calendar'. Application code must "
            "resolve this text deterministically before any write occurs."
        ),
    )
    calendar_id: str = Field(
        default="primary",
        description=(
            "Canonical Google Calendar ID to use for storage and API writes. "
            "This field is authoritative only after application code has "
            "resolved requested_calendar_text. It should not be treated as a "
            "free-form user-facing label."
        ),
    )
    calendar_display_name: str = Field(
        default="Primary",
        description=(
            "Human-readable calendar label for UI only after deterministic "
            "resolution. This is not an API identifier and must never be used "
            "in place of calendar_id."
        ),
    )
    target_event_summary: Optional[str] = Field(
        default=None,
        description="For cancel/reschedule: the original event title or shorthand to match against upcoming events.",
    )
    target_event_start_time: Optional[str] = Field(
        default=None,
        description="For cancel/reschedule: optional original event start time in timezone-aware ISO 8601 format to disambiguate a specific event.",
    )
    target_event_id: Optional[str] = Field(
        default=None,
        description="Resolved canonical Google Calendar event ID for cancel/reschedule operations.",
    )
    target_event_calendar_id: Optional[str] = Field(
        default=None,
        description="Resolved canonical calendar ID containing target_event_id.",
    )


class ProposalThreadDraft(BaseModel):
    """
    LLM-facing draft for one related set of calendar proposals.

    A single user turn may produce zero, one, or multiple calendar items, but
    related items should stay under one thread so the application can confirm
    or revise them one at a time without losing their shared intent.
    """

    title: str = Field(
        description="Short title for the proposal thread, such as 'Moving house schedule'.",
    )
    rationale: str = Field(
        description="Concise explanation of why these proposed events belong together.",
    )
    proposed_events: list[ProposedEvent] = Field(
        default_factory=list,
        description="Concrete calendar actions proposed for later confirmation.",
    )


class WeekReviewResponse(BaseModel):
    """
    LLM-facing schema for the week review stage of the Sunday review workflow.

    This schema describes what Gemini should populate. The orchestrator for review_manager maps it
    into the durable, generic StageCheckpoint stored on ReviewWorkflowRecord.
    """

    summary: str = Field(
        description="One to three sentences summarizing what actually happened during the past week.",
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description=(
            "Three to six compact findings later review stages should account "
            "for when auditing goals, auditing memory, planning the next week, "
            "or proposing calendar blocks."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Current constraints, recurring friction, or learned scheduling and "
            "energy limits that should shape later review stages."
        ),
    )
    carry_forward: list[str] = Field(
        default_factory=list,
        description=(
            "Unresolved items, meaningful rejections, open loops, or priorities "
            "that should be considered by later review stages."
        ),
    )


class GoalsAuditResponse(BaseModel):
    """
    LLM-facing schema for the goals audit stage of the Sunday review workflow.

    The orchestrator maps this stage-specific response into a generic
    StageCheckpoint while preserving the stage identity on ReviewWorkflowRecord.
    """

    summary: str = Field(
        description=(
            "One to three sentences assessing whether the durable goals and "
            "operating principles still appear accurate and useful."
        ),
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description=(
            "Compact findings about goals or principles that still hold, may be "
            "stale, need clarification, or appear misaligned with this week's evidence."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Goal-related constraints, tensions, or tradeoffs that later review "
            "stages should respect when planning the next week."
        ),
    )
    carry_forward: list[str] = Field(
        default_factory=list,
        description=(
            "Goal questions, possible emphasis shifts, or reconfirmation points "
            "that should be carried into memory audit, weekly planning, or final review."
        ),
    )


class MemoryAuditResponse(BaseModel):
    """
    LLM-facing schema for the memory audit stage of the Sunday review workflow.

    This response captures memory-quality findings before a separate proposal
    pass derives concrete decision-log changes from the checkpoint.
    """

    summary: str = Field(
        description=(
            "One to three sentences assessing whether rolling memory and recent "
            "decisions remain accurate, useful, and non-duplicative."
        ),
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description=(
            "Compact findings about durable memory that should be kept, stale "
            "or duplicated memory, useful recent decisions, meaningful rejections, "
            "or unresolved loops."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Memory-related constraints, risks, or interpretation boundaries "
            "that later review stages should respect."
        ),
    )
    carry_forward: list[str] = Field(
        default_factory=list,
        description=(
            "Memory edits, compaction candidates, or reconfirmation questions "
            "that should be considered by weekly planning or final review."
        ),
    )


class DecisionLogChangeProposalResponse(BaseModel):
    """
    LLM-facing schema for proposed decision-log operations.

    These are candidate operations only. The application materializes them into
    proposed markdown deterministically, and nothing is written until the user
    confirms the memory-audit gate.
    """

    proposed_rolling_context_additions: list[str] = Field(
        default_factory=list,
        description=(
            "Candidate complete bullet lines to add to the Current Rolling Context section."
        ),
    )
    proposed_rolling_context_deletions: list[str] = Field(
        default_factory=list,
        description=(
            "Candidate exact existing bullet lines from Current Rolling Context to remove."
        ),
    )
    proposed_rolling_context_modifications: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Candidate exact old bullet line to replacement bullet line mappings. "
            "Keys must match existing Current Rolling Context bullets exactly."
        ),
    )
    proposed_recent_decisions_reset: bool = Field(
        default=True,
        description=(
            "Whether the proposed markdown should reset Recent Decisions to the "
            "placeholder after compaction."
        ),
    )
    proposed_recent_decisions_carry_forward: list[str] = Field(
        default_factory=list,
        description=(
            "Candidate recent-decision notes to keep visible because they were "
            "not compacted into Current Rolling Context."
        ),
    )


class WeeklyPlanResponse(BaseModel):
    """
    LLM-facing schema for the weekly plan stage of the Sunday review workflow.

    This stage produces both a compact checkpoint and the full proposed
    weekly_state.md markdown for later user confirmation.
    """

    summary: str = Field(
        description="One to three sentences summarizing the proposed next-week operating plan.",
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description=(
            "Compact planning findings that explain the most important priority, "
            "carryover, or tradeoff choices."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Current-week constraints that should shape scheduling, prioritization, "
            "or revision of the proposed weekly plan."
        ),
    )
    carry_forward: list[str] = Field(
        default_factory=list,
        description=(
            "Important unresolved items or active carryover that should remain "
            "visible after this planning stage."
        ),
    )
    state_change_summary: str = Field(
        description=(
            "A concise human-readable summary of how the proposed weekly state "
            "differs from the current weekly state."
        ),
    )
    weekly_state_content: str = Field(
        description=(
            "The full proposed markdown content for weekly_state.md. This should "
            "be ready for user confirmation before application."
        ),
    )


class SchedulingPassResponse(BaseModel):
    """
    LLM-facing schema for the scheduling pass stage of the Sunday review workflow.

    This stage proposes calendar actions for later confirmation while also
    persisting a compact checkpoint for restart safety and downstream review.
    """

    summary: str = Field(
        description="One to three sentences summarizing the scheduling recommendation.",
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description="Compact findings that explain the most important scheduling choices.",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Scheduling constraints that shaped the proposed events or lack of events.",
    )
    carry_forward: list[str] = Field(
        default_factory=list,
        description="Scheduling questions or unresolved items to preserve for later review.",
    )
    proposed_events: list[ProposedEvent] = Field(
        default_factory=list,
        description="Calendar actions proposed for later user confirmation.",
    )
    scheduling_rationale: str = Field(
        description="A concise explanation of why the proposed calendar actions make sense as a set.",
    )
