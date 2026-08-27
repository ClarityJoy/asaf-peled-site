#!/usr/bin/env bash
#
# Nightly job-digest run, invoked from cron.
#
# Cron fires at a fixed minute, so every job on the machine starts at the same
# instant every day. This script sleeps a random interval first, so the actual
# request to any board lands at a different time each night. Firing at exactly
# 06:40:00 daily is a pattern; 06:40 plus nought to nine minutes is not.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/jobdigest"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/run-$(date +%Y-%m-%d).log"
MAX_JITTER_SECONDS="${JOBDIGEST_MAX_JITTER:-540}"   # 9 minutes

mkdir -p "$LOG_DIR"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "$(date -Is) FATAL: $VENV_PYTHON not found. Run: python3 -m venv .venv && .venv/bin/pip install -e ." \
        | tee -a "$LOG_FILE" >&2
    exit 1
fi

# Skip the jitter when a human is running this by hand.
if [[ -t 1 ]]; then
    echo "$(date -Is) interactive, skipping jitter" | tee -a "$LOG_FILE"
else
    JITTER=$((RANDOM % (MAX_JITTER_SECONDS + 1)))
    echo "$(date -Is) sleeping ${JITTER}s before starting" >> "$LOG_FILE"
    sleep "$JITTER"
fi

# Only one run at a time. If yesterday's somehow hung, do not start a second
# one alongside it -- no parallelism against LinkedIn, ever.
LOCK_FILE="$PROJECT_DIR/.run.lock"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        echo "$(date -Is) another run is already in progress, exiting" >> "$LOG_FILE"
        exit 0
    fi
fi

echo "$(date -Is) starting run" >> "$LOG_FILE"
"$VENV_PYTHON" "$@" >> "$LOG_FILE" 2>&1
STATUS=$?
echo "$(date -Is) finished with exit code $STATUS" >> "$LOG_FILE"

# Exit codes: 0 fine, 1 every source failed, 3 a board blocked us.
case "$STATUS" in
    3) echo "$(date -Is) NOTE: run was aborted by a rate limit or challenge. \
Not retrying today." >> "$LOG_FILE" ;;
    1) echo "$(date -Is) NOTE: the run produced nothing usable. See the output above." >> "$LOG_FILE" ;;
esac

# Keep a month of logs.
find "$LOG_DIR" -name 'run-*.log' -type f -mtime +31 -delete 2>/dev/null || true
exit "$STATUS"
