# David — Architecture

Companion to [DESIGN_DOC.md](./DESIGN_DOC.md). End-state runtime architecture only.

## 1. System Context

David sits between Isaac and three external systems:
- Telegram for interaction
- Google Calendar for read/write scheduling
- an LLM API for reasoning and synthesis

```mermaid
C4Context
    title David — System Context

    Person(isaac, "Isaac", "Single user. Interacts through Telegram.")

    System(david, "David", "Personal executive assistant. Holds context, reviews the week, proposes plans, and manages calendar changes.")

    System_Ext(telegram, "Telegram", "Message interface and confirmation surface.")
    System_Ext(gcal, "Google Calendar", "Source of truth for scheduled time.")
    System_Ext(llm, "LLM API", "Reasoning, synthesis, and review stages.")

    Rel(isaac, telegram, "Sends messages and feedback")
    Rel(telegram, david, "Delivers messages and button actions")
    Rel(david, telegram, "Sends responses, drafts, and confirmations")
    Rel(david, gcal, "Reads availability and writes confirmed changes")
    Rel(david, llm, "Sends structured prompts and receives structured outputs")
```

## 2. Normal Interaction Flow

Normal conversation stays lightweight and selective.

- The router classifies each turn as `operational` or `strategic`.
- The router also detects whether the turn needs calendar context, strategy context, or both.
- The context builder selects the smallest valid context profile.
- Calendar proposals enter a revision-aware proposal thread.
- Only a confirmed proposal is executed against Google Calendar.

```mermaid
flowchart TD
    A["Inbound Telegram message"] --> B["RoutingDecision
    intent + context flags"]
    B --> C["ContextBuilder
    select smallest profile"]
    C --> D["LLM call"]
    D --> E{"Response contains
    calendar proposal?"}

    E -- "No" --> F["Reply to user"]
    E -- "Yes" --> G["Proposal thread
    create or revise draft"]
    G --> H["Show draft for feedback"]
    H --> I{"User response"}

    I -- "Revise" --> G
    I -- "Confirm" --> J["Execute confirmed calendar change"]
    I -- "Reject" --> K["Mark thread rejected"]

    J --> F
    K --> F
```

### Context Profiles

David uses four context profiles:
- `lean`
- `calendar_context`
- `priority_strategy`
- `full`

The router chooses the smallest profile that can answer the turn correctly.

The system also runs two scheduled routines:
- daily check-in, which reuses the normal interaction flow with scheduled initiation
- Sunday review, which uses the staged workflow below

## 3. Sunday Review Workflow

Sunday review is a durable, gated workflow. Each model call is narrow and
checkpointed, and user confirmation is required before downstream stages depend
on uncertain facts or user-impacting artifacts.

It runs as a staged pipeline:
1. `week_review`
2. factual confirmation
3. `goals_audit`
4. conditional goals confirmation
5. `memory_audit`
6. conditional memory confirmation
7. `weekly_plan`
8. weekly-plan confirmation
9. `scheduling_pass`
10. calendar proposal confirmation
11. `final_review`

Each stage consumes:
- the frozen `SourceSnapshot`
- committed outputs from earlier stages
- active proposal and revision state when relevant

```mermaid
flowchart TD
    A["Start Sunday review"] --> B["Freeze SourceSnapshot"]
    B --> C["week_review"]
    C --> C1{"Week facts confirmed?"}
    C1 -- "Revise" --> C
    C1 -- "Confirm" --> D["goals_audit"]
    D --> D1{"Goal changes need confirmation?"}
    D1 -- "Revise/reconfirm" --> D
    D1 -- "No or confirmed" --> E["memory_audit"]
    E --> E1{"Memory edits need confirmation?"}
    E1 -- "Revise/reconfirm" --> E
    E1 -- "No or confirmed" --> F["weekly_plan"]
    F --> P{"Weekly plan accepted?"}
    P -- "Revise" --> F
    P -- "Accept" --> G["scheduling_pass"]
    G --> I["Confirm calendar proposals item by item"]
    I --> H["final_review"]
```

### Sunday Review Guarantees

- Later stages must respect constraints learned earlier in the review.
- Review progress is persisted after each stage.
- A restart resumes the active stage rather than restarting the workflow.
- Review proposals remain revision-aware through feedback loops.

## 4. Persistent State Model

The architecture uses four kinds of durable state:
- markdown artifacts
- a frozen review snapshot
- compact workflow state
- proposal threads and revisions

```mermaid
flowchart LR
    subgraph Artifacts["Managed Artifacts"]
        G["goals.md"]
        W["weekly_state.md"]
        D["decision_log.md"]
    end

    subgraph Review["Review Workflow State"]
        S["SourceSnapshot"]
        R["ReviewState
        current_stage
        stage_status
        stage_outputs"]
        C["ArtifactChangeSets
        additions / deletions / modifications"]
    end

    subgraph Proposals["Proposal Lifecycle"]
        T["ProposalThread"]
        V["ProposalRevision
        active / superseded"]
    end

    subgraph Calendar["Execution"]
        Q["Confirmed write queue"]
        GC["Google Calendar"]
    end

    G --> S
    W --> S
    D --> S
    S --> R
    R --> C
    R --> T
    T --> V
    T --> Q
    Q --> GC
```

### Source Snapshot

Each review freezes one `SourceSnapshot` containing:
- `goals.md`
- `weekly_state.md`
- `decision_log.md`
- past-week calendar data
- upcoming calendar context when needed

The snapshot is stored once per workflow and reused by all stages.

### ReviewState

`ReviewState` is the source of truth for review recovery. It stores:
- workflow status
- current stage
- stage status
- source snapshot reference
- compact stage outputs
- artifact change sets
- active proposal threads

Chat history may support the workflow, but it is never the authoritative state.

### Stage Outputs

Each stage writes a compact structured result, typically:
- `summary`
- `key_findings`
- `constraints`
- `carry_forward`
- final artifact text when that stage directly produces one

These outputs are concise and behavior-driving. They are not transcript dumps.

### Artifact Change Sets

Managed markdown files are updated through semantic change sets rather than raw line diffs.

Each change set records:
- additions
- deletions
- modifications
- a short summary

Full markdown is the final rendered artifact, not the only stored form.

## 5. Proposal Lifecycle

Calendar work uses proposal threads with revisions.

Thread states:
- `draft`
- `in_revision`
- `ready_for_confirmation`
- `confirmed`
- `rejected`
- `executed`

Revision states:
- `active`
- `superseded`

Rules:
- `superseded` applies to an older revision that was replaced.
- `rejected` applies to the thread as a whole.
- calendar `cancel` remains an event action, not a proposal lifecycle state.
- only confirmed proposals enter the execution queue.

## 6. Recovery And Restart Behavior

Review workflows are durable and resumable.

The system guarantees:
- stale-session cleanup does not discard active reviews
- each stage has a commit boundary
- stage outputs are persisted before advancing the workflow
- the system resumes from the exact active stage and interaction state
- important progress is never stored only in chat history

Normal ad hoc conversations may be lightweight. Sunday review is not.
