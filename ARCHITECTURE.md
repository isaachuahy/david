# David — Architecture

*Companion to the design document. Three diagrams at increasing levels of detail.*

---

## 1. System Context

The highest-level view. David sits between Isaac and three external systems: Telegram (interface), Google Calendar (integration), and the Gemini API (reasoning). Everything else is internal.

```mermaid
C4Context
    title David — System Context

    Person(isaac, "Isaac", "Single user. Interacts via Telegram on mobile or desktop.")

    System(david, "David", "Personal executive assistant. Manages calendar, holds context, supports reasoning and planning.")

    System_Ext(telegram, "Telegram", "Message interface. Delivers check-ins, receives commands, surfaces confirmation buttons.")
    System_Ext(gcal, "Google Calendar", "Source of truth for scheduled time. David reads all calendars and writes confirmed blocks.")
    System_Ext(gemini, "Gemini API (Google)", "Reasoning layer. Flash for daily/ad hoc, and session synthesis. Pro for weekly review.")
    System_Ext(langfuse, "Langfuse", "LLM observability. Traces every call with token counts and cost.")
    System_Ext(sentry, "Sentry", "Error alerting. Catches unhandled exceptions.")
    System_Ext(b2, "Backblaze B2", "Off-site backup. Daily snapshot of SQLite + context files.")

    Rel(isaac, telegram, "Sends messages and confirms proposals")
    Rel(telegram, david, "Delivers inbound messages")
    Rel(david, telegram, "Sends responses and proposals")
    Rel(david, gcal, "Reads events; writes confirmed blocks")
    Rel(david, gemini, "Sends assembled context; receives reasoning")
    Rel(david, langfuse, "Streams LLM call traces")
    Rel(david, sentry, "Reports exceptions")
    Rel(david, b2, "Daily backup via rclone")
```

---

## 2. Reasoning & Message Data Flow

How a message moves through the system from receipt to response, using thinking budget routing.

```mermaid
flowchart TD
    A(["📨 Inbound message
    (Telegram)"])

    A --> B{"SessionManager
    What is current state?"}

    B -- "IDLE or ACTIVE" --> C["MessageRouter
    Classify intent & assign budget"]
    B -- "ACTIVE session
    scheduled trigger fires" --> D{"Conflict resolution"}

    D -- "Daily check-in" --> D1["Queue check-in
    Deliver after session close"]
    D -- "Sunday review" --> D2["Send nudge to user
    Wait for /start_review"]

    C -- "OPERATIONAL (No budget)" --> E
    C -- "BRAINSTORM (Low budget)" --> E
    C -- "GOAL_REVIEW (High budget)" --> E

    E["ContextBuilder
    Assemble prompt
    + session history (always)"] --> G["Gemini Flash
    Returns structured FlashResponse
    {message, proposed_calendar_action}"]

    G --> J{"Session closing?
    /done or 30min timeout"}

    J -- "False
    still active" --> P{"Contains calendar
    actions?"}
    J -- "True
    closing" --> M["Send full transcript
    to Gemini Flash (High Budget)
    for synthesis"]

    M --> N["Gemini Flash
    (Session synthesis)"]

    N --> O["Update decision_log.md"]

    P -- "No" --> K["Send response
    to Telegram"]
    P -- "Yes" --> Q["ConfirmationQueue
    Write pending row to
    calendar_writes table"]

    Q --> R["Send proposal
    to Telegram
    with confirm buttons"]

    R --> S{"User response"}

    S -- "Confirmed" --> T["Execute write
    Google Calendar API
    Update row: confirmed"]
    S -- "Rejected / adjusted" --> U["Discard or re-propose
    Log outcome"]

    T --> K
    U --> K

    K --> V(["📤 Response delivered"])

    style N fill:#c8e6c9
    style G fill:#fff9c4
    style Q fill:#ffe0b2
    style T fill:#c8e6c9
```

### Key invariants

Every calendar write passes through `ConfirmationQueue` — there is no path from a model response to a Google Calendar write that bypasses user confirmation.

Flash always returns a typed `FlashResponse` object. The `MessageRouter` assigns a thinking budget based on intent classification (`OPERATIONAL`, `BRAINSTORM`, `GOAL_REVIEW`) via keyword heuristics. Because an operational query can seamlessly evolve into a brainstorm, chat history is maintained and injected into the context for all modes. This simplifies the architecture by keeping all ad hoc reasoning on a single model while scaling compute dynamically.

Session synthesis always goes to Pro regardless of whether the session was escalated mid-way.

---

## 3. Detailed Component Flowchart

Full internal architecture showing all components, data stores, and external services.

```mermaid
flowchart TD
%% ─────────────────────────────────────────
%% EXTERNAL
%% ─────────────────────────────────────────
USER(["👤 Isaac
(Telegram)"])
GCAL[("📅 Google Calendar
All Calendars")]
LANGFUSE["📊 Langfuse
LLM Traces + Cost"]
SENTRY["🚨 Sentry
Error Alerting"]

subgraph INTERFACE["📱 Telegram Interface"]
    TG["python-telegram-bot
    async message + button handlers"]
end

subgraph ORCH["⚙️ Orchestration Layer"]
    SM["SessionManager
    IDLE → ACTIVE → CLOSING → IDLE"]
    MR["MessageRouter
        Intent: Operational, Brainstorm, Goal Review
        Assigns thinking budget"]
    CB["ContextBuilder
    goals + weekly_state +
        decision_log + calendar
        + session history"]
    CQ["ConfirmationQueue
    pending calendar writes
    status: pending → confirmed"]
    TS["TriggerScheduler
    APScheduler
    daily 7–9am + Sunday 10am
    conflict resolution"]
end

subgraph REASONING["🧠 Reasoning Layer"]
    FLASH["Gemini Flash

    Daily check-ins
    Ad hoc operational
    Brainstorm turns
    Session synthesis
    Returns typed FlashResponse
    ~$0.50/$3 per 1M tokens"]
    PRO["Gemini Pro

    Sunday review
    Receives full context
    ~$2/$12 per 1M tokens"]
end

subgraph CONTEXT["📄 Context Layer"]
    GOALS["goals.md
    long / medium / short term
    human-edited"]
    WEEKLY["weekly_state.md
    current week priorities
    overwritten every Sunday"]
    DLOG["decision_log.md
    rationale trail
    appended daily, synthesised weekly"]
end

subgraph PERSIST["🗄️ Persistence — SQLite"]
    T_SESS["sessions"]
    T_DEC["decisions"]
    T_CAL["calendar_writes"]
    T_SNAP["weekly_snapshots"]
end

subgraph OBS["🔍 Observability"]
    LOGURU["loguru / app.log"]
end

subgraph INFRA["🖥️ AWS Lightsail"]
    SYSTEMD["systemd — auto-restart"]
    BACKUP["rclone → Backblaze B2"]
end

USER -- "message / button" --> TG
TG --> SM --> MR
    MR --> CB

CB --> GOALS & WEEKLY & DLOG & GCAL
CB --> FLASH

    FLASH --> TG
    FLASH --> DLOG
PRO --> TG
PRO --> DLOG & WEEKLY

FLASH & PRO --> CQ
CQ -- "proposal" --> TG
TG -- "confirm" --> CQ
CQ -- "write" --> GCAL

TS -- "daily" --> CB
TS -- "Sunday" --> PRO
TS -- "conflict" --> SM

SM --> T_SESS
CQ --> T_CAL
CB --> T_DEC
PRO --> T_SNAP

FLASH & PRO --> LANGFUSE
SM --> LOGURU
CQ --> LOGURU

SYSTEMD -.-> ORCH
BACKUP -.-> PERSIST & CONTEXT
SENTRY -.-> ORCH & REASONING
```
