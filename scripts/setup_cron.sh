#!/usr/bin/env bash
# Install (or update) the JayAgents daily cron job.
#
# AUTO-RUN IS DISABLED BY DEFAULT for Woodway + Keira.
# Even if you install cron, daily.py skips those agents unless:
#   DAILY_RUN_WOODWAY=true  and/or  DAILY_RUN_KEIRA=true
#
# Prefer manual pipelines for now:
#   python -m src.woodway_pipeline
#   python -m src.keira_pipeline
#
# Usage:
#   ./scripts/setup_cron.sh            # refuses unless FORCE_CRON_INSTALL=1
#   FORCE_CRON_INSTALL=1 ./scripts/setup_cron.sh [HOUR]
# Remove with: ./scripts/remove_cron.sh

set -euo pipefail

if [ "${FORCE_CRON_INSTALL:-}" != "1" ]; then
  echo "Cron install blocked — Woodway/Keira must not auto-run right now."
  echo "  Remove any existing job:  ./scripts/remove_cron.sh"
  echo "  Force install (not recommended): FORCE_CRON_INSTALL=1 $0 [HOUR]"
  echo "  Note: even with cron, set DAILY_RUN_WOODWAY=true / DAILY_RUN_KEIRA=true to enable those agents."
  exit 1
fi

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

echo "Installed cron job at $(printf '%02d' "$HOUR"):00 local time"
echo "  $CRON_LINE"
echo
echo "Woodway/Keira still skip unless DAILY_RUN_WOODWAY / DAILY_RUN_KEIRA are true in .env"
echo "Logs:    $LOG_DIR/daily.log"
echo "Remove:  ./scripts/remove_cron.sh"
