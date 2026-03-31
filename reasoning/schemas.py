from pydantic import BaseModel, Field


class ProposedEvent(BaseModel):
    summary: str = Field(description="The title of the calendar event.")
    start_time: str = Field(description="The event start time in timezone-aware ISO 8601 format, including a UTC offset, e.g., 2026-03-22T09:00:00-04:00")
    end_time: str = Field(description="The event end time in timezone-aware ISO 8601 format, including a UTC offset, e.g., 2026-03-22T11:00:00-04:00")
    description: str = Field(description="A brief description of the event's purpose.")
