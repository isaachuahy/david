# David on AWS Lightsail

This folder is the canonical `systemd` deployment path for running David on a Lightsail instance.

## What lives here

- `david.service`: the main long-running bot service
- `david-backup.service`: a one-shot backup job
- `david-backup.timer`: the daily schedule for backups
- `david.env.example`: the production env template loaded by both services
- `install_lightsail_systemd.sh`: installer for the units and the `david` service user

## Intended server layout

```text
/opt/david/                  # repo checkout + .venv
/etc/david/david.env         # production environment variables
/etc/david/credentials.json  # Google OAuth client JSON
/var/lib/david/context/      # goals.md, weekly_state.md, decision_log.md
/var/lib/david/assistant.db
/var/lib/david/telegram_state.pkl
/var/lib/david/token.json
```

Production context lives under `/var/lib/david/context`, which keeps mutable planning files separate from the code checkout.

## Before installing

1. Put the repo on the server at `/opt/david`.
2. Create the virtualenv and install dependencies:

   ```bash
   cd /opt/david
   uv sync
   ```

3. Make sure the server has the external tools used at runtime:
   - `sqlite3`
   - `rclone`

4. Prepare Google Calendar auth:
   - copy the OAuth client JSON to `/etc/david/credentials.json`
   - pre-create `token.json` and place it at `/var/lib/david/token.json` if you want headless startup on first calendar use
5. Copy your live context files into `/var/lib/david/context`:
   - `goals.md`
   - `weekly_state.md`
   - `decision_log.md`

## Install the units

```bash
cd /opt/david/ops/david
sudo ./install_lightsail_systemd.sh
```

Then edit the real env file:

```bash
sudoedit /etc/david/david.env
```

That file should contain your production secrets and path overrides. It is not meant to be committed to git.

## First startup

Start the bot first:

```bash
sudo systemctl start david.service
sudo journalctl -u david.service -f
```

Once the bot is healthy, test one backup manually:

```bash
sudo systemctl start david-backup.service
sudo journalctl -u david-backup.service -n 200
```

Then enable the daily timer:

```bash
sudo systemctl start david-backup.timer
sudo systemctl status david-backup.timer
```

## Operational commands

```bash
# Service state
sudo systemctl status david.service
sudo systemctl status david-backup.service
sudo systemctl status david-backup.timer

# Logs
sudo journalctl -u david.service -f
sudo journalctl -u david-backup.service -n 200

# Restart the bot after a deploy
sudo systemctl restart david.service

# Run a backup immediately
sudo systemctl start david-backup.service
```

## Notes

- `david.service` uses an explicit entrypoint: `/opt/david/.venv/bin/python /opt/david/main.py`
- `/etc/david/david.env` stores env vars, not shell commands
- the backup timer runs daily with `Persistent=true`, so a missed run is caught up after the machine comes back
- mutable runtime state now lives under `/var/lib/david`, including the context files
