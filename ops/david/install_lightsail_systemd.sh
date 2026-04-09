#!/usr/bin/env bash
set -euo pipefail

SYSTEMD_DIR="/etc/systemd/system"
DAVID_ETC_DIR="/etc/david"
APP_DIR="/opt/david"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root (use sudo)." >&2
  exit 1
fi

if [[ ! -f "${APP_DIR}/main.py" || ! -x "${APP_DIR}/scripts/backup.sh" ]]; then
  echo "Expected the deployed app at ${APP_DIR} before installing systemd units." >&2
  echo "Clone or sync this repository to ${APP_DIR}, run 'uv sync', then rerun this installer." >&2
  exit 1
fi

id -u david >/dev/null 2>&1 || useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin david
install -d -m 0755 "${APP_DIR}"
install -d -o david -g david -m 0755 /var/log/david /var/lib/david /var/lib/david/context
install -d -o root -g david -m 0750 "${DAVID_ETC_DIR}"
chown -R david:david /var/log/david /var/lib/david

install -m 0644 "${SRC_DIR}/david.service" "${SYSTEMD_DIR}/david.service"
install -m 0644 "${SRC_DIR}/david-backup.service" "${SYSTEMD_DIR}/david-backup.service"
install -m 0644 "${SRC_DIR}/david-backup.timer" "${SYSTEMD_DIR}/david-backup.timer"

if [[ ! -f "${DAVID_ETC_DIR}/david.env" ]]; then
  install -o root -g david -m 0640 "${SRC_DIR}/david.env.example" "${DAVID_ETC_DIR}/david.env"
  echo "Created ${DAVID_ETC_DIR}/david.env from template. Fill in the real secrets before starting services."
fi

systemctl daemon-reload
systemctl enable david.service
systemctl enable david-backup.timer

echo "Installed. Next steps:"
echo "  1) Edit /etc/david/david.env"
echo "  2) Install Google credentials at /etc/david/credentials.json"
echo "  3) (Recommended) install a pre-created token at /var/lib/david/token.json"
echo "  4) systemctl start david.service"
echo "  5) systemctl start david-backup.service"
echo "  6) systemctl start david-backup.timer"
