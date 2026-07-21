#!/usr/bin/env bash
# Install (or update) the JayAgents daily cron job.
# Usage: ./scripts/setup_cron.sh [HOUR]   — default HOUR=0 (midnight local time)

set -euo pipefail

HOUR="${1:-0}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"

if [ ! -x "$PYTHON" ]; then
  echo "error: $PYTHON not found — create the venv first (see README Setup)" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

CRON_CMD="cd $PROJECT_DIR && $PYTHON -m src.daily >> $LOG_DIR/daily.log 2>&1"
CRON_LINE="0 $HOUR * * * $CRON_CMD"

# Replace any existing src.daily entry, keep everything else
( crontab -l 2>/dev/null | grep -v 'src\.daily' ; echo "$CRON_LINE" ) | crontab -

echo "Installed cron job — all agents (Woodway, FONEX, Keira) run daily at $(printf '%02d' "$HOUR"):00 local time"
echo "  $CRON_LINE"
echo
echo "Logs:    $LOG_DIR/daily.log"
echo "Test it: $PYTHON -m src.daily"
echo "Remove:  crontab -e   (delete the src.daily line)"
