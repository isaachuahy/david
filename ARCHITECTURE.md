# David — Architecture

*Companion to the design document. Three diagrams at increasing levels of detail.*

---

## 1. System Context

The highest level view. David sits between Isaac and three external systems: Telegram (interface), Google Calendar (integration), and the Gemini API (reasoning). Everything else is internal.

```mermaid
C4Context
    title David — System Context

    Person(isaac, "Isaac", "Single user. Interacts via Telegram on mobile or desktop.")

    System(david, "David", "Personal executive assistant. Manages calendar, holds context, supports reasoning and planning.")

    System_Ext(telegram, "Telegram", "Message interface. Delivers check-ins, receives commands, surfaces confirmation buttons.")
    System_Ext(gcal, "Google Calendar", "Source of truth for scheduled time. David reads all calendars and writes confirmed blocks.")
    System_Ext(gemini, "Gemini API (Google)", "Reasoning layer. Flash for daily/ad hoc. 3.1 Pro for weekly review, escalations, session synthesis.")
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

## 2. Reasoning & Escalation Data Flow

How a message moves through the system from receipt to response, and when Flash hands off to Pro.

```mermaid
flowchart TD
    A(["📨 Inbound message\n(Telegram)"])

    A --> B{SessionManager\nWhat is current state?}

    B -- "IDLE or ACTIVE\noperational" --> C[MessageRouter\nClassify intent]
    B -- "ACTIVE session\nscheduled trigger fires" --> D{Conflict resolution}

    D -- "Daily check-in" --> D1["Queue check-in\nDeliver after session close"]
    D -- "Sunday review" --> D2["Send nudge to user\nWait for /start_review"]

    C -- "Mode A\noperational" --> E[ContextBuilder\nAssemble prompt]
    C -- "Mode B\nbrainstorm" --> F[ContextBuilder\nAssemble prompt\n+ session history]

    E --> G["Gemini Flash\n(single-turn)"]
    F --> H["Gemini Flash\n(brainstorm turn)"]

    G --> I{Response contains\nESCALATE signal?}
    H --> J{Session closing?\n/done or 30min timeout}

    I -- "No" --> K["Send response\nto Telegram"]
    I -- "Yes" --> L["EscalationHandler\nPass full context\n+ Flash summary to Pro"]

    J -- "No\nstill active" --> K
    J -- "Yes\nclosing" --> M["Send full transcript\nto Gemini 3.1 Pro\nfor synthesis"]

    L --> N["Gemini 3.1 Pro\n(escalated reasoning)"]
    M --> N

    N --> O["Pro response\nor synthesis output"]

    O --> P{Contains calendar\nactions?}

    P -- "No" --> K
    P -- "Yes" --> Q["ConfirmationQueue\nWrite pending row to\ncalendar_writes table"]

    Q --> R["Send proposal\nto Telegram\nwith confirm buttons"]

    R --> S{User response}

    S -- "Confirmed" --> T["Execute write\nGoogle Calendar API\nUpdate row: confirmed"]
    S -- "Rejected / adjusted" --> U["Discard or re-propose\nLog outcome"]

    T --> K
    U --> K

    K --> V(["📤 Response delivered"])

    style N fill:#c8e6c9
    style H fill:#fff9c4
    style G fill:#fff9c4
    style Q fill:#ffe0b2
    style T fill:#c8e6c9
```

### Key invariants

Every calendar write passes through `ConfirmationQueue` — there is no path from a model response to a Google Calendar write that bypasses user confirmation. The `[[ESCALATE: reason]]` signal is emitted by Flash as plain text at the end of its response; the orchestrator checks for it with a simple string match. Session synthesis always goes to Pro regardless of whether the session was escalated mid-way.

---

## 3. Detailed Component Flowchart

Full internal architecture showing all components, data stores, and external services.

```mermaid
flowchart TD
    %% ─────────────────────────────────────────
    %% EXTERNAL
    %% ─────────────────────────────────────────
    USER(["👤 Isaac\n(Telegram)"])
    GCAL[("📅 Google Calendar\nAll Calendars")]
    LANGFUSE["📊 Langfuse\nLLM Traces + Cost"]
    SENTRY["🚨 Sentry\nError Alerting"]

    subgraph INTERFACE["📱 Telegram Interface"]
        TG["python-telegram-bot 20.x\nasync message + button handlers"]
    end

    subgraph ORCH["⚙️ Orchestration Layer"]
        SM["SessionManager\nIDLE → ACTIVE → CLOSING → IDLE"]
        MR["MessageRouter\nMode A: Operational\nMode B: Brainstorm"]
        CB["ContextBuilder\ngoals + weekly_state +\ndecision_log + calendar"]
        EH["EscalationHandler\nFlash → Pro handoff\nPro synthesis at session close"]
        CQ["ConfirmationQueue\npending calendar writes\nstatus: pending → confirmed"]
        TS["TriggerScheduler\nAPScheduler\ndaily 7–9am + Sunday 10am\nconflict resolution"]
    end

    subgraph REASONING["🧠 Reasoning Layer"]
        FLASH["Gemini Flash\n\nDaily check-ins\nAd hoc operational\nBrainstorm turns\n~$0.50/$3 per 1M tokens\n1M context"]
        PRO["Gemini 3.1 Pro\n\nSunday review\nEscalated decisions\nSession synthesis\n~$2/$12 per 1M tokens\n1M context"]
    end

    subgraph CONTEXT["📄 Context Layer"]
        GOALS["goals.md\nlong / medium / short term\nhuman-edited"]
        WEEKLY["weekly_state.md\ncurrent week priorities\noverwritten every Sunday"]
        DLOG["decision_log.md\nrationale trail\nappended daily, synthesized weekly"]
    end

    subgraph PERSIST["🗄️ Persistence — SQLite"]
        T_SESS["sessions"]
        T_DEC["decisions"]
        T_CAL["calendar_writes"]
        T_ESC["escalations"]
        T_SNAP["weekly_snapshots"]
    end

    subgraph OBS["🔍 Observability"]
        LOGURU["loguru / app.log"]
    end

    subgraph INFRA["AWS Lightsail"]
        SYSTEMD["systemd — auto-restart"]
        BACKUP["rclone → Backblaze B2"]
    end

    USER -- "message / button" --> TG
    TG --> SM --> MR
    MR -- "Mode A" --> CB
    MR -- "Mode B" --> CB

    CB --> GOALS & WEEKLY & DLOG & GCAL
    CB --> FLASH

    FLASH -- "ESCALATE" --> EH
    FLASH -- "response" --> TG
    EH --> PRO
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
    EH --> T_ESC
    CB --> T_DEC
    PRO --> T_SNAP

    FLASH & PRO --> LANGFUSE
    SM --> LOGURU
    CQ --> LOGURU

    SYSTEMD -.-> ORCH
    BACKUP -.-> PERSIST & CONTEXT
    SENTRY -.-> ORCH & REASONING
```
