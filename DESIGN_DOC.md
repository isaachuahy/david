# David — Design Document

Single-user personal executive assistant for Isaac. End-state design only.

For runtime flow and component boundaries, see [ARCHITECTURE.md](./ARCHITECTURE.md).

## Purpose

David exists to reduce the gap between intention and execution.

The system should make it easier to:
- keep long-term goals connected to weekly action
- make scheduling and prioritization decisions intentionally rather than reactively
- reduce decision fatigue during both ordinary days and weekly planning
- preserve durable lessons, preferences, and tradeoffs across time

David is not meant to automate Isaac's life. It is meant to lower the cost of making good decisions and following through on them.

## Target Outcomes

David is successful when it reliably produces the following outcomes:

- Isaac can ask for operational help or strategic help without re-explaining the full context every time.
- Calendar changes are proposed clearly, revised through feedback when needed, and executed only after explicit confirmation.
- Weekly planning reflects what actually happened during the week, not just what was previously intended.
- Durable goals, durable memory, and weekly execution state stay cleanly separated.
- The system preserves important positive and negative signal: decisions, rejections, recurring friction, and unresolved issues.
- Review workflows survive restarts and resume from the exact active stage.

## Core Interaction Modes

David has two runtime reasoning modes:

- `operational`
  Short-horizon assistance such as scheduling, availability checks, next-step help, and lightweight execution guidance.
- `strategic`
  Longer-horizon reasoning such as prioritization, tradeoffs, reflection, review, planning, and goal alignment.

These modes drive both thinking depth and context selection. They do not imply separate assistants.

## Scheduled Workflows

David also runs two recurring workflows:

- daily check-in
  A lightweight recurring planning touchpoint that helps reset the day against current priorities and calendar reality.
- Sunday review
  The heavier weekly reset that reviews the past week, audits durable context, and prepares the coming week.

## System Shape

David is designed as a lightweight, always-on assistant with four core layers:

- interface
  Telegram is the primary user surface for conversation, review feedback, and confirmations.
- reasoning
  LLM calls handle operational help, strategic help, memory synthesis, and staged weekly review.
- memory
  `goals.md`, `weekly_state.md`, and `decision_log.md` carry durable context in human-readable form.
- persistence
  Durable workflow state, proposal state, and audit data survive restarts and support resumable review flows.

This design favors clarity, inspectability, and low operational overhead over framework-heavy orchestration.

## Context Model

Context is selective, not always full.

David uses four context profiles:

- `lean`
  `CURRENT_DATETIME`, `WEEKLY_STATE`
- `calendar_context`
  `CURRENT_DATETIME`, `WEEKLY_STATE`, `UPCOMING_CALENDAR`
- `priority_strategy`
  `CURRENT_DATETIME`, `GOALS`, `WEEKLY_STATE`, `DECISION_LOG`
- `full`
  `CURRENT_DATETIME`, `GOALS`, `WEEKLY_STATE`, `DECISION_LOG`, `UPCOMING_CALENDAR`

The system should choose the smallest valid profile for the task at hand. Calendar-aware turns earn calendar context. Strategy-aware turns earn goal and memory context. Mixed turns earn full context.

## Key Architectural Decisions

- Selective context over always-full context
  The system uses the smallest valid context profile for the turn instead of injecting all artifacts into every call.
- Markdown artifacts as first-class memory
  Goals, weekly state, and rolling memory stay human-readable, editable, and easy to audit.
- Staged Sunday review over one-shot review
  Weekly review is split into sequential stages so later steps inherit earlier findings.
- Revision-aware proposal threads
  Calendar proposals are refined through revisions and feedback before execution.
- Durable workflow state over chat-history-only coordination
  Structured workflow records, snapshots, and change sets are the source of truth for review recovery and resume behavior.

## Persistent Artifacts

David maintains three long-lived markdown artifacts. Each has a strict ownership boundary.

### `goals.md`

Purpose:
- store durable direction and operating principles

Contains:
- long-term goals
- medium-term goals
- near-term goals that still matter beyond a single week
- stable operating principles David should consistently respect

Does not contain:
- this week's priorities
- session-level decisions
- temporary scheduling concerns
- short-lived execution notes

### `weekly_state.md`

Purpose:
- store this week's operating plan only

Contains:
- top priorities for the current week
- intentional carryover
- current-week constraints
- execution focus for this week

Does not contain:
- durable goals
- cross-week preferences
- rolling memory
- transcript-style reasoning

### `decision_log.md`

Purpose:
- store cross-week memory plus a current-week inbox

Contains two sections:
- `Current Rolling Context`
  Durable memory that should survive week resets
- `Recent Decisions (Appended Daily)`
  Current-week notes waiting for weekly compaction

`Current Rolling Context` stores:
- durable preferences and boundaries
- important accepted or rejected decisions with lasting relevance
- recurring friction patterns
- interpretation rules that improve future reasoning
- unresolved issues worth resurfacing later

`Recent Decisions` stores:
- compact weekly signal for later review and compaction

It should not become a transcript archive or a duplicate of `weekly_state.md`.

## Memory Maintenance

David maintains memory in two passes:

- session synthesis
  Distills completed sessions into compact entries for `Recent Decisions (Appended Daily)`
- weekly compaction
  Promotes durable signal from `Recent Decisions` into `Current Rolling Context` and clears the weekly inbox

Session synthesis should keep:
- accepted decisions
- meaningful rejections
- important rationale
- recurring friction
- notable follow-ups or unresolved issues
- interpretation rules clarified during the session

Weekly compaction should keep only what still matters after the week ends. It should not preserve routine schedule snapshots or restate weekly execution plans.

## Rejected Alternatives

- Always injecting full context into every call
  This increases token use and noise on turns that only need a narrow slice of state.
- One-shot Sunday review
  A single reasoning pass cannot reliably carry forward constraints, revisions, and feedback across the whole review flow.
- Single immutable proposals
  Calendar work often needs iterative revision, so proposals must support multiple drafts before confirmation.
- Transcript-only memory
  Raw history is too noisy to serve as durable memory without structured synthesis and weekly compaction.
- Raw line-diff artifact management
  Markdown files need semantic additions, deletions, and modifications rather than low-level textual diffs.

## Proposal Model

Calendar proposals are revision-aware.

David uses proposal threads, not single immutable proposals.

A proposal thread represents one underlying intent, such as:
- reschedule an event
- find a workable deep work block
- refine a weekly review scheduling proposal

Thread-level states:
- `draft`
- `in_revision`
- `ready_for_confirmation`
- `confirmed`
- `rejected`
- `executed`

Revision-level states:
- `active`
- `superseded`

`superseded` applies to an older revision that has been replaced by a newer revision in the same thread.

`rejected` applies to the proposal thread as a whole.

Calendar `cancel` remains an event operation, not a general proposal lifecycle state.

## Sunday Review

Sunday review is a staged workflow.

Its responsibilities are:
- review what happened during the week
- reconfirm goals and detect drift
- audit rolling memory for accuracy and relevance
- reset the coming week's operating plan
- propose time blocks that respect what the review has learned

The workflow runs in order:

1. `week_review`
2. `goals_audit`
3. `memory_audit`
4. `weekly_plan`
5. `scheduling_pass`
6. `final_review`

Later stages must inherit constraints and lessons from earlier stages. If the review learns that a certain class of schedule proposal does not work, the scheduling pass must respect that.

## Workflow State

Sunday review is a durable workflow, not a disposable session.

Each review begins by freezing one `SourceSnapshot`:
- `goals.md`
- `weekly_state.md`
- `decision_log.md`
- past-week calendar
- upcoming calendar context when needed

This snapshot is stored once for the workflow and reused by later stages. Stages should not keep duplicating the same source files.

The workflow persists a `ReviewState` record that stores:
- workflow status
- current stage
- stage status
- source snapshot reference
- compact stage outputs
- artifact change sets
- active proposal threads

Chat history may support the workflow, but it is not the source of truth.

## Stage Outputs

Each review stage writes a compact structured result. A stage output should store only what later stages need, such as:
- `summary`
- `key_findings`
- `constraints`
- `carry_forward`
- final artifact text when that stage directly produces one

Stage outputs should be short, behavior-driving summaries rather than verbose reasoning dumps.

## Artifact Changes

Markdown updates should be tracked as semantic change sets rather than raw line diffs.

Each change set records:
- additions
- deletions
- modifications
- a short summary of intent

Full markdown is the final rendered artifact, not the only stored representation.

This keeps review outputs:
- inspectable
- resumable
- easier to revise after feedback

## Reliability And Recovery

Review workflows must survive restarts and process failures. The system guarantees:
- stale-session cleanup does not discard active review workflows
- each stage has a commit boundary
- a stage advances only after its output is validated and persisted
- workflows resume from the exact active stage and interaction state
- important review progress is never stored only in chat history

Ordinary chat sessions may be disposable. Sunday review workflows are not.

## Functional Deliverables

The end-state system should deliver:

- operational and strategic reasoning modes
- selective context routing
- confirmation-gated calendar execution
- revision-aware proposal threads
- compact session synthesis
- durable weekly memory compaction
- staged Sunday review orchestration
- persisted review workflow state
- semantic change tracking for managed markdown artifacts

## Constraints

The system is designed for:
- one user
- low to moderate daily interaction volume
- always-on availability
- auditable calendar writes
- low monthly operating cost

The design optimizes for clarity, durability, and low-friction reasoning rather than maximal automation or multi-user scale.

## Out Of Scope

The following are out of scope for this design:
- multi-user support
- shared-calendar collaboration workflows
- voice-first interaction
- enterprise/team planning
- transcript archival as a primary memory system
- generic agent framework abstraction for its own sake
