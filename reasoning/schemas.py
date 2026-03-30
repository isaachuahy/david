from pydantic import BaseModel, Field


class ProposedEvent(BaseModel):
    summary: str = Field(description="The title of the calendar event.")
    start_time: str = Field(description="The start time in ISO 8601 format (UTC), e.g., 2026-03-22T09:00:00Z")
    end_time: str = Field(description="The end time in ISO 8601 format (UTC), e.g., 2026-03-22T11:00:00Z")
    description: str = Field(description="A brief description of the event's purpose.")
