from typing import Literal, Optional
from pydantic import BaseModel, Field

CalendarActionType = Literal["schedule", "reschedule", "cancel"]


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

    This response captures memory-quality findings for the orchestrator to store
    as a durable checkpoint before later stages propose concrete artifact edits.
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
