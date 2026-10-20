#!/bin/bash
# prices_hive_read_flip.sh — guarded one-shot flip of OPENCLAW_PRICES_HIVE_READ=1
# (readers move from the single prices.parquet to the hive-partitioned store; spec
# docs/specs/2026-10-19-six-repo-adoptions-spec.md §1 A3/A6, ruling R6). Modelled on
# scripts/pit_gap_flip_after_fleet.sh.
#
# Sets the flag in .env and restarts the user-scope johnbot ONLY IF all gates hold:
#   G0 .env already has OPENCLAW_PRICES_HIVE_READ=1 -> "already applied", disable own timer.
#   G1 .env has OPENCLAW_PRICES_HIVE=1 (the dual-write must be live).
#   G2 the parity log (scripts/prices_hive_parity.py via openclaw-prices-hive-parity.timer;
#      each run = an ISO "[...Z] prices_hive_parity run" line followed by a
#      "[prices_hive_parity] ... verdict=PASS|FAIL" line) holds >= --min-sessions PASS verdicts
#      on distinct UTC dates (the last PASS of each date counts), the most recent verdict is
#      PASS, and no verdict=FAIL appears after the earliest of the counted PASS lines.
#   G3 never inside the weekday 13:00-20:15 UTC compute window (--apply only).
# Otherwise it prints why and exits 0 ("not yet" is not a unit failure). Once applied it
# stops its own timer.
#
# Usage:
#   scripts/prices_hive_read_flip.sh            # check only, prints the verdict
#   scripts/prices_hive_read_flip.sh --apply    # check, then flip + restart on success
#   --env-file PATH (default /root/openclaw/.env)  --log PATH (default logs/prices_hive_read_flip.log)
#   --parity-log PATH (default /root/openclaw/logs/prices_hive_parity.log)  --min-sessions N (default 5)
#   --no-restart  --no-post  --timer-unit NAME (default openclaw-prices-hive-read-flip.timer)
# Test seam: PRICES_HIVE_FLIP_NOW="<ISO dow 1-7> <HHMM>" overrides the clock used by G3.
set -uo pipefail
cd /root/openclaw || exit 2

APPLY=0; ENVF=/root/openclaw/.env; RESTART=1; POST=1; MINSESS=5
TIMER_UNIT=openclaw-prices-hive-read-flip.timer
PARITY_LOG=/root/openclaw/logs/prices_hive_parity.log
LOG=/root/openclaw/logs/prices_hive_read_flip.log
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;; --env-file) ENVF="$2"; shift;; --log) LOG="$2"; shift;;
    --parity-log) PARITY_LOG="$2"; shift;; --min-sessions) MINSESS="$2"; shift;;
    --no-restart) RESTART=0;; --no-post) POST=0;; --timer-unit) TIMER_UNIT="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
ts() { date -u +%FT%TZ; }
say() { echo "[prices-hive-read-flip $(ts)] $*" | tee -a "$LOG"; }
PG_URI="$(grep -E '^POSTGRES_URI=' /root/openclaw/.env 2>/dev/null | cut -d= -f2- | tr -d '"')"

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
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-prices-hive-read-flip/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
}

# --- G0 already applied? ------------------------------------------------------
if grep -qE '^OPENCLAW_PRICES_HIVE_READ=1' "$ENVF"; then
  say "already applied (OPENCLAW_PRICES_HIVE_READ=1 set in $ENVF) — nothing to do"
  systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
  exit 0
fi

# --- G3 compute-window guard (weekdays 13:00–20:15 UTC) ----------------------
if [ -n "${PRICES_HIVE_FLIP_NOW:-}" ]; then dow=${PRICES_HIVE_FLIP_NOW%% *}; hm=${PRICES_HIVE_FLIP_NOW##* }
else dow=$(date -u +%u); hm=$(date -u +%H%M); fi
if [ "$dow" -le 5 ] && [ "$hm" -ge 1300 ] && [ "$hm" -le 2015 ] && [ "$APPLY" = 1 ]; then
  say "refusing to flip inside the weekday compute window (UTC $hm)"; exit 0
fi

# --- G1 dual-write live -------------------------------------------------------
if ! grep -qE '^OPENCLAW_PRICES_HIVE=1' "$ENVF"; then
  say "NOT_YET: G1 OPENCLAW_PRICES_HIVE=1 is not set in $ENVF (the dual-write must be live first)"
  exit 0
fi

# --- G2 parity history (read-only) -------------------------------------------
VERDICT="$(python3 - "$PARITY_LOG" "$MINSESS" <<'PY' 2>&1
import re, sys
path, need = sys.argv[1], int(sys.argv[2])
try:
    lines = open(path, errors='replace').read().splitlines()
except OSError as e:
    print(f"NOT_YET\nG2 parity log unreadable: {e}"); sys.exit(0)
cur = None          # UTC date of the most recent timestamp line
events = []         # (line_index, date, 'PASS'|'FAIL')
for i, ln in enumerate(lines):
    m = re.match(r'\[(\d{4}-\d{2}-\d{2})T[0-9:]+Z\]', ln)
    if m:
        cur = m.group(1); continue
    m = re.search(r'\[prices_hive_parity\].*\bverdict=(PASS|FAIL)\b', ln)
    if m:
        events.append((i, cur, m.group(1)))
if not events:
    print("NOT_YET\nG2 no verdict lines in the parity log"); sys.exit(0)
last_pass = {}      # date -> index of that date's last PASS
for i, d, v in events:
    if v == 'PASS' and d is not None:
        last_pass[d] = i
counted = sorted(last_pass.items())[-need:] if need > 0 else []
detail = [f"G2 PASS dates: {len(last_pass)} distinct (need {need}); verdicts total {len(events)}; latest {events[-1][2]}"]
ok = True
if len(last_pass) < need:
    ok = False; detail.append(f"G2 only {len(last_pass)} distinct-date PASS < {need}")
if events[-1][2] != 'PASS':
    ok = False; detail.append("G2 most recent verdict is FAIL")
if counted:
    first = min(i for _, i in counted)
    late_fail = [(d, i) for i, d, v in events if v == 'FAIL' and i > first]
    if late_fail:
        ok = False; detail.append(f"G2 verdict=FAIL after the earliest counted PASS (dates {sorted({d for d, _ in late_fail}, key=str)})")
print(("OK" if ok else "NOT_YET") + "\n" + "\n".join(detail))
PY
)"
STATUS="$(echo "$VERDICT" | head -1)"
say "verdict: $STATUS"; echo "$VERDICT" | tail -n +2 | tee -a "$LOG"

if [ "$STATUS" != "OK" ]; then
  post_discord "[prices-hive-read-flip] NOT applied — gate not met yet:
$(echo "$VERDICT" | tail -n +2 | head -12)"
  exit 0
fi
[ "$APPLY" = 1 ] || { say "check-only: gate MET; run with --apply to flip"; exit 0; }

# --- apply --------------------------------------------------------------------
cp -p "$ENVF" "$ENVF.bak.hive-read-flip.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -qE '^OPENCLAW_PRICES_HIVE_READ=' "$ENVF"; then sed -i -E 's|^OPENCLAW_PRICES_HIVE_READ=.*|OPENCLAW_PRICES_HIVE_READ=1|' "$ENVF"
else printf '\nOPENCLAW_PRICES_HIVE_READ=1\n' >> "$ENVF"; fi
say "flag set: $(grep -E '^OPENCLAW_PRICES_HIVE_READ=' "$ENVF" | tr '\n' ' ')"

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
post_discord "[prices-hive-read-flip] APPLIED — OPENCLAW_PRICES_HIVE_READ=1 ($RESULT). Gate:
$(echo "$VERDICT" | tail -n +2 | head -8)
Kill switch: OPENCLAW_PRICES_HIVE_READ=0 + user-scope johnbot restart (readers fall back to the single prices.parquet; hive_stale also falls back automatically)."
exit 0
