# David on AWS Lightsail

This folder is the canonical `systemd` deployment path for running David on a Lightsail instance.

## What lives here

- `david.service`: the main long-running bot service
- `david-backup.service`: a one-shot backup job
- `david-backup.timer`: the daily schedule for backups
- `david.env.example`: the production env template loaded by both services
- `install_lightsail_systemd.sh`: installer for the units and the `david` service user
- `Caddyfile`: optional HTTPS reverse proxy for the Telegram context editor

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
   uv sync --locked
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

## Telegram context editor over HTTPS

The Mini App edits the live files in `DAVID_CONTEXT_DIR`. Backblaze remains the
daily backup destination. Send `/edit_context` in a private chat with David to
open Markdown editing, preview, history, and restore. `/context` still reports
model context usage.

1. Attach a [Lightsail static public IPv4 address](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-create-static-ip.html).
   A private address such as `172.26.15.68` cannot host a public Telegram Mini App.
2. Choose an owned hostname with an A record pointing to that public IP, or use
   `<public-IP>.sslip.io`. [sslip.io](https://sslip.io/) resolves embedded IP
   addresses and supports ordinary HTTPS certificates without buying a domain.
3. Allow TCP 80 and 443 in the Lightsail firewall and any Ubuntu firewall.
   Keep port 8080 closed publicly; David binds the API to `127.0.0.1`.
4. Install Caddy using its [official Ubuntu instructions](https://caddyserver.com/docs/install#debian-ubuntu-raspbian).
5. Set the chosen HTTPS URL in `/etc/david/david.env`:

   ```dotenv
   DAVID_CONTEXT_EDITOR_URL="https://YOUR-HOSTNAME/context"
   DAVID_CONTEXT_EDITOR_PORT="8080"
   ```

6. If Caddy already serves another application, merge the provided site into its
   configuration. On a new Caddy installation, copy the template:

   ```bash
   sudo install -m 644 /opt/david/ops/david/Caddyfile /etc/caddy/Caddyfile
   sudo install -d -m 755 /etc/systemd/system/caddy.service.d
   sudoedit /etc/systemd/system/caddy.service.d/david-editor.conf
   ```

   Save this service override with the same hostname and port:

   ```ini
   [Service]
   Environment=DAVID_EDITOR_HOST=YOUR-HOSTNAME
   Environment=DAVID_EDITOR_PORT=8080
   ```

7. Validate Caddy with those values, then restart both services:

   ```bash
   sudo env DAVID_EDITOR_HOST=YOUR-HOSTNAME DAVID_EDITOR_PORT=8080 \
     caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   sudo systemctl daemon-reload
   sudo systemctl restart david.service
   sudo systemctl restart caddy.service
   ```

   Caddy obtains and renews the certificate automatically. Its HTTPS setup
   requires DNS to point to the instance and external access to ports 80/443.
   See [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).
8. Verify from a computer outside Lightsail:

   ```bash
   curl --fail --silent --show-error https://YOUR-HOSTNAME/context -o /dev/null
   curl --silent --show-error -o /dev/null -w '%{http_code}\n' \
     https://YOUR-HOSTNAME/api/context
   ```

   The page must return 200 with a trusted certificate. The API must return 401
   without Telegram authorization. Do not use `curl -k` to bypass certificate checks.
9. In Telegram, send `/edit_context`, change one document, save, reload, and
   verify History contains its previous text. Restore that version if the edit
   was only a smoke test. The button supplies signed Telegram `initData`; no
   separate password is needed. Reopen the Mini App if authorization expires.

Saves require the loaded revision and a stable operation UUID. Concurrent edits
show a conflict instead of overwriting newer context. The bot, review workflow,
and editor share a process-local write lock; do not run a second writer process
or edit context files externally while saving. Context files and backups remain
private on disk. HTTP access logs are disabled, and editor error telemetry is
suppressed to keep request credentials and drafts out of Sentry.

To disable the editor, remove `DAVID_CONTEXT_EDITOR_URL` and restart David.
The bot continues operating with its existing context files. Remove the Caddy
site separately if its public endpoint is no longer needed.

## Verify the editor locally

1. Run `uv sync --locked`.
2. Run `uv run --locked pytest -q` for the full Python suite.
3. Install the browser test runtime outside the checkout:

   ```bash
   npm install --prefix /tmp/david-editor-browser --no-save --package-lock=false playwright@1.63.0
   /tmp/david-editor-browser/node_modules/.bin/playwright install chromium
   ```

4. Run the permanent browser checks:

   ```bash
   PLAYWRIGHT_MODULE_PATH=/tmp/david-editor-browser/node_modules/playwright \
     node scripts/test_context_editor_browser.cjs
   ```

   The script starts the real API with temporary files and synthetic signed
   Telegram credentials. It exercises Save, Cancel, reload, conflicts, retries
   after a lost response, history restore, mobile layout, and preview safety.
   It fetches and verifies the pinned preview assets. It never starts Telegram
   polling or writes your live context. CI runs it before production deployment.
   `PYTHON_BIN` can select another virtualenv; `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`
   can select an existing Chrome executable.

## Notes

- `david.service` uses an explicit entrypoint: `/opt/david/.venv/bin/python /opt/david/main.py`
- `/etc/david/david.env` stores env vars, not shell commands
- the backup timer runs daily with `Persistent=true`, so a missed run is caught up after the machine comes back
- mutable runtime state now lives under `/var/lib/david`, including the context files
