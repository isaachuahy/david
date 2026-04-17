from pydantic import BaseModel, Field


class ProposedEvent(BaseModel):
    """
    Structured calendar proposal returned by the reasoning layer.

    The reasoning layer may identify which calendar the user meant via
    `requested_calendar_text`, but application code is responsible for resolving
    that text into authoritative calendar metadata exactly once.

    `calendar_id` is the canonical Google Calendar identifier that must be
    preserved throughout the scheduling flow after deterministic resolution.
    `calendar_display_name` is a human-readable label for UI only and must
    never be used as the write target.
    """

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
