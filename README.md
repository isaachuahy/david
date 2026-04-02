# David

David is a personal executive AI assistant built for a specific failure mode of modern assistants: they can answer questions, but they struggle to help a person **run their life across time**.

The project is designed around a narrower and more operational goal:

- maintain short-term conversational context during a working session
- ground responses in persistent files such as goals, weekly state, and decision logs
- read live calendar state before making suggestions
- propose calendar actions instead of silently executing them
- run recurring check-ins and weekly reviews without requiring the user to remember to initiate them
- preserve enough structure that the system is auditable, debuggable, and incrementally extensible

David currently runs as a **single-user Telegram assistant** with **Gemini reasoning**, **Google Calendar integration**, **SQLite-backed workflow state**, and scheduled routines for daily and weekly planning.

---

## Why this exists

Typical chat assistants are optimized for stateless question answering. Executive assistance is a different problem.

In practice, the hard parts are not just generation quality. The real issues are:

- **context fragmentation**: goals, recent decisions, and calendar obligations live in different places
- **weak temporal continuity**: a good answer now can still be wrong in the context of the next week
- **unsafe write behavior**: assistants that can modify external systems need explicit control boundaries
- **no operational cadence**: users still have to remember when to review priorities, close loops, or reset direction
- **poor memory structure**: long-term plans and short-term sessions get mixed together, creating drift and noise

David addresses those issues by separating conversational reasoning from operational state.

---

## What David fixes

### 1. It turns planning into an actual system
David reads from persistent context files and live calendar events before reasoning. Instead of generating from scratch each time, it works from an operating context made of:

- goals
- weekly state
- decision log
- upcoming calendar events

### 2. It keeps calendar writes safe
Model output does **not** directly mutate Google Calendar. Calendar actions are proposed, stored as pending writes, and only executed after explicit user confirmation in Telegram.

### 3. It distinguishes live sessions from long-term memory
A session stays active while the conversation is ongoing. When it ends, David synthesizes the session into a durable decision artifact and clears the short-term conversation state.

### 4. It supports recurring executive workflows
David includes built-in scheduled routines:

- a daily check-in
- a Sunday review

These are not passive reminders. They feed into reasoning and planning workflows that can produce updated weekly state and proposed calendar actions.

---

## Current outcomes

David is already structured to provide the following outcomes:

- lower decision fatigue around planning and scheduling
- better continuity between goals, weekly priorities, and calendar reality
- explicit human approval for external writes
- session-level memory for working conversations without uncontrolled context growth
- durable decision logging after each completed session
- automated cadence for operational review

This makes it closer to an **operational copilot** than a generic chatbot.

---

## Core capabilities

### Conversational assistance
- Telegram-based interaction loop
- session-aware conversation handling
- lightweight intent classification for operational vs brainstorming vs goal-review flows
- different reasoning budgets depending on request type

### Context-aware reasoning
- loads persistent markdown context from the `context/` directory
- injects live calendar context into prompts
- includes current-session chat history in model calls

### Calendar support
- fetches upcoming events across all calendars
- fetches past events for weekly review
- proposes new events with explicit approval UI
- writes to Google Calendar only after confirmation

### Session lifecycle management
- creates session records in SQLite
- auto-times out inactive sessions after 30 minutes
- synthesizes completed sessions into decision artifacts
- reconciles orphaned sessions on restart

### Scheduled executive workflows
- daily check-in trigger
- Sunday review trigger
- weekly-state update proposal flow
- queued review event proposals, confirmed one at a time

### Operational durability
- SQLite persistence for workflow state
- Telegram persistence for user state across restarts
- restart-safe invalidation of volatile caches
- hooks for Langfuse and Sentry
- backup-related environment variables for deployment workflows

---

## Tech stack

### Runtime
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management

### Interfaces and orchestration
- `python-telegram-bot` for the main user interface and job queue
- `APScheduler` via PTB job queue for recurring triggers

### Reasoning
- Google Gemini via `google-genai`
- Gemini Flash for conversational responses and session synthesis
- Gemini Pro for the higher-value Sunday review workflow
- Pydantic schemas for structured model outputs

### Integrations
- Google Calendar API
- OAuth via `google-auth-oauthlib`

### Persistence and observability
- SQLite via `sqlite-utils`
- Loguru for logging
- Langfuse for tracing/observability
- Sentry for alerting

---

## Architecture

```text
Telegram user
    |
    v
bot/handlers.py
    |
    v
orchestrator/router.py
    |
    +--> orchestrator/context_builder.py
    |         |
    |         +--> context/*.md
    |         +--> integrations/calendar.py
    |
    +--> reasoning/flash_client.py
    |         |
    |         +--> Gemini Flash
    |
    +--> proposed calendar action?
              |
              +--> orchestrator/confirmation_queue.py
                        |
                        +--> SQLite pending write
                        +--> Telegram confirm/reject UI
                        +--> integrations/calendar.py -> Google Calendar

Session close
    |
    v
orchestrator/session_manager.py
    |
    +--> reasoning/flash_client.py (session synthesis)
    +--> context/decision_log.md
    +--> SQLite decisions table

Scheduled triggers
    |
    v
orchestrator/trigger_scheduler.py
    |
    +--> daily check-in
    +--> weekly review
              |
              +--> orchestrator/review_manager.py
              +--> reasoning/pro_client.py -> Gemini Pro
              +--> weekly state update + proposed events
```

---

## System design rationale

### Single-user by design
David is intentionally restricted to a configured Telegram user ID.

Why:
- the assistant has access to sensitive planning context and calendar data
- the current architecture is optimized for personal executive support, not multi-tenant isolation
- this sharply reduces the security and product surface area while the core operating model is still being refined

### Explicit confirmation for writes
David uses a pending-write queue before touching Google Calendar.

Why:
- model inference should not have direct write authority
- approval creates a clean human-in-the-loop control point
- queued writes are inspectable, rejectable, and expire if left unresolved

### Persistent files + live APIs instead of opaque memory only
The model context is assembled from markdown files and live calendar data.

Why:
- goals and weekly direction should be editable outside the model
- operational state should remain understandable without replaying hidden memory
- calendar data must reflect reality, not just prior conversation summaries

### Session memory, not unlimited memory
David stores current-session chat history and then synthesizes it into a durable artifact when the session ends.

Why:
- raw long chat history does not scale cleanly
- synthesis compresses useful signal into a decision log
- clearing volatile session state reduces drift and stale assumptions

### Different models for different jobs
David uses Gemini Flash for interactive response loops and Gemini Pro for Sunday review.

Why:
- not every request deserves the same latency/cost profile
- weekly review is a higher-value batch workflow with more strategic reasoning requirements
- model routing is a simpler and more controllable optimization than one-model-for-everything

### Cache where latency matters, invalidate where correctness matters
Upcoming events are cached within a session, but restart-volatile caches are invalidated on boot.

Why:
- repeated calendar fetches on every message are unnecessary
- stale external state after restart is dangerous
- this balances responsiveness with correctness

### SQLite over heavier infrastructure
David persists workflow state in SQLite.

Why:
- the current system is single-user and operationally small
- SQLite is fast, simple, inspectable, and deployment-friendly
- it keeps the architecture easy to run on a VPS without introducing service sprawl

---

## Repository structure

```text
.
├── bot/                 # Telegram handlers and UI keyboards
├── context/             # Long-lived markdown context: goals, weekly state, decisions
├── integrations/        # Google Calendar auth and API integration
├── orchestrator/        # Context assembly, routing, sessions, triggers, review flow
├── persistence/         # SQLite schema and typed records
├── reasoning/           # Gemini clients, response schemas, prompt templates
├── data/                # SQLite DB and Telegram persistence files at runtime
├── main.py              # Application entrypoint
├── config.py            # Runtime configuration loading and validation
├── pyproject.toml       # Project metadata and dependencies
└── .env.example         # Example environment variables
```

---

## Quick start

### 1. Prerequisites
You need:

- Python 3.12+
- `uv`
- a Telegram bot token
- your Telegram user ID
- a Gemini API key
- Google Calendar OAuth client credentials

### 2. Clone and install
```bash
git clone https://github.com/isaachuahy/david.git
cd david
uv sync
```

### 3. Configure environment variables
Create a local `.env` file.

```bash
cp .env.example .env
```

Required values:

```env
TELEGRAM_BOT_TOKEN="..."
ALLOWED_USER_ID="..."
GEMINI_API_KEY="..."
GOOGLE_CREDENTIALS_PATH="credentials.json"
GOOGLE_TOKEN_PATH="token.json"
```

Notes:
- `ALLOWED_USER_ID` must be your numeric Telegram user ID.
- `GOOGLE_CREDENTIALS_PATH` should point to your OAuth client JSON from Google Cloud Console.
- `GOOGLE_TOKEN_PATH` is where David stores the authorized user token after OAuth completes.

### 4. Add Google OAuth credentials
Place your Google OAuth credentials file at the configured location, commonly:

```text
credentials.json
```

On first use, David may open a local browser-based OAuth flow to create `token.json`.

For headless or VPS deployment, generate `token.json` ahead of time and deploy it with the app.

### 5. Prepare context files
Populate the `context/` directory with your operating context:

- `goals.md`
- `weekly_state.md`
- `decision_log.md`

David will still boot if some files are missing, but the assistant is materially better when these are maintained.

### 6. Run the bot
```bash
uv run python main.py
```

If startup succeeds, David will:
- validate config
- initialize SQLite tables
- reconcile orphaned sessions
- restore Telegram persistence
- register daily and weekly scheduled triggers
- start polling Telegram

---

## How to use it directly

Once the bot is running, message your Telegram bot.

Typical usage patterns:

### Operational scheduling
Examples:
- “Schedule deep work tomorrow from 9 to 11.”
- “Block 30 minutes for interview prep this afternoon.”

David will propose the event in Telegram and wait for confirmation before writing to Google Calendar.

### Brainstorming and planning
Examples:
- “Let’s discuss how I should structure this week.”
- “Brainstorm approaches for reducing decision fatigue.”

### Goal review
Examples:
- “What should I prioritize this week?”
- “Review my direction based on current goals and calendar.”

### Session closure
Use:

```text
/done
```

This closes the active session and triggers background synthesis into the decision log.

---

## Data model and persistence

David currently persists four main workflow artifacts in SQLite:

- `calendar_writes`: pending/executed/rejected/expired calendar actions
- `sessions`: session lifecycle state
- `decisions`: synthesized session outputs
- `weekly_snapshots`: backups of accepted weekly state revisions

It also stores Telegram persistence separately to preserve UI-related user state across restarts.

This separation is deliberate:
- SQLite stores auditable workflow records
- Telegram persistence stores bot interaction state
- markdown files remain the human-editable source of planning context

---

## Scheduling behavior

By default, David schedules:

- **daily check-in** at **8:00 AM America/Toronto**
- **weekly review** at **8:05 AM Sunday America/Toronto**

These are defined in `orchestrator/trigger_scheduler.py`.

If you deploy for another timezone or routine, this is one of the first places to adapt.

---

## Operational considerations

### Security
Current security posture is intentionally simple:

- bot access is restricted to one Telegram user ID
- calendar writes require explicit approval
- credentials are loaded from environment variables and local files

This is appropriate for a personal system, not a hardened multi-user SaaS.

### Failure handling
David includes several pragmatic safeguards:

- startup reconciliation of orphaned sessions
- expiration of stale calendar proposals
- auto-rejection of interrupted or session-abandoned writes
- cache invalidation on restart

### Deployment
The repo already hints at VPS-style deployment concerns:

- pre-generated `token.json` for headless calendar access
- backup-related environment variables
- Langfuse and Sentry hooks for production visibility

---

## Known limitations

At its current stage, David is intentionally opinionated and narrow.

- single-user only
- Google Calendar focused
- Telegram is the only interface
- intent classification is heuristic, not learned
- local markdown files are simple and effective, but not yet collaborative or remotely managed
- no formal API or multi-service separation yet

These are tradeoffs, not accidents. The current architecture optimizes for correctness, simplicity, and iteration speed.

---

## Where this can evolve next

Natural next steps include:

- richer planning primitives beyond calendar events
- more explicit task and project state models
- better intent routing than keyword heuristics
- calendar modification/deletion flows in addition to insertion
- stronger observability around reasoning quality and trigger outcomes
- a web or mobile control plane on top of the current core
- multi-user or role-based variants, if the product direction broadens

---

## Development philosophy

David is not trying to be a general assistant with shallow capability breadth.

It is a narrower system built around a stronger thesis:

> executive assistance works best when the assistant can reason over durable context, interact with live operational systems, preserve explicit safety boundaries, and maintain continuity across time.

That is the design center of this repository.
