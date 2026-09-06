#!/bin/bash
# rf_flip_after_fleet.sh — guarded one-shot flip of OPENCLAW_RF_SOURCE=macro
# (spec docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md Part C;
# runbook docs/runbooks/2026-09-04-options-surface-rollout.md "Flags").
#
# Flips OPENCLAW_RF_SOURCE=macro in .env, removes the fleet unit's temporary
# rf-macro drop-in (so .env is the single source of truth again) and restarts
# the user-scope johnbot ONLY IF all three gates hold:
#   G1 fleet: every manifest state=live strategy's LATEST primary backtest row
#      carries config_json.rf.source='macro' (at most --max-lagging exceptions,
#      default 3 — a strategy whose backtest OOMs every night must not hold the
#      flip hostage; the exceptions are listed in the verdict).
#   G2 live shadow: at least --min-lines `[rf_shadow]` lines from the daily
#      cycle logs since --since (default 2026-09-08, the first post-Labor-Day
#      session), spanning >= 2 distinct days, every one parseable with a finite
#      const AND macro Sharpe and |const - macro| <= 0.5 (the 20-day book/SPY
#      Sharpe moves by (0.05 - rf)/vol — anything larger is a parsing/data fault).
#   G3 never inside the weekday 13:00–20:15 UTC compute window.
# Otherwise it posts why and leaves everything alone (exit 0 — "not yet" is not
# a unit failure). Once applied it stops its own transient timer.
#
# Usage:
#   scripts/rf_flip_after_fleet.sh            # check only, prints the verdict
#   scripts/rf_flip_after_fleet.sh --apply    # check, then flip + restart on success
#   --since YYYY-MM-DD  --min-lines N (default 5)  --max-lagging N (default 3)
#   --env-file PATH (default /root/openclaw/.env; use a copy to rehearse)
#   --no-restart  --no-post  --timer-unit NAME (default rf-flip-after-fleet.timer)
set -uo pipefail
cd /root/openclaw || exit 2

APPLY=0; SINCE=2026-09-08; MIN=5; MAXLAG=3; ENVF=/root/openclaw/.env; RESTART=1; POST=1
TIMER_UNIT=rf-flip-after-fleet.timer
DROPIN=/etc/systemd/system/openclaw-fleet-overnight-resume.service.d/rf-macro.conf
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;; --since) SINCE="$2"; shift;; --min-lines) MIN="$2"; shift;;
    --max-lagging) MAXLAG="$2"; shift;; --env-file) ENVF="$2"; shift;;
    --no-restart) RESTART=0;; --no-post) POST=0;; --timer-unit) TIMER_UNIT="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
LOG=/root/openclaw/logs/rf_flip.log
ts() { date -u +%FT%TZ; }
say() { echo "[rf-flip $(ts)] $*" | tee -a "$LOG"; }
PG_URI="$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2- | tr -d '"')"

post_discord() {  # $1 = text; best-effort, never fails the script
  [ "$POST" = 1 ] || return 0
  POSTGRES_URI="$PG_URI" RF_FLIP_TEXT="$1" python3 - <<'PY' 2>>"$LOG" || true
import json, os, urllib.request, psycopg2
text = os.environ['RF_FLIP_TEXT'][:1900]
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
    row = cur.fetchone()
if row and row[0]:
    req = urllib.request.Request(row[0], data=json.dumps({'content': text}).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-rf-flip/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
}

# --- already applied? ---------------------------------------------------------
if grep -qE '^OPENCLAW_RF_SOURCE=macro' "$ENVF"; then
  say "already applied (OPENCLAW_RF_SOURCE=macro in $ENVF) — nothing to do"
  systemctl stop "$TIMER_UNIT" 2>/dev/null || true
  exit 0
fi

# --- G3 compute-window guard (weekdays 13:00–20:15 UTC) ----------------------
dow=$(date -u +%u); hm=$(date -u +%H%M)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1300 ] && [ "$hm" -le 2015 ] && [ "$APPLY" = 1 ]; then
  say "refusing to flip inside the weekday compute window (UTC $hm)"; exit 0
fi

# --- G1 + G2 ------------------------------------------------------------------
VERDICT="$(POSTGRES_URI="$PG_URI" SINCE="$SINCE" MIN="$MIN" MAXLAG="$MAXLAG" python3 - <<'PY'
import glob, json, os, re, math, psycopg2
since, need, maxlag = os.environ['SINCE'], int(os.environ['MIN']), int(os.environ['MAXLAG'])
out, ok = [], True
# G1 — fleet uniformity for the live tier
m = json.load(open('/root/openclaw/src/strategies/manifest.json'))['strategies']
live = sorted(k for k, v in m.items() if v.get('state') == 'live')
latest = {}
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("""SELECT DISTINCT ON (strategy_id) strategy_id, config_json->'rf'->>'source', run_at
                   FROM strategy_backtest_runs WHERE primary_window=true
                   ORDER BY strategy_id, run_at DESC""")
    for sid, src, run_at in cur.fetchall():
        latest[sid] = (src, run_at)
lagging = [s for s in live if latest.get(s, (None, None))[0] != 'macro']
g1 = len(lagging) <= maxlag
ok &= g1
out.append(f"  G1 fleet: live={len(live)} on_macro={len(live)-len(lagging)} lagging={len(lagging)} (max {maxlag}) -> {'OK' if g1 else 'NOT_YET'}")
if lagging:
    out.append('    lagging: ' + ', '.join(lagging[:12]) + (' …' if len(lagging) > 12 else ''))
# G2 — live shadow lines
pat = re.compile(r'\[rf_shadow\] site=(\S+)(?: source=\S+)?(?: regime=\S+ h=\d+)? const=(\S+) macro=(\S+) n=(\d+)(?: rf_mean=(\S+))?')
clean, dirty, days = [], [], set()
seen = set()  # (day, canonical '[rf_shadow] ...' text) — a line present in both sinks counts once

def _record(day, raw_line):
    if '[rf_shadow]' not in raw_line:
        return
    canon = raw_line[raw_line.index('[rf_shadow]'):].strip()
    key = (day, canon)
    if key in seen:
        return
    seen.add(key)
    mm = pat.search(canon)
    if not mm:
        dirty.append((day, 'unparseable: ' + canon[-120:])); return
    site, c, mac, n, rfm = mm.groups()
    try:
        cf, mf = float(c), float(mac)
    except ValueError:
        dirty.append((day, f'{site}: const={c} macro={mac}')); return
    if not (math.isfinite(cf) and math.isfinite(mf)):
        dirty.append((day, f'{site}: non-finite const/macro')); return
    if abs(cf - mf) > 0.5:
        dirty.append((day, f'{site}: |const-macro|={abs(cf-mf):.2f} > 0.5')); return
    clean.append((day, f'{site} const={cf:.3f} macro={mf:.3f} n={n} rf_mean={rfm or "-"}')); days.add(day)

for f in sorted(glob.glob('/root/openclaw/logs/daily_cycle_steps_*.log')):
    day = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(f)).group(1)
    if day < since:
        continue
    for line in open(f, errors='replace'):
        _record(day, line)

# Dedicated durable sink (lib.shadow_log) — survives the 4,000-char step-log
# tail that dropped this line before. One record per line: '<ISO ts> <line>'.
_rf_shadow_log = '/root/openclaw/logs/rf_shadow.log'
if os.path.exists(_rf_shadow_log):
    for line in open(_rf_shadow_log, errors='replace'):
        day = line[:10]
        if day < since:
            continue
        _record(day, line)

g2 = len(clean) >= need and len(days) >= 2 and not dirty
ok &= g2
out.append(f"  G2 shadow: clean={len(clean)} (need {need}) days={len(days)} (need 2) dirty={len(dirty)} since={since} -> {'OK' if g2 else 'NOT_YET'}")
for d, r in clean[-6:]: out.append(f'    clean {d}: {r}')
for d, r in dirty[:6]: out.append(f'    dirty {d}: {r}')
print('OK' if ok else 'NOT_YET')
print('\n'.join(out))
PY
)"
STATUS="$(echo "$VERDICT" | head -1)"
say "verdict: $STATUS"; echo "$VERDICT" | tail -n +2 | tee -a "$LOG"

if [ "$STATUS" != "OK" ]; then
  post_discord "[rf-flip] NOT applied — gate not met yet:
$(echo "$VERDICT" | tail -n +2 | head -12)"
  exit 0
fi
[ "$APPLY" = 1 ] || { say "check-only: gate MET; run with --apply to flip"; exit 0; }

# --- apply --------------------------------------------------------------------
cp -p "$ENVF" "$ENVF.bak.rf-flip.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -qE '^OPENCLAW_RF_SOURCE=' "$ENVF"; then sed -i -E 's|^OPENCLAW_RF_SOURCE=.*|OPENCLAW_RF_SOURCE=macro|' "$ENVF"
else printf '\nOPENCLAW_RF_SOURCE=macro\n' >> "$ENVF"; fi
say "flag set: $(grep -E '^OPENCLAW_RF_SOURCE=' "$ENVF")"
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
systemctl stop "$TIMER_UNIT" 2>/dev/null || true
post_discord "[rf-flip] APPLIED — OPENCLAW_RF_SOURCE=macro ($RESULT). Gate:
$(echo "$VERDICT" | tail -n +2 | head -8)
Next: S_m / bench_realized / every backtest now use FRED DGS3MO. Kill switch: OPENCLAW_RF_SOURCE=const + user-scope johnbot restart."
exit 0
