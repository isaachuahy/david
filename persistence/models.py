from enum import Enum
from typing import Optional
from pydantic import BaseModel

class SessionStatus(str, Enum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"

class CalendarWriteStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"

class CalendarWriteRecord(BaseModel):
    id: str
    summary: str
    start_time: str
    end_time: str
    description: str
    status: CalendarWriteStatus
    created_at: str

class SessionRecord(BaseModel):
    id: str
    status: SessionStatus
    start_time: str
    end_time: Optional[str] = None


class DecisionRecord(BaseModel):
    id: str
    session_id: str
    timestamp: str
    content: str


class WeeklySnapshotRecord(BaseModel):
    id: str
    timestamp: str
    weekly_state_content: str
