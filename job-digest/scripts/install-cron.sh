#!/usr/bin/env bash
#
# Install (or update) the daily crontab entry. Safe to re-run.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$PROJECT_DIR/scripts/run-daily.sh"
HOUR="${JOBDIGEST_HOUR:-6}"
MINUTE="${JOBDIGEST_MINUTE:-40}"
MARKER="# job-digest daily run"

if [[ ! -x "$RUNNER" ]]; then
    chmod +x "$RUNNER"
fi

ENTRY="$MINUTE $HOUR * * * $RUNNER $MARKER"

# Drop any previous entry of ours, keep everything else untouched.
EXISTING="$(crontab -l 2>/dev/null | grep -v -F "$MARKER" || true)"
printf '%s\n%s\n' "$EXISTING" "$ENTRY" | grep -v '^$' | crontab -

echo "Installed:"
echo "  $ENTRY"
echo
echo "Cron uses this machine's local time. Current local time: $(date)"
echo "The run itself sleeps a random 0-9 minutes after firing, so the actual"
echo "request time varies night to night."
echo
echo "Verify with:  crontab -l"
echo "Logs land in: $PROJECT_DIR/logs/"
