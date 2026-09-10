#!/bin/bash
# target_mode_flip_after_fleet.sh — guarded one-shot flip of OPENCLAW_TARGET_MODE=atr_r
# (BaseStrategy.compute_stops_and_targets target geometry, 2026-09-10).
#
# Flips OPENCLAW_TARGET_MODE=atr_r in .env, removes the fleet unit's temporary
# target-atr-r drop-in (so .env is the single source of truth again) and restarts
# the user-scope johnbot ONLY IF all gates hold (scripts/target_mode_flip_gate.py):
#   G1 fleet: every manifest state=live strategy's LATEST primary backtest row
#      carries config_json.target_mode='atr_r' (<= --max-lagging exceptions).
#   G2 sharpe: median (atr_r - flat) Sharpe >= --min-median-dsharpe and the
#      positive-Sharpe count under atr_r >= --min-positive-frac x the flat count.
#   G3 never inside the weekday 13:00–20:15 UTC compute window.
# Otherwise it posts why and leaves everything alone (exit 0 — "not yet" is not
# a unit failure). Once applied it stops its own timer.
#
# Usage:
#   scripts/target_mode_flip_after_fleet.sh            # check only, prints the verdict
#   scripts/target_mode_flip_after_fleet.sh --apply    # check, then flip + restart on success
#   --max-lagging N (default 3)  --min-median-dsharpe X (default -0.10)
#   --min-positive-frac F (default 0.90)  --env-file PATH (default /root/openclaw/.env)
#   --no-restart  --no-post  --timer-unit NAME (default openclaw-target-mode-flip.timer)
set -uo pipefail
cd /root/openclaw || exit 2

APPLY=0; MAXLAG=3; MINMED=-0.10; MINPOS=0.90; ENVF=/root/openclaw/.env; RESTART=1; POST=1
TIMER_UNIT=openclaw-target-mode-flip.timer
DROPIN=/etc/systemd/system/openclaw-fleet-overnight-resume.service.d/target-atr-r.conf
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;; --max-lagging) MAXLAG="$2"; shift;; --min-median-dsharpe) MINMED="$2"; shift;;
    --min-positive-frac) MINPOS="$2"; shift;; --env-file) ENVF="$2"; shift;;
    --no-restart) RESTART=0;; --no-post) POST=0;; --timer-unit) TIMER_UNIT="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
LOG=/root/openclaw/logs/target_mode_flip.log
ts() { date -u +%FT%TZ; }
say() { echo "[target-flip $(ts)] $*" | tee -a "$LOG"; }
PG_URI="$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2- | tr -d '"')"

post_discord() {  # $1 = text; best-effort, never fails the script
  [ "$POST" = 1 ] || return 0
  POSTGRES_URI="$PG_URI" FLIP_TEXT="$1" python3 - <<'PY' 2>>"$LOG" || true
import json, os, urllib.request, psycopg2
text = os.environ['FLIP_TEXT'][:1900]
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
    row = cur.fetchone()
if row and row[0]:
    req = urllib.request.Request(row[0], data=json.dumps({'content': text}).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-target-flip/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
}

# --- already applied? ---------------------------------------------------------
if grep -qE '^OPENCLAW_TARGET_MODE=atr_r' "$ENVF"; then
  say "already applied (OPENCLAW_TARGET_MODE=atr_r in $ENVF) — nothing to do"
  systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
  exit 0
fi

# --- G3 compute-window guard (weekdays 13:00–20:15 UTC) ----------------------
dow=$(date -u +%u); hm=$(date -u +%H%M)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1300 ] && [ "$hm" -le 2015 ] && [ "$APPLY" = 1 ]; then
  say "refusing to flip inside the weekday compute window (UTC $hm)"; exit 0
fi

# --- G1 + G2 (read-only) ------------------------------------------------------
VERDICT="$(POSTGRES_URI="$PG_URI" python3 scripts/target_mode_flip_gate.py \
             --max-lagging "$MAXLAG" --min-median-dsharpe "$MINMED" --min-positive-frac "$MINPOS" 2>&1)"
STATUS="$(echo "$VERDICT" | head -1)"
say "verdict: $STATUS"; echo "$VERDICT" | tail -n +2 | tee -a "$LOG"

if [ "$STATUS" != "OK" ]; then
  post_discord "[target-flip] NOT applied — gate not met yet:
$(echo "$VERDICT" | tail -n +2 | head -12)"
  exit 0
fi
[ "$APPLY" = 1 ] || { say "check-only: gate MET; run with --apply to flip"; exit 0; }

# --- apply --------------------------------------------------------------------
cp -p "$ENVF" "$ENVF.bak.target-flip.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -qE '^OPENCLAW_TARGET_MODE=' "$ENVF"; then sed -i -E 's|^OPENCLAW_TARGET_MODE=.*|OPENCLAW_TARGET_MODE=atr_r|' "$ENVF"
else printf '\nOPENCLAW_TARGET_MODE=atr_r\n' >> "$ENVF"; fi
say "flag set: $(grep -E '^OPENCLAW_TARGET_MODE=' "$ENVF")"
if [ -f "$DROPIN" ]; then
  rm -f "$DROPIN" && systemctl daemon-reload && say "removed fleet drop-in $DROPIN (.env is now the single source)"
fi

RESULT="flag flipped"
if [ "$RESTART" = 1 ]; then
  if XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service; then
    sleep 5; ST=$(XDG_RUNTIME_DIR=/run/user/0 systemctl --user is-active johnbot.service)
    RESULT="$RESULT; johnbot restarted ($ST)"
  else
    RESULT="$RESULT; johnbot restart FAILED — restart manually: XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot"
  fi
fi
say "$RESULT"
systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
post_discord "[target-flip] APPLIED — OPENCLAW_TARGET_MODE=atr_r ($RESULT). Gate:
$(echo "$VERDICT" | tail -n +2 | head -8)
Next: live brackets = R-multiples of the ATR stop from the next 15:00 ET cycle. OWED: canonical sequence (weights rebuild -> floor recheck -> activation) on the atr_r rows. Kill switch: OPENCLAW_TARGET_MODE=flat + user-scope johnbot restart."
exit 0
