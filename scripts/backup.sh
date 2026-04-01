#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${DAVID_PROJECT_DIR:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
DB_PATH="${DAVID_DB_PATH:-$PROJECT_DIR/data/assistant.db}"
CONTEXT_DIR="${DAVID_CONTEXT_DIR:-$PROJECT_DIR/context}"
BACKUP_REMOTE="${DAVID_BACKUP_REMOTE:-}"
BACKUP_TMPDIR="${DAVID_BACKUP_TMPDIR:-/tmp/david-backups}"
BACKUP_HOST="${DAVID_BACKUP_HOST:-$(hostname -s)}"
TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
RUN_DIR="$BACKUP_TMPDIR/$TIMESTAMP"
ARCHIVE_BASENAME="david-backup-$BACKUP_HOST-$TIMESTAMP.tar.gz"
ARCHIVE_PATH="$RUN_DIR/$ARCHIVE_BASENAME"
REMOTE_PATH="${BACKUP_REMOTE%/}/$BACKUP_HOST/"

cleanup() {
  rm -rf "$RUN_DIR"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

if [[ -z "$BACKUP_REMOTE" ]]; then
  echo "DAVID_BACKUP_REMOTE must be set, for example b2:my-david-backups" >&2
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "SQLite database not found at $DB_PATH" >&2
  exit 1
fi

if [[ ! -d "$CONTEXT_DIR" ]]; then
  echo "Context directory not found at $CONTEXT_DIR" >&2
  exit 1
fi

require_command sqlite3
require_command tar
require_command rclone

mkdir -p "$RUN_DIR"
trap cleanup EXIT

sqlite3 "$DB_PATH" ".timeout 2000" ".backup $RUN_DIR/assistant.db"
cp -R "$CONTEXT_DIR" "$RUN_DIR/context"

tar -C "$RUN_DIR" -czf "$ARCHIVE_PATH" assistant.db context
rclone copy "$ARCHIVE_PATH" "$REMOTE_PATH"

echo "Uploaded $ARCHIVE_BASENAME to $REMOTE_PATH"
