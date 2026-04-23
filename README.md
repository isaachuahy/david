# David

Personal executive assistant for a single user, built around Telegram, Google Calendar, persistent markdown context, and structured workflow state.

## Demo

Typing and scrolling are sped up in the demo; operational calls are not.

https://github.com/user-attachments/assets/d04ce538-03a8-4995-a3d2-3e1954462dd6

## Documentation

- [DESIGN_DOC.md](./DESIGN_DOC.md)
  Target product design, file contracts, workflow model, and key design decisions.
- [ARCHITECTURE.md](./ARCHITECTURE.md)
  Runtime flow, Sunday review pipeline, proposal lifecycle, and persisted state boundaries.
- [DEPLOYMENT.md](./DEPLOYMENT.md)
  Deployment and operational setup details.
- [ops/david/README.md](./ops/david/README.md)
  `systemd`-based production deployment assets.

## What It Does

David is designed to:

- answer operational and strategic planning questions through Telegram
- ground reasoning in `goals.md`, `weekly_state.md`, `decision_log.md`, and live calendar state
- propose calendar changes and require explicit confirmation before writing them
- maintain session synthesis and rolling memory
- run recurring workflows such as daily check-ins and Sunday review

## Tech Stack

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- `python-telegram-bot`
- Google Gemini via `google-genai`
- Google Calendar API
- SQLite
- Loguru
- Langfuse
- Sentry

## Quick Start

### 1. Prerequisites

You need:

- Python 3.12+
- `uv`
- a Telegram bot token
- your Telegram user ID
- a Gemini API key
- Google Calendar OAuth client credentials

### 2. Install

```bash
git clone https://github.com/isaachuahy/david.git
cd david
uv sync
```

### 3. Configure environment

Create a local `.env`:

```bash
cp .env.example .env
```

Minimum required values:

```env
TELEGRAM_BOT_TOKEN="..."
ALLOWED_USER_ID="..."
GEMINI_API_KEY="..."
GOOGLE_CREDENTIALS_PATH="credentials.json"
GOOGLE_TOKEN_PATH="token.json"
```

Notes:

- `ALLOWED_USER_ID` must be your numeric Telegram user ID.
- `GOOGLE_CREDENTIALS_PATH` should point to your Google OAuth client JSON.
- `GOOGLE_TOKEN_PATH` is where the authorized user token is stored.

### 4. Add Google auth files

Place your Google OAuth client credentials at the configured path, commonly:

```text
credentials.json
```

On first use, David may open a local browser-based OAuth flow to generate `token.json`.

For headless or VPS deployment, generate `token.json` ahead of time and deploy it with the app.

### 5. Prepare context files

Populate `context/` with:

- `goals.md`
- `weekly_state.md`
- `decision_log.md`

David can boot without them, but the system is materially better when they are maintained.

### 6. Run the bot

```bash
uv run python main.py
```

On successful startup, David will:

- validate config
- initialize persistence
- reconcile restart-sensitive state
- restore Telegram persistence
- register recurring triggers
- start polling Telegram

## Production Deployment

For a practical VPS setup, use the `systemd` assets in [`ops/david/`](./ops/david/).

Recommended production split:

```text
/opt/david/                  # repo checkout + .venv
/etc/david/david.env         # production env vars
/etc/david/credentials.json  # Google OAuth client JSON
/var/lib/david/context/      # goals.md, weekly_state.md, decision_log.md
/var/lib/david/assistant.db
/var/lib/david/telegram_state.pkl
/var/lib/david/token.json
```

For full instructions, see:

- [DEPLOYMENT.md](./DEPLOYMENT.md)
- [ops/david/README.md](./ops/david/README.md)

## Backups

Backups use `scripts/backup.sh` plus the production `systemd` units:

- `david-backup.service`
- `david-backup.timer`

Each backup includes:

- a consistent SQLite snapshot
- the full context directory

For restore and backup operations, use [DEPLOYMENT.md](./DEPLOYMENT.md) and the ops docs rather than this README.

## Operations

### Logging

David uses:

- Loguru for application logs
- Langfuse for LLM traces and usage visibility
- Sentry for exception reporting

In production, the first place to inspect issues is usually the service journal:

```bash
sudo journalctl -u david.service -f
```

### Health Checks

Common checks:

```bash
sudo systemctl status david.service
sudo journalctl -u david.service -n 200
```

A healthy service should:

- stay in the `active (running)` state
- start without config or auth errors
- register recurring triggers
- respond to Telegram messages
- continue reading context and calendar state normally

If backup automation is enabled, you can also verify:

```bash
sudo systemctl status david-backup.timer
sudo journalctl -u david-backup.service -n 200
```

For deeper operational troubleshooting, use [DEPLOYMENT.md](./DEPLOYMENT.md) and [ops/david/README.md](./ops/david/README.md).

## Repository Layout

```text
.
├── bot/                 # Telegram handlers and UI
├── context/             # Goals, weekly state, and decision log
├── integrations/        # Google Calendar auth and API access
├── orchestrator/        # Routing, context assembly, sessions, triggers, review flow
├── persistence/         # SQLite schema and typed records
├── reasoning/           # Model clients, schemas, and prompt templates
├── data/                # Runtime DB and Telegram persistence files
├── ops/                 # Deployment assets
├── scripts/             # Operational scripts such as backups
├── main.py              # Application entrypoint
├── config.py            # Runtime configuration loading
└── pyproject.toml       # Project metadata and dependencies
```

## Typical Use

Examples:

- “Schedule deep work tomorrow from 9 to 11.”
- “Am I free this afternoon?”
- “What should I prioritize this week?”
- “Let’s think through next week.”

Use `/done` to close an active working session and trigger background synthesis.
