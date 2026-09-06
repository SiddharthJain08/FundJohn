#!/usr/bin/env bash
# scripts/fleet_weekend_window.sh — one bounded weekend/holiday window of the
# resumable fleet re-backtest driver (scripts/refresh_backtests_resumable.js).
#
# The nightly unit (openclaw-fleet-overnight-resume, Mon–Fri 21:30 UTC) only
# covers weeknights; a fresh epoch (~128 strategies × ~25 min ≈ 53 h serial)
# needs the weekend too. This wrapper is meant to run as a transient unit:
#
#   systemd-run --on-calendar="2026-09-06 08:05:00 UTC" --unit=fleet-rf-epoch-20260906 \
#     -p Nice=19 -p OOMPolicy=continue -p RuntimeMaxSec=<seconds to the hard stop> \
#     -p EnvironmentFile=/root/openclaw/.env -p WorkingDirectory=/root/openclaw \
#     --setenv=NUMEXPR_MAX_THREADS=1 --setenv=NUMEXPR_NUM_THREADS=1 \
#     --setenv=OPENCLAW_RF_SOURCE=macro \
#     /bin/bash /root/openclaw/scripts/fleet_weekend_window.sh \
#       --deadline 2026-09-07T10:30 --wait-unit options-surface-rollout-20260906.service
#
# --deadline   UTC YYYY-MM-DDTHH:MM — no NEW strategy is spawned after it (the
#              unit's RuntimeMaxSec is the real wall; keep it ≥ deadline + the
#              per-strategy timeout, or set it just past the deadline and accept
#              one in-flight kill — post-write kills are salvaged by the driver).
# --wait-unit  systemd unit that must be inactive before we start (never co-run
#              two heavy jobs on this 2-core / 8 GB box); polled every 60 s for
#              up to --wait-max seconds (default 4 h), then we proceed anyway
#              with a WARN line so the log says so.
# Same guard as /root/fleet_overnight_resume.sh: never two drivers at once.
set -u
cd /root/openclaw || exit 1
LOG=/root/openclaw/logs/fleet_overnight_resume.log
DEADLINE=""; WAIT_UNIT=""; WAIT_MAX=14400; PER_TIMEOUT=14400
while [ $# -gt 0 ]; do
  case "$1" in
    --deadline) DEADLINE="$2"; shift;;
    --wait-unit) WAIT_UNIT="$2"; shift;;
    --wait-max) WAIT_MAX="$2"; shift;;
    --per-timeout) PER_TIMEOUT="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
[ -n "$DEADLINE" ] || { echo "--deadline required" >&2; exit 2; }
say() { echo "[weekend $(date -u +%FT%TZ)] $*" >> "$LOG"; }

if pgrep -a -x node 2>/dev/null | grep -q 'refresh_backtests_resumable\.js'; then
  say "a fleet driver is already running — skipping this window"; exit 0
fi
if [ -n "$WAIT_UNIT" ]; then
  waited=0
  while systemctl is-active --quiet "$WAIT_UNIT"; do
    if [ "$waited" -ge "$WAIT_MAX" ]; then
      say "WARN $WAIT_UNIT still active after ${waited}s — proceeding anyway"; break
    fi
    [ "$waited" -eq 0 ] && say "waiting for $WAIT_UNIT to finish"
    sleep 60; waited=$((waited + 60))
  done
  [ "$waited" -gt 0 ] && say "$WAIT_UNIT inactive after ${waited}s"
fi

say "start (pid $$); --deadline ${DEADLINE}Z --per-timeout ${PER_TIMEOUT} rf_source=${OPENCLAW_RF_SOURCE:-unset}"
node scripts/refresh_backtests_resumable.js --resume \
  --deadline "$DEADLINE" --per-timeout "$PER_TIMEOUT" --mem-floor 4500 --mem-wait 3600 >> "$LOG" 2>&1
rc=$?
OUT=$(grep -oE '[0-9]+ strategies still outstanding' "$LOG" | tail -1 | grep -oE '^[0-9]+')
say "exit rc=$rc; outstanding=${OUT:-unknown} (compute only — no weights rebuild, no activation)"
exit 0
