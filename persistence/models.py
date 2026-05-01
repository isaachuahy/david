from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

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


class ProposalThreadStatus(str, Enum):
    """Lifecycle for a group of related proposal items."""

    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ProposalItemStatus(str, Enum):
    """Lifecycle for one draft item inside a proposal thread."""

    QUEUED = "queued"
    ACTIVE = "active"
    IN_REVISION = "in_revision"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CalendarWriteRecord(BaseModel):
    """Executable calendar write created after user confirmation."""

    id: str
    proposal_item_id: Optional[str] = None
    action_type: str = "schedule"
    summary: str
    start_time: str
    end_time: str
    description: str
    calendar_id: str = "primary"
    target_event_id: Optional[str] = None
    target_event_calendar_id: Optional[str] = None
    status: CalendarWriteStatus
    created_at: str
    created_event_id: Optional[str] = None
    created_event_calendar_id: Optional[str] = None


class ProposalThreadRecord(BaseModel):
    """
    Durable scope for one related proposal flow.

    A chat session or Sunday review can create multiple threads. Each thread
    owns its own queue of proposal items so feedback can revise the active item
    without confusing it with unrelated calendar topics in the same session.
    """

    id: str
    source_type: str
    source_id: Optional[str] = None
    title: str = ""
    status: ProposalThreadStatus
    active_item_id: Optional[str] = None
    created_at: str
    updated_at: str


class ProposalItemRecord(BaseModel):
    """
    Durable draft for one proposed action inside a proposal thread.

    Calendar fields intentionally mirror CalendarWriteRecord. Keeping the draft
    flat makes SQLite inspection and future migrations easier while preserving a
    direct path from accepted proposal item to executable calendar write.
    """

    id: str
    thread_id: str
    item_type: str = "calendar_write"
    status: ProposalItemStatus
    sequence_index: int = 0
    revision_count: int = 0
    last_feedback: Optional[str] = None
    action_type: str = "schedule"
    summary: str
    start_time: str
    end_time: str
    description: str
    calendar_id: str = "primary"
    target_event_id: Optional[str] = None
    target_event_calendar_id: Optional[str] = None
    created_at: str
    updated_at: str


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


class ReviewWorkflowStatus(str, Enum):
    """Top-level lifecycle for a durable Sunday review workflow."""

    ACTIVE = "active"
    AWAITING_FEEDBACK = "awaiting_feedback"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewStage(str, Enum):
    """Sequential stages for the Sunday review pipeline."""

    WEEK_REVIEW = "week_review"
    GOALS_AUDIT = "goals_audit"
    MEMORY_AUDIT = "memory_audit"
    WEEKLY_PLAN = "weekly_plan"
    SCHEDULING_PASS = "scheduling_pass"
    FINAL_REVIEW = "final_review"


class StageStatus(str, Enum):
    """
    Fine-grained state inside the current Sunday review stage.

    This lets the application resume within the active stage after restarts
    instead of rerunning the entire workflow from the beginning.
    """

    NOT_STARTED = "not_started"
    RUNNING = "running"
    IN_REVISION = "in_revision"
    AWAITING_FEEDBACK = "awaiting_feedback"
    COMPLETED = "completed"


class SourceSnapshot(BaseModel):
    """
    Frozen inputs captured once at the beginning of a Sunday review.

    Storing one shared snapshot avoids copying the same source materials into
    each stage checkpoint while still giving later stages a stable baseline
    for reasoning and recovery.
    """

    goals_markdown: str
    weekly_state_markdown: str
    decision_log_markdown: str
    past_week_events: list[str] = Field(default_factory=list)
    upcoming_events: list[str] = Field(default_factory=list)


class StageCheckpoint(BaseModel):
    """
    Minimal persisted result for a Sunday review stage.

    The checkpoint is intentionally compact: it keeps only the distilled output
    needed for downstream stages and restart safety, not an exhaustive trace of
    the model's intermediate reasoning.
    """

    summary: str = ""
    key_findings: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    carry_forward: list[str] = Field(default_factory=list)


class ArtifactChangeSummary(BaseModel):
    """
    Compact semantic diff summary for one managed markdown artifact.

    These fields capture the high-signal intent of the change so the workflow
    can resume and reason about pending edits without persisting bulky raw
    line-by-line diffs at every stage. `proposed_markdown` stores the full
    rendered candidate artifact when a stage produces one for confirmation.
    """

    additions: list[str] = Field(default_factory=list)
    deletions: list[str] = Field(default_factory=list)
    modifications: list[str] = Field(default_factory=list)
    proposed_markdown: Optional[str] = None


class ReviewWorkflowRecord(BaseModel):
    """
    Durable state for the staged Sunday review workflow.

    This model is intentionally specific to Sunday review rather than a generic
    workflow abstraction. The narrower scope keeps persistence readable while
    still giving the system enough information to recover from process restarts,
    continue within the active stage, and avoid context drift during review.
    """

    id: str
    workflow_status: ReviewWorkflowStatus = ReviewWorkflowStatus.ACTIVE
    current_stage: ReviewStage = ReviewStage.WEEK_REVIEW
    stage_status: StageStatus = StageStatus.NOT_STARTED
    last_completed_stage: Optional[ReviewStage] = None
    created_at: str
    updated_at: str
    source_snapshot: SourceSnapshot
    week_review: Optional[StageCheckpoint] = None
    goals_audit: Optional[StageCheckpoint] = None
    memory_audit: Optional[StageCheckpoint] = None
    weekly_plan: Optional[StageCheckpoint] = None
    scheduling_pass: Optional[StageCheckpoint] = None
    final_review: Optional[StageCheckpoint] = None
    goals_changes: Optional[ArtifactChangeSummary] = None
    weekly_state_changes: Optional[ArtifactChangeSummary] = None
    decision_log_changes: Optional[ArtifactChangeSummary] = None
    feedback_history: list[str] = Field(default_factory=list)
