# David Deployment Guide

This guide is for running David on an Ubuntu VPS such as AWS Lightsail without Docker.

## Mental Model

There are three different kinds of files involved:

- App code: `main.py`, `config.py`, `bot/handlers.py`
- Ops script: `scripts/backup.sh`
- System service templates: `deploy/systemd/david.service`, `deploy/systemd/david-backup.service`, `deploy/systemd/david-backup.timer`

If you mostly know Python, the easiest framing is:

- Python files define what David does.
- `systemd` defines how Ubuntu keeps David running.
- The shell script defines a maintenance task Ubuntu can run for you.

## What `deploy/` Is

The `deploy/` directory is just a folder in the repo for deployment templates.

Ubuntu does not automatically use these files from the repo. You copy them into `/etc/systemd/system/` when you are ready to activate them.

## What Each Deployment File Does

`deploy/systemd/david.service`
- Runs the bot as a long-lived background service.
- Starts at boot if enabled.
- Restarts automatically if the Python process crashes.

`deploy/systemd/david-backup.service`
- Runs one backup job once.
- Not a permanent process.
- Usually triggered by the timer below.

`deploy/systemd/david-backup.timer`
- Schedules the backup job daily.
- Similar role to cron, but managed by `systemd`.

`scripts/backup.sh`
- Makes a safe SQLite snapshot with `sqlite3 .backup`
- Copies the `context/` folder
- Compresses both into a `.tar.gz`
- Uploads the archive via `rclone`

## How The Main Service Works

When Ubuntu starts the main service, `systemd` reads `deploy/systemd/david.service` and runs:

```bash
uv run python main.py
```

Inside Python:

1. `main.py` calls `config.py`
2. `config.py` loads `.env` and validates required settings
3. The Telegram app is built and handlers are registered
4. Polling starts and the service stays alive

## Key `systemd` Fields In Plain English

These are the lines you are most likely to edit:

`User=ubuntu`
- Which Linux user account runs the bot.
- On Lightsail, `ubuntu` is common, but use the actual account that owns `/opt/david`.

`WorkingDirectory=/opt/david`
- The folder the bot runs from.
- Think of this like the terminal directory before you run `python main.py`.

`EnvironmentFile=/opt/david/.env`
- The file `systemd` reads for environment variables before starting David.

`ExecStart=/usr/bin/env uv run python main.py`
- The exact command Ubuntu will execute to start the bot.

`Restart=on-failure`
- If the process crashes, Ubuntu starts it again.

`OnCalendar=*-*-* 03:15:00`
- In the timer file, this means every day at 03:15 server time.

## Required Files On The Server

A practical target layout is:

```text
/opt/david/
  main.py
  config.py
  .env
  credentials.json
  token.json
  data/
  context/
  scripts/
```

You will also copy these out of the repo into Ubuntu's service directory:

```text
/etc/systemd/system/david.service
/etc/systemd/system/david-backup.service
/etc/systemd/system/david-backup.timer
```

## Environment Variables

Use `.env.example` as the template for your real `.env`.

Required for startup:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_ID`
- `GEMINI_API_KEY`

Needed for Google Calendar:

- `GOOGLE_CREDENTIALS_PATH`
- `GOOGLE_TOKEN_PATH`

Needed for backups:

- `DAVID_BACKUP_REMOTE`

Optional overrides:

- `DAVID_DB_PATH`
- `DAVID_PROJECT_DIR`
- `DAVID_CONTEXT_DIR`
- `DAVID_BACKUP_HOST`
- `DAVID_BACKUP_TMPDIR`

## One-Time Server Setup

This assumes your repo is already on the server at `/opt/david`.

1. Create and fill in the env file.

```bash
cd /opt/david
cp .env.example .env
nano .env
```

2. Make sure the Google auth files exist.

Required before production:

- `credentials.json`
- `token.json`

If `token.json` is missing, the first calendar call may try to start an interactive OAuth flow, which is not what you want on a headless VPS.

3. Make sure the runtime tools exist on the server.

You need:

- Python
- `uv`
- `sqlite3`
- `rclone`

## Install The Services

Copy the unit files into Ubuntu's system service directory:

```bash
sudo cp /opt/david/deploy/systemd/david.service /etc/systemd/system/david.service
sudo cp /opt/david/deploy/systemd/david-backup.service /etc/systemd/system/david-backup.service
sudo cp /opt/david/deploy/systemd/david-backup.timer /etc/systemd/system/david-backup.timer
sudo systemctl daemon-reload
```

Enable and start the bot:

```bash
sudo systemctl enable --now david.service
```

Enable the daily backup timer:

```bash
sudo systemctl enable --now david-backup.timer
```

## Useful Commands

Check whether the bot is running:

```bash
sudo systemctl status david.service
```

Watch bot logs live:

```bash
sudo journalctl -u david.service -f
```

Check whether the timer is scheduled:

```bash
sudo systemctl status david-backup.timer
sudo systemctl list-timers david-backup.timer
```

Run one backup immediately:

```bash
sudo systemctl start david-backup.service
sudo journalctl -u david-backup.service -n 100
```

Restart the bot after pulling new code:

```bash
cd /opt/david
git pull
sudo systemctl restart david.service
```

## What The Backup Script Actually Does

`scripts/backup.sh` is a Bash script. In practical terms, it is just a saved list of terminal commands with a little logic around them.

Its flow is:

1. Read settings from environment variables
2. Check that `sqlite3`, `tar`, and `rclone` are installed
3. Create a temporary working folder
4. Use SQLite's backup command to make a consistent copy of the database
5. Copy the `context/` directory
6. Compress the results
7. Upload the archive with `rclone`
8. Delete the temporary working folder

## Customization Checklist

Before using these templates in production, review:

- `deploy/systemd/david.service`: `User`, `WorkingDirectory`, `EnvironmentFile`
- `deploy/systemd/david-backup.service`: `User`, `WorkingDirectory`, `EnvironmentFile`
- `deploy/systemd/david-backup.timer`: backup time
- `.env.example`: actual values in your real `.env`

## Suggested First Deployment Path

If you want the least confusing first pass:

1. Get `david.service` running first
2. Confirm Telegram + Gemini + Calendar work
3. Run `scripts/backup.sh` manually once
4. Only then enable `david-backup.timer`

That keeps the “always-on bot” problem separate from the “automated backup” problem.
