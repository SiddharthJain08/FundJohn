#!/bin/bash
# pit_gap_flip_after_fleet.sh — guarded one-shot flip of OPENCLAW_FINANCIALS_PIT=1
# and OPENCLAW_BT_GAP_FILL=open (spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md
# §A1/§A2, epoch §A4). Modelled line-for-line on scripts/target_mode_flip_after_fleet.sh.
#
# Flips BOTH flags in .env, removes the fleet unit's temporary pit-gap drop-in
# (so .env is the single source of truth again) and restarts the user-scope
# johnbot ONLY IF all gates hold:
#   G1 fleet: every manifest state=live strategy's LATEST primary backtest row
#      carries config_json.gap_fill='open' AND config_json.financials_pit=true
#      (<= --max-lagging exceptions) — scripts/pit_gap_flip_gate.py.
#   G3 never inside the weekday 13:00–20:15 UTC compute window.
# There is no Sharpe gate on purpose: ruling R1 accepts PIT-driven Sharpe
# reductions and demotions up front.
# Otherwise it posts why and leaves everything alone (exit 0 — "not yet" is not
# a unit failure). Once applied it stops its own timer.
#
# Usage:
#   scripts/pit_gap_flip_after_fleet.sh            # check only, prints the verdict
#   scripts/pit_gap_flip_after_fleet.sh --apply    # check, then flip + restart on success
#   --max-lagging N (default 3)  --env-file PATH (default /root/openclaw/.env)
#   --no-restart  --no-post  --timer-unit NAME (default openclaw-pit-gap-flip.timer)
set -uo pipefail
cd /root/openclaw || exit 2

APPLY=0; MAXLAG=3; ENVF=/root/openclaw/.env; RESTART=1; POST=1
TIMER_UNIT=openclaw-pit-gap-flip.timer
DROPIN=/etc/systemd/system/openclaw-fleet-overnight-resume.service.d/pit-gap.conf
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;; --max-lagging) MAXLAG="$2"; shift;;
    --env-file) ENVF="$2"; shift;;
    --no-restart) RESTART=0;; --no-post) POST=0;; --timer-unit) TIMER_UNIT="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
LOG=/root/openclaw/logs/pit_gap_flip.log
ts() { date -u +%FT%TZ; }
say() { echo "[pit-gap-flip $(ts)] $*" | tee -a "$LOG"; }
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
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-pit-gap-flip/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
}

# --- already applied? ---------------------------------------------------------
if grep -qE '^OPENCLAW_BT_GAP_FILL=open' "$ENVF" && grep -qE '^OPENCLAW_FINANCIALS_PIT=1' "$ENVF"; then
  say "already applied (both flags set in $ENVF) — nothing to do"
  systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
  exit 0
fi

# --- G3 compute-window guard (weekdays 13:00–20:15 UTC) ----------------------
dow=$(date -u +%u); hm=$(date -u +%H%M)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1300 ] && [ "$hm" -le 2015 ] && [ "$APPLY" = 1 ]; then
  say "refusing to flip inside the weekday compute window (UTC $hm)"; exit 0
fi

# --- G1 (read-only) -----------------------------------------------------------
VERDICT="$(POSTGRES_URI="$PG_URI" python3 scripts/pit_gap_flip_gate.py --max-lagging "$MAXLAG" 2>&1)"
STATUS="$(echo "$VERDICT" | head -1)"
say "verdict: $STATUS"; echo "$VERDICT" | tail -n +2 | tee -a "$LOG"

if [ "$STATUS" != "OK" ]; then
  post_discord "[pit-gap-flip] NOT applied — gate not met yet:
$(echo "$VERDICT" | tail -n +2 | head -12)"
  exit 0
fi
[ "$APPLY" = 1 ] || { say "check-only: gate MET; run with --apply to flip"; exit 0; }

# --- apply --------------------------------------------------------------------
cp -p "$ENVF" "$ENVF.bak.pit-gap-flip.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -qE '^OPENCLAW_FINANCIALS_PIT=' "$ENVF"; then sed -i -E 's|^OPENCLAW_FINANCIALS_PIT=.*|OPENCLAW_FINANCIALS_PIT=1|' "$ENVF"
else printf '\nOPENCLAW_FINANCIALS_PIT=1\n' >> "$ENVF"; fi
if grep -qE '^OPENCLAW_BT_GAP_FILL=' "$ENVF"; then sed -i -E 's|^OPENCLAW_BT_GAP_FILL=.*|OPENCLAW_BT_GAP_FILL=open|' "$ENVF"
else printf 'OPENCLAW_BT_GAP_FILL=open\n' >> "$ENVF"; fi
say "flags set: $(grep -E '^OPENCLAW_(FINANCIALS_PIT|BT_GAP_FILL)=' "$ENVF" | tr '\n' ' ')"
if [ -f "$DROPIN" ]; then
  rm -f "$DROPIN" && systemctl daemon-reload && say "removed fleet drop-in $DROPIN (.env is now the single source)"
fi

RESULT="flags flipped"
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
post_discord "[pit-gap-flip] APPLIED — OPENCLAW_FINANCIALS_PIT=1 + OPENCLAW_BT_GAP_FILL=open ($RESULT). Gate:
$(echo "$VERDICT" | tail -n +2 | head -8)
Next: live aux['financials'] is unchanged (engine.py already sees filed data only); the backtest stops reading unfiled quarters and stops protecting stops against gaps. OWED: canonical sequence (weights rebuild -> floor recheck -> activation). Kill switch: OPENCLAW_FINANCIALS_PIT=0 + OPENCLAW_BT_GAP_FILL=level + user-scope johnbot restart."
exit 0
