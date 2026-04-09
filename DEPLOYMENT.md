# David Deployment Guide

This repository now uses `ops/david` as the canonical `systemd` deployment path for AWS Lightsail.

## The simple mental model

- `/opt/david` holds the repo checkout and `.venv`
- `/etc/david/david.env` holds production secrets and path overrides
- `systemd` keeps the bot running with `david.service`
- `systemd` runs backups with `david-backup.service` and `david-backup.timer`
- `scripts/backup.sh` uploads archives through `rclone`

## Production layout

```text
/opt/david/
  main.py
  .venv/
  scripts/

/etc/david/
  david.env
  credentials.json

/var/lib/david/
  context/
  assistant.db
  telegram_state.pkl
  token.json
```

## Required env vars

At minimum, the production env file needs:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_ID`
- `GEMINI_API_KEY`
- `GOOGLE_CREDENTIALS_PATH`
- `GOOGLE_TOKEN_PATH`
- `DAVID_DB_PATH`
- `DAVID_TELEGRAM_PERSISTENCE_PATH`
- `DAVID_CONTEXT_DIR`
- `DAVID_BACKUP_REMOTE`

Use `ops/david/david.env.example` as the template.

## First deploy

1. Put the repo on the server at `/opt/david`.
2. Run `uv sync` inside `/opt/david`.
3. Install runtime tools such as `sqlite3` and `rclone`.
4. Install the systemd units:

   ```bash
   cd /opt/david/ops/david
   sudo ./install_lightsail_systemd.sh
   ```

5. Edit `/etc/david/david.env`.
6. Copy Google OAuth credentials to `/etc/david/credentials.json`.
7. Pre-create `/var/lib/david/token.json` if you want fully headless Google Calendar access on first use.
8. Copy your live context files to `/var/lib/david/context`.
9. Start the bot:

   ```bash
   sudo systemctl start david.service
   sudo journalctl -u david.service -f
   ```

10. Run one backup manually:

   ```bash
   sudo systemctl start david-backup.service
   sudo journalctl -u david-backup.service -n 200
   ```

11. Start the daily timer:

   ```bash
   sudo systemctl start david-backup.timer
   sudo systemctl status david-backup.timer
   ```

## Operational notes

- The main service runs with an explicit command: `/opt/david/.venv/bin/python /opt/david/main.py`
- The env file is for secrets and paths, not startup shell snippets
- `Persistent=true` on the backup timer means missed runs are caught up after downtime
- `deploy/systemd` is now a legacy reference path; prefer `ops/david` for future changes
