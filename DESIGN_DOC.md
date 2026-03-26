# David — Design Document

*Personal executive assistant bot. Single author, single user. Last updated: March 2026.*

---

## Why

The problem is not a lack of information or ambition — it is a gap between intention and execution caused by two compounding failure modes.

The first is reactive decision-making. Without a structured system that holds long-term goals in view, daily decisions get made in response to immediate pressure rather than actual priority. A message arrives, a task surfaces, and attention shifts — not because it should, but because the friction of re-evaluating everything from scratch every time is too high.

The second is decision fatigue compounding into paralysis. The same friction that causes reactive decisions also causes perfectionism to stall progress. If the cost of deciding what to do next is high, the temptation is to either react or do nothing. Neither moves the needle on what actually matters.

What's missing is a system that holds context across time — one that knows not just what's on the calendar today, but why it's there, what it connects to, and what would happen if it moved. Off-the-shelf AI assistants cannot do this. They have no persistent memory of decisions and rationale, no ability to write to a calendar, and no mechanism to trigger reviews or check-ins without manual initiation. They are reactive by design, and they may have one or a few of these capabilities, but are not integrated or seamless in terms of user experience.

David is a custom-built personal assistant with a front-end user interface on Telegram, integrates directly with Google Calendar, and maintains a living context document injected into every reasoning call. The goal is not to automate decisions but to reduce the cost of making good ones and stick with them, so that structured thinking becomes the path of least resistance rather than a tax on willpower.

In three months, success looks like: a more structured daily plan that flexes based on actual priorities, less friction between intention and action, and a clearer sense of how each day connects to longer-term goals. The gym is already frictionless. David should extend that quality — habitual, low-overhead, consistent — to cognitively demanding work.

---

## What

### Success Criteria

David works if, after three months of use:

- Daily and weekly plans are proposed, confirmed, and largely followed — with changes made intentionally rather than reactively
- Long-term goals remain visible and connected to near-term actions, not buried until a quarterly review
- The cost of deciding what to do next is low enough that it no longer becomes a reason to stall

These are intentionally qualitative. This is a personal productivity system, not a production ML service — the measure is whether it changes behaviour, not whether it hits a metric.

### Functional Requirements

David must handle three interaction modes. The first is automated: a daily morning check-in and a Sunday weekly review, both triggered without manual initiation. The second is operational ad hoc: short exchanges to reschedule events, retrieve past decisions, or block time. The third is reasoning ad hoc: open-ended brainstorming and priority discussion sessions that may run long.

All calendar writes require explicit user confirmation before execution. The bot proposes, the user confirms or adjusts, only then does anything get written to Google Calendar.

The Sunday review must read the prior week's actual calendar against what was planned, reason about the gap, propose the coming week's time blocks, and wait for sign-off before writing anything.

The system must maintain three persistent context documents — a goals poster, a weekly state, and a decision log — and inject all three into every LLM call. This is what separates David from a stateless chat assistant: it always knows the why behind the current state of the calendar.

### Technical Requirements and Constraints

David runs as a single-user system on a VPS with always-on availability — no cold starts, no sleep timeouts. The Telegram interface must be responsive to messages at any hour. Scheduled triggers must fire reliably without manual intervention.

Total cost must stay under $20/month. Expected actual cost is approximately $8–9/month including infrastructure, LLM API calls, and observability tooling.

Any calendar write must be auditable after the fact. The system must log what was proposed, whether it was confirmed, and when it was executed. Timeouts must be handled automatically, with requests or triggers queued for scheduled runs that arrive during active sessions.

### Out of Scope (v1)

The following are explicitly deferred: multi-user or shared calendar access, voice input, work calendar integration, semantic search over historical decisions, and any mobile notification mechanism beyond Telegram messages. Data privacy hardening (encryption at rest, self-hosted LLM) is also deferred.

### Assumptions

Isaac is the only user. Daily interaction volume is low — on the order of 5–15 exchanges per day across all modes. The reasoning workload is primarily natural language (priority tradeoffs, goal framing, scheduling logic), not structured ML inference. Google Calendar is the single source of truth for scheduled time.

---

## How

### Methodology

The core design decision is to treat context as a first-class artifact rather than relying on conversation history. Every LLM call is assembled by a `ContextBuilder` that reads three flat files — `goals.md`, `weekly_state.md`, `decision_log.md` — along with a live read of the Google Calendar. This means the reasoning model always has the full picture, regardless of whether the current exchange is a 1-turn reschedule request or a 30-turn brainstorm.

Instead of model routing, David uses **thinking budget routing**. The system uses a single general driver model (Gemini Flash) for all daily and ad hoc interactions, allocating inference compute dynamically per message. Gemini Pro is reserved exclusively for the Sunday weekly review and session synthesis.

The `MessageRouter` evaluates incoming messages using heuristic classification to determine the user's intent: `OPERATIONAL` (reschedule, retrieve, simple queries — no thinking budget), `BRAINSTORM` (open-ended discussion — low-medium thinking budget), or `GOAL_REVIEW` (direction, priorities, what should I do — high thinking budget). Flash is then called with the assembled context and the corresponding thinking budget passed through to the API config.

Gemini Flash always returns a typed `FlashResponse` object with two fields: `message` (the response text) and an optional `proposed_calendar_action` (a structured event proposal). If a calendar action is proposed, the orchestrator queues it for confirmation. Using a structured response schema via the `google-genai` SDK enforces this at the API level.

Brainstorming sessions run on Flash throughout and close when the user presses a `/done` button or after 30 minutes of inactivity. At close, the full session transcript is sent to Pro for synthesis. This avoids the cost of Pro for every brainstorm turn while ensuring the output benefits from stronger reasoning.

For system diagrams, see [ARCHITECTURE.md](./ARCHITECTURE.md).

### System Design

The system has five layers.

The **interface layer** is a `python-telegram-bot` async process. It receives messages and button presses, routes them to the orchestration layer, and sends responses back. Inline buttons handle confirmation flows and session close actions.

The **orchestration layer** is self-built Python — no LangChain, no LangGraph. It owns five concerns: session lifecycle state (`SessionManager`), intent classification and budget routing (`MessageRouter`), context assembly (`ContextBuilder`), confirmation-gated calendar writes (`ConfirmationQueue`), and scheduled trigger management (`TriggerScheduler`). Each is a single-responsibility module; the whole layer is approximately 400 lines.

The **reasoning layer** is two Gemini clients — Flash and Pro — both via the `google-genai` SDK. Flash receives dynamic thinking budgets based on intent. Flash responses are typed via Pydantic response schema, enforcing structured output at the API level. Both models have 1M token context windows, which means the assembled context never needs to be trimmed.

The **context layer** is three markdown files on disk. They are read by `ContextBuilder` on every call and written by Pro at the end of brainstorm sessions and Sunday reviews. Keeping them as flat files rather than database rows means they are human-readable, human-editable, and straightforward to debug.

The **persistence layer** is SQLite with four tables: `sessions`, `decisions`, `calendar_writes`, and `weekly_snapshots`. This is the structured audit trail — not the LLM's working memory, which lives in the context files. The `decision_log.md` is synthesised weekly to maintain a rolling window of recent decisions, preventing indefinite growth.

### Conversation Lifecycle

Every conversation has an explicit lifecycle: `IDLE → ACTIVE → CLOSING → IDLE`. The transition from `ACTIVE` to `CLOSING` is triggered either by the user pressing a `/done` button on Telegram (which surfaces two options — close without calendar actions, or close and propose calendar changes) or by a 30-minute inactivity timeout. At `CLOSING`, the session transcript is sent to Pro to distil decisions, rationale, and calendar actions, which are then appended to the current `decision_log.md`. The full log is only synthesised and compacted into a rolling window once a week during the Sunday review.

Scheduled triggers respect active sessions. If the daily check-in fires during an active brainstorm, it is queued and delivered immediately after the session closes. If the Sunday review fires during an active session, a non-intrusive nudge is sent and the review waits for manual initiation or fires automatically one hour after the session closes.

### Infrastructure

David runs as a `systemd` service on AWS Lightsail. Latency is 15–20ms — imperceptible given LLM call times of 1–3 seconds. The service costs approximately USD $5/month. Daily backups of `assistant.db` and the `/context` directory are pushed to Backblaze B2 via `rclone` at negligible cost.

Observability uses three tools: Langfuse (cloud free tier) for per-call LLM traces, token counts, and cost tracking; Sentry (free tier) for exception capture; and `loguru` for structured local logs with rotation.

### Tech Stack

| Layer | Tool |
|---|---|
| Language | Python 3.12 |
| Dependency management | `uv` |
| Telegram interface | `python-telegram-bot` |
| LLM — daily/ad hoc | Gemini Flash (`google-genai`) |
| LLM — weekly/escalated | Gemini Pro |
| Calendar | `google-api-python-client` + `google-auth-oauthlib` |
| Scheduler | `APScheduler` 3.x |
| Database | SQLite + `sqlite-utils` |
| Config | `python-dotenv` |
| Logging | `loguru` |
| LLM tracing | Langfuse |
| Error alerting | Sentry |
| Deployment | AWS Lightsail + `systemd` |
| Backups | `rclone` → Backblaze B2 |

### Cost

| Item | Monthly |
|---|---|
| AWS Lightsail | ~$5 USD |
| Gemini Flash (daily + ad hoc) | ~$1 |
| Gemini Pro (weekly + escalations + synthesis) | ~$1.50 |
| Langfuse, Sentry, Backblaze B2 | $0 |
| **Total** | **~$8.50** |

---

## Alternatives Considered and Rejected

**LangChain / LangGraph as the orchestration framework.** Both were considered and rejected. LangChain adds abstraction over APIs that are already mature and easy to use directly; the abstraction makes debugging harder without meaningful benefit at this scale. LangGraph is well-suited for complex agent graphs with parallel branches and many nodes — the orchestration here is sequential with a single escalation path, which is better served by 400 lines of clean Python than by a graph framework.

**Two-tier model escalation (Flash drafting for Pro).** Initially considered as a way to reserve reasoning costs. Rejected because dynamically adjusting the "thinking budget" on a single model (Flash) is architecturally simpler, faster, and achieves the same reasoning depth without maintaining an `EscalationHandler`, `escalations` database table, or complex inter-model state handoffs.

**`[[ESCALATE: reason]]` string signal for escalation routing.** Initially considered as a simple mechanism for Flash to signal the need for escalation. Rejected because Flash is a probabilistic system — it will sometimes emit partial matches, capitalisation variations, or omit the signal under certain generation conditions. String matching on free-text output is a fragile interface for a routing decision that affects cost, latency, and response quality. The current approach uses a Pydantic response schema enforced at the API level via `google-genai`'s `response_schema` parameter, making `should_escalate` a typed boolean field the orchestrator reads directly.
**Three-layer router architecture (daily layer → strategy layer → logic layer).** Considered at the suggestion of common agentic design patterns. Rejected because the complexity — additional latency, a separate routing model, more prompt engineering surface area — is not justified for a single-user, low-volume system. Heuristic classification of thinking budgets in a single router achieves the same functional outcome with far less to debug.

**Sending only Flash's summary to Pro during escalation.** Considered as a way to reduce token cost on Pro calls. Rejected because Pro's job during escalation is to reason about a real tradeoff involving goals, calendar commitments, and decision history — exactly the context Flash had. Stripping that context and asking Pro to judge from a summary alone would make Pro's reasoning less grounded than Flash's, which inverts the purpose of the escalation. Token cost on rare escalation calls is negligible given the 1M context window.

**Claude Sonnet 4.6 as the reasoning model.** Strong instruction-following and structured output reliability. Rejected primarily on cost ($3/$15 per 1M tokens vs. $2/$12 for Gemini Pro) and context window (200k vs. 1M). The 1M context window eliminates an entire class of engineering problems — no need to trim the context document as the decision log grows.

**GPT-5.4 as the reasoning model.** Impressive benchmark performance. Rejected because its tiered pricing doubles past 272k tokens for the full session — an active risk for a system that deliberately injects large context on every call.

**PostgreSQL instead of SQLite.** Rejected. PostgreSQL introduces a separate server process, network overhead, connection management, and ops complexity. For a single-user system generating ~50–100 rows per day, SQLite is the correct choice — it handles millions of rows, provides full ACID transactions, and is a single file that can be backed up with `cp`.

**Fly.io or Render for deployment.** Both considered. Fly.io has usage-based pricing that is hard to predict and risks cold starts. Render's free tier sleeps on inactivity — fatal for an always-on bot. The paid Render tier that avoids sleep costs $19/month before compute. AWS Lightsail provides full control, predictable cost, and no cold start behaviour.

**Offloading brainstorming to an external chatbot (Claude.ai, ChatGPT).** Considered as a way to leverage better UX for long reasoning sessions and avoid LLM costs during multi-turn exchanges. Rejected because it breaks the integrated workflow: an external chatbot has no awareness of the goals poster, decision log, or calendar state, so it cannot make grounded recommendations. The Flash-drafts-Pro-synthesises approach achieves low cost during exploration and high-quality output at session close, without leaving the integrated system.

---

## Appendix

### Repository Structure

```
david/
├── main.py
├── .env
├── .env.example
├── pyproject.toml
│
├── bot/
│   ├── handlers.py
│   └── keyboards.py
│
├── orchestrator/
│   ├── session_manager.py
│   ├── message_router.py
│   ├── context_builder.py
│   ├── confirmation_queue.py
│   └── trigger_scheduler.py
│
├── reasoning/
│   ├── flash_client.py
│   ├── pro_client.py
│   └── prompts/
│       ├── daily_checkin.txt
│       ├── adhoc_operational.txt
│       ├── adhoc_brainstorm.txt
│       ├── synthesis.txt
│       └── sunday_review.txt
│
├── integrations/
│   ├── calendar.py
│   └── auth.py
│
├── persistence/
│   ├── database.py
│   └── models.py
│
├── context/
│   ├── goals.md
│   ├── weekly_state.md
│   └── decision_log.md
│
├── data/
│   └── assistant.db
│
├── logs/
│   └── app.log
│
└── scripts/
    ├── setup.py
    └── backup.sh
```

### Build Order

1. Repo scaffold + `uv` environment
2. Google Calendar OAuth + basic read test
3. Telegram bot loop (echo test)
4. `ContextBuilder` + `goals.md` schema
5. First Gemini Flash call through orchestrator with typed `FlashResponse`
6. Calendar write with confirmation queue
7. APScheduler daily trigger
8. Session lifecycle (`SessionManager` + `/done` button)
9. Sunday review flow (Pro)
10. Langfuse + Sentry wiring
11. Backup script
