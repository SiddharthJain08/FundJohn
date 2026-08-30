#!/bin/bash
# bench_flip_after_shadow.sh — guarded one-shot flip of the benchmark-relative
# sizing flags (spec docs/specs/2026-08-30-beta-budget-sizing-spec.md §5, D-5).
#
# Flips BOTH OPENCLAW_BENCH_RELATIVE_SIZING=1 and OPENCLAW_BENCH_BETA_BUDGET=1
# in .env and restarts the user-scope johnbot ONLY IF at least --min-cycles
# distinct daily-cycle logs since --since carry a CLEAN shadow line:
#   bench_sizing.shadow[<regime>]: S_m=<number> h=1 bench=['SPY'] … beta_usd_budget_capped=<n>
# and that day's log has no `bench_sizing: failed` / `flag ON but no
# net-direction-qualified` warning. Otherwise it posts why and leaves the flags
# alone (exit 0 — a "not yet" is not a unit failure). Never acts inside the
# weekday 13:00–20:15 UTC compute window.
#
# Usage:
#   scripts/bench_flip_after_shadow.sh            # check only, prints the verdict
#   scripts/bench_flip_after_shadow.sh --apply    # check, then flip + restart on success
#   --since YYYY-MM-DD (default 2026-08-31)  --min-cycles N (default 2)
#   --env-file PATH (default /root/openclaw/.env; use a copy to rehearse)
#   --no-restart (flip the file only)  --no-post (skip the Discord post)
# Armed as a transient timer: see docs/archive/changelog.md 2026-08-30 11:45 UTC.
set -uo pipefail
cd /root/openclaw || exit 2

APPLY=0; SINCE=2026-08-31; MIN=2; ENVF=/root/openclaw/.env; RESTART=1; POST=1
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;; --since) SINCE="$2"; shift;; --min-cycles) MIN="$2"; shift;;
    --env-file) ENVF="$2"; shift;; --no-restart) RESTART=0;; --no-post) POST=0;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
LOG=/root/openclaw/logs/bench_flip.log
ts() { date -u +%FT%TZ; }
say() { echo "[bench-flip $(ts)] $*" | tee -a "$LOG"; }

post_discord() {  # $1 = text; best-effort, never fails the script
  [ "$POST" = 1 ] || return 0
  POSTGRES_URI="$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2- | tr -d '"')" \
  BENCH_FLIP_TEXT="$1" python3 - <<'PY' 2>>"$LOG" || true
import json, os, urllib.request, psycopg2
text = os.environ['BENCH_FLIP_TEXT'][:1900]
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
    row = cur.fetchone()
if row and row[0]:
    req = urllib.request.Request(row[0], data=json.dumps({'content': text}).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-bench-flip/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
}

# --- compute-window guard (weekdays 13:00–20:15 UTC) -------------------------
dow=$(date -u +%u); hm=$(date -u +%H%M)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1300 ] && [ "$hm" -le 2015 ] && [ "$APPLY" = 1 ]; then
  say "refusing to flip inside the weekday compute window (UTC $hm)"; exit 0
fi

# --- gather the shadow lines ---------------------------------------------------
VERDICT="$(SINCE="$SINCE" MIN="$MIN" python3 - <<'PY'
import glob, os, re, sys
since, need = os.environ['SINCE'], int(os.environ['MIN'])
clean, dirty = [], []
for f in sorted(glob.glob('/root/openclaw/logs/daily_cycle_steps_*.log')):
    day = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(f)).group(1)
    if day < since:
        continue
    txt = open(f, errors='replace').read()
    lines = [l for l in txt.splitlines() if 'bench_sizing.shadow[' in l or 'bench_sizing.apply[' in l]
    if not lines:
        dirty.append((day, 'no bench_sizing line')); continue
    last = lines[-1]
    m = re.search(r'bench_sizing\.(shadow|apply)\[(\w+)\]: S_m=(\S+) h=(\d+) bench=(\[[^\]]*\]).*?beta_usd_budget=(\d+)(?: beta_usd_budget_capped=(\d+))?', last)
    reasons = []
    if not m:
        reasons.append('line does not parse: ' + last[-160:])
    else:
        mode, regime, s_m, h, bench, usd, capped = m.groups()
        if s_m == 'None': reasons.append('S_m=None')
        if h != '1': reasons.append(f'h={h}')
        if "'SPY'" not in bench: reasons.append(f'bench={bench}')
        if capped is None: reasons.append('no beta_usd_budget_capped field')
    if 'bench_sizing: failed' in txt: reasons.append('bench_sizing: failed present')
    if 'flag ON but no net-direction-qualified' in txt: reasons.append('no qualified benchmark ticker')
    (dirty if reasons else clean).append((day, '; '.join(reasons) if reasons else last.split(': ', 2)[-1][:220]))
ok = len(clean) >= need
print('OK' if ok else 'NOT_YET')
for d, r in clean: print(f'  clean {d}: {r}')
for d, r in dirty: print(f'  dirty {d}: {r}')
print(f'  clean={len(clean)} needed={need} since={since}')
PY
)"
STATUS="$(echo "$VERDICT" | head -1)"
say "verdict: $STATUS"; echo "$VERDICT" | tail -n +2 | tee -a "$LOG"

if [ "$STATUS" != "OK" ]; then
  post_discord "[bench-flip] NOT applied — shadow gate not met yet:
$(echo "$VERDICT" | tail -n +2 | head -12)"
  exit 0
fi
[ "$APPLY" = 1 ] || { say "check-only: gate MET; run with --apply to flip"; exit 0; }

# --- flip both flags in the env file -------------------------------------------
set_flag() {  # $1 key
  if grep -qE "^$1=" "$ENVF"; then sed -i -E "s|^$1=.*|$1=1|" "$ENVF"; else printf '\n%s=1\n' "$1" >> "$ENVF"; fi
}
cp -p "$ENVF" "$ENVF.bak.bench-flip.$(date -u +%Y%m%dT%H%M%SZ)"
set_flag OPENCLAW_BENCH_RELATIVE_SIZING
set_flag OPENCLAW_BENCH_BETA_BUDGET
say "flags set: $(grep -E '^OPENCLAW_BENCH_(RELATIVE_SIZING|BETA_BUDGET)=' "$ENVF" | tr '\n' ' ')"

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
post_discord "[bench-flip] APPLIED — OPENCLAW_BENCH_RELATIVE_SIZING=1 + OPENCLAW_BENCH_BETA_BUDGET=1 ($RESULT). Gate:
$(echo "$VERDICT" | tail -n +2 | head -6)
Next cycle: expect bench_sizing.apply[…] beta_budget=apply; SPY ≈ beta_usd_budget_capped; alpha trims at 15:55. Kill switch: OPENCLAW_BENCH_BETA_BUDGET=0 + user-scope johnbot restart."
exit 0
