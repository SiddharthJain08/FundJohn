#!/usr/bin/env bash
# Retry-into-the-week for the weekend research units (operator directive
# 2026-08-10): a failed run (OOM, timeout, crash) re-arms itself instead of
# dying silently until someone notices the following weekend — the Saturday
# 2026-08-08 finisher OOM sat unnoticed until Monday.
#
#   - max 3 retries per unit per weekend cycle (4-day sliding window)
#   - retry lands +2h; if that falls inside RTH (13:00-20:00 UTC weekday) it
#     is pushed to 20:30 UTC the same day so a heavy re-run never fights the
#     live trading lanes for the 8GB box
#   - the armed timer is transient (does not survive reboot) — the next
#     weekend's scheduled timer is the ultimate backstop, and the existing
#     openclaw-failure-notify@ OnFailure hook posts on every failure either way
#
# Invoked by openclaw-research-retry@<unit>.service via each research unit's
# OnFailure= (guards.conf drop-in). Runs as root (needs systemctl start).
set -euo pipefail

UNIT="$1"
STATE_DIR=/var/lib/openclaw/research-retries
mkdir -p "$STATE_DIR"
F="$STATE_DIR/${UNIT}.log"
NOW=$(date -u +%s)

# Prune retry stamps older than one weekend cycle so last week's attempts
# never exhaust this week's budget.
if [[ -f "$F" ]]; then
  awk -v cutoff=$((NOW - 4*86400)) '$1 >= cutoff' "$F" > "$F.tmp" && mv "$F.tmp" "$F"
fi
COUNT=0
[[ -f "$F" ]] && COUNT=$(wc -l < "$F")
if (( COUNT >= 3 )); then
  echo "research-retry: ${UNIT} exhausted (${COUNT} retries in 4d) — giving up until the next scheduled run"
  exit 0
fi
echo "${NOW} retry-$((COUNT+1))" >> "$F"

DELAY=7200
TARGET=$((NOW + DELAY))
DOW=$(date -u -d "@${TARGET}" +%u)
HOUR=$(date -u -d "@${TARGET}" +%H)
if (( DOW <= 5 )) && (( 10#$HOUR >= 13 )) && (( 10#$HOUR < 20 )); then
  TARGET=$(date -u -d "$(date -u -d "@${TARGET}" +%F) 20:30:00" +%s)
fi
WHEN=$(date -u -d "@${TARGET}" '+%Y-%m-%d %H:%M:%S UTC')

echo "research-retry: arming retry $((COUNT+1))/3 for ${UNIT} at ${WHEN}"
systemd-run --collect --unit="research-retry-fire-$(date -u +%s)" \
  --on-calendar="${WHEN}" /usr/bin/systemctl start "${UNIT}"
