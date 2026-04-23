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
