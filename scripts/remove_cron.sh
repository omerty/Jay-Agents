#!/usr/bin/env bash
# Remove JayAgents daily cron job(s) so agents never auto-run.
# Usage: ./scripts/remove_cron.sh

set -euo pipefail

EXISTING="$(crontab -l 2>/dev/null || true)"
if ! printf '%s\n' "$EXISTING" | grep -q 'src\.daily'; then
  echo "No JayAgents src.daily cron entry found — already clear."
  exit 0
fi

printf '%s\n' "$EXISTING" | grep -v 'src\.daily' | crontab -
echo "Removed all crontab lines matching src.daily"
echo "Verify: crontab -l"
