#!/bin/bash
# options_surface_flip_after_shadow.sh — guarded one-shot flip of
# OPENCLAW_OPTIONS_SURFACE=1 (spec docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md
# A.7; runbooks docs/runbooks/2026-09-04-options-surface-rollout.md and
# docs/runbooks/2026-09-06-options-surface-v3-rollout.md). Operator 2026-09-06:
# "you may flip the options surface automatically as well".
#
# Flips OPENCLAW_OPTIONS_SURFACE=1 in .env and restarts the user-scope johnbot
# ONLY IF all four gates hold:
#   G1 master: data/master/options_surface.parquet carries the v3/§H contract —
#      the `iv30_source` column exists and every latest-session row is
#      options_features_version 3 (the v3 rollout unit landed).
#   G2 live shadow: at least --min-lines CLEAN `[options_surface] shadow` lines
#      since --since (default 2026-09-08) on >= 2 distinct days. Clean =
#      version=3, n >= 3000, iv30 old/new median in [1.5, 3.5], rv20_nonnull >= 95,
#      iv_rank_nonnull >= 60, vrp_nonnull >= 60, mfiv_nonnull >= 80 (over tickers
#      with >= 2 fitted expiries), dur < 180 s, and that day's log carries no
#      "v2 build failed" / "partial shadow" warning. (The 2026-09-04 runbook's
#      iv_rank_nonnull >= 80 assumed v1-like coverage; §H restores most of it,
#      60 is the floor below which the served dict would starve the strategies.)
#   G3 backtest authority: the five options strategies' latest primary backtest
#      rows post-date the v3 panel (the fleet re-derived them on the §H panel),
#      so eligibility reflects the same feature definitions the live dict serves.
#   G4 never inside the weekday 13:00–20:15 UTC compute window.
# Otherwise it posts why and leaves the flag alone (exit 0). Once applied it
# stops its own transient timer. Kill switch: OPENCLAW_OPTIONS_SURFACE=0 +
# user-scope johnbot restart (the legacy dict path is intact in engine.py).
#
# Usage:
#   scripts/options_surface_flip_after_shadow.sh            # check only
#   scripts/options_surface_flip_after_shadow.sh --apply    # check, then flip + restart on success
#   --since YYYY-MM-DD  --min-lines N (default 2)  --env-file PATH  --no-restart
#   --no-post  --timer-unit NAME (default options-surface-flip.timer)
set -uo pipefail
cd /root/openclaw || exit 2

APPLY=0; SINCE=2026-09-08; MIN=2; ENVF=/root/openclaw/.env; RESTART=1; POST=1
TIMER_UNIT=options-surface-flip.timer
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;; --since) SINCE="$2"; shift;; --min-lines) MIN="$2"; shift;;
    --env-file) ENVF="$2"; shift;; --no-restart) RESTART=0;; --no-post) POST=0;;
    --timer-unit) TIMER_UNIT="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
LOG=/root/openclaw/logs/options_surface_flip.log
ts() { date -u +%FT%TZ; }
say() { echo "[surface-flip $(ts)] $*" | tee -a "$LOG"; }
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
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-surface-flip/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
}

# --- already applied? ---------------------------------------------------------
if grep -qE '^OPENCLAW_OPTIONS_SURFACE=1' "$ENVF"; then
  say "already applied (OPENCLAW_OPTIONS_SURFACE=1 in $ENVF) — nothing to do"
  systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
  exit 0
fi

# --- G4 compute-window guard (weekdays 13:00–20:15 UTC) ----------------------
dow=$(date -u +%u); hm=$(date -u +%H%M)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1300 ] && [ "$hm" -le 2015 ] && [ "$APPLY" = 1 ]; then
  say "refusing to flip inside the weekday compute window (UTC $hm)"; exit 0
fi

# --- G1 + G2 + G3 ---------------------------------------------------------------
VERDICT="$(POSTGRES_URI="$PG_URI" SINCE="$SINCE" MIN="$MIN" python3 - <<'PY'
import glob, os, re, math, datetime as dt
import psycopg2
import pyarrow.parquet as pq
since, need = os.environ['SINCE'], int(os.environ['MIN'])
out, ok = [], True
# G1 — the v3/§H master landed
master = '/root/openclaw/data/master/options_surface.parquet'
g1 = False; g1_msg = 'master missing'
if os.path.exists(master):
    names = set(pq.read_schema(master).names)
    if 'iv30_source' not in names:
        g1_msg = 'iv30_source column absent (v3 rollout not landed)'
    else:
        d = pq.read_table(master, columns=['date']).to_pandas()['date']; last = d.max()
        v = pq.read_table(master, columns=['options_features_version'], filters=[('date', '==', last)]).to_pandas()['options_features_version']
        g1 = len(v) > 0 and bool((v == 3).all())
        g1_msg = f'latest session {last} rows={len(v)} version3={int((v == 3).sum())}'
ok &= g1
out.append(f"  G1 master: {g1_msg} -> {'OK' if g1 else 'NOT_YET'}")
master_mtime = dt.datetime.fromtimestamp(os.path.getmtime(master), dt.timezone.utc) if os.path.exists(master) else None
# G2 — clean live shadow lines
pat = re.compile(r'\[options_surface\] shadow n=(\d+) iv30 old/new median=(\S+) p90=(\S+) iv_rank_nonnull=(\d+)% '
                 r'rv20_nonnull=(\d+)% vrp_nonnull=(\d+)% mfiv_nonnull=(\d+)% rn_nonnull=(\d+)% '
                 r'iv30_src smile=(\d+)% band=(\d+)% spot_stale=(\d+)% dur=(\S+) version=(\d+)')
clean, dirty, days = [], [], set()
seen = set()   # (day, canonical '[options_surface] shadow ...' text) — a line present in both sinks counts once
day_txt: dict[str, str] = {}   # day -> concatenated daily_cycle_steps_*/engine*.log text (for the day-level checks)

def _record(day, raw_line):
    if '[options_surface] shadow' not in raw_line:
        return
    canon = raw_line[raw_line.index('[options_surface] shadow'):].strip()
    key = (day, canon)
    if key in seen:
        return
    seen.add(key)
    mm = pat.search(canon)
    if not mm:
        dirty.append((day, 'unparseable/old format: ' + canon[-140:])); return
    n, med, p90, ivr, rv, vrp, mf, rn, sm, bd, stale, dur, ver = mm.groups()
    reasons = []
    try:
        n = int(n); med = float(med); ivr = int(ivr); rv = int(rv); vrp = int(vrp); mf = int(mf)
        durs = None if dur == 'n/a' else float(dur.rstrip('s'))
    except ValueError:
        dirty.append((day, 'numeric parse: ' + canon[-140:])); return
    if ver != '3': reasons.append(f'version={ver}')
    if n < 3000: reasons.append(f'n={n}<3000')
    if not (math.isfinite(med) and 1.5 <= med <= 3.5): reasons.append(f'iv30 ratio {med}')
    if rv < 95: reasons.append(f'rv20_nonnull={rv}<95')
    if ivr < 60: reasons.append(f'iv_rank_nonnull={ivr}<60')
    if vrp < 60: reasons.append(f'vrp_nonnull={vrp}<60')
    if mf < 80: reasons.append(f'mfiv_nonnull={mf}<80')
    if durs is None or durs >= 180: reasons.append(f'dur={dur}')
    txt = day_txt.get(day, '')
    if 'v2 build failed' in txt: reasons.append('v2 build failed present that day')
    if 'partial shadow' in txt: reasons.append('budget partial shadow that day')
    if reasons:
        dirty.append((day, '; '.join(reasons)))
    else:
        clean.append((day, f'n={n} ratio={med:.2f} iv_rank={ivr}% rv20={rv}% vrp={vrp}% mfiv={mf}% smile/band={sm}/{bd}% dur={dur}')); days.add(day)

files = sorted(set(glob.glob('/root/openclaw/logs/daily_cycle_steps_*.log') + glob.glob('/root/openclaw/logs/engine*.log')))
file_days = []
for f in files:
    m = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(f))
    day = m.group(1) if m else dt.datetime.fromtimestamp(os.path.getmtime(f), dt.timezone.utc).strftime('%Y-%m-%d')
    if day < since:
        continue
    txt = open(f, errors='replace').read()
    day_txt[day] = day_txt.get(day, '') + txt
    file_days.append((day, txt))
for day, txt in file_days:
    for line in txt.splitlines():
        _record(day, line)

# Dedicated durable sink (lib.shadow_log) — survives the 4,000-char step-log
# tail that dropped this line before 2026-09-06; the glob above stays a
# secondary source. Its lines never carry "v2 build failed"/"partial shadow"
# (shadow_log.record only ever gets the shadow_summary line, never a build
# warning), so the day-level text checks above still key off day_txt.
_dedicated = '/root/openclaw/logs/options_surface_shadow.log'
if os.path.exists(_dedicated):
    for line in open(_dedicated, errors='replace'):
        day = line[:10]
        if day < since:
            continue
        _record(day, line)

g2 = len(clean) >= need and len(days) >= 2 and not dirty
ok &= g2
out.append(f"  G2 shadow: clean={len(clean)} (need {need}) days={len(days)} (need 2) dirty={len(dirty)} since={since} -> {'OK' if g2 else 'NOT_YET'}")
for d, r in clean[-4:]: out.append(f'    clean {d}: {r}')
for d, r in dirty[:4]: out.append(f'    dirty {d}: {r}')
# G3 — options strategies re-derived on the v3 panel
sids = ('S21_iv_hv_spread', 'S_HV8_gamma_theta_carry', 'S_HV19_iv_surface_tilt', 'S_HV20_iv_dispersion_reversion', 'S_options_flow_confirmed_momentum')
panel = '/root/openclaw/data/master/options_aggregates_enriched.parquet'
panel_mtime = dt.datetime.fromtimestamp(os.path.getmtime(panel), dt.timezone.utc) if os.path.exists(panel) else None
lagging = []
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("""SELECT DISTINCT ON (strategy_id) strategy_id, run_at FROM strategy_backtest_runs
                   WHERE primary_window=true AND strategy_id = ANY(%s) ORDER BY strategy_id, run_at DESC""", (list(sids),))
    latest = {s: r for s, r in cur.fetchall()}
for s in sids:
    r = latest.get(s)
    if r is None or panel_mtime is None or not g1 or r < panel_mtime:
        lagging.append(s)
g3 = not lagging
ok &= g3
out.append(f"  G3 backtests: panel mtime={panel_mtime.strftime('%Y-%m-%dT%H:%MZ') if panel_mtime else 'n/a'} lagging={len(lagging)} -> {'OK' if g3 else 'NOT_YET'}")
if lagging: out.append('    lagging: ' + ', '.join(lagging))
print('OK' if ok else 'NOT_YET')
print('\n'.join(out))
PY
)"
STATUS="$(echo "$VERDICT" | head -1)"
say "verdict: $STATUS"; echo "$VERDICT" | tail -n +2 | tee -a "$LOG"

if [ "$STATUS" != "OK" ]; then
  post_discord "[surface-flip] NOT applied — gate not met yet:
$(echo "$VERDICT" | tail -n +2 | head -12)"
  exit 0
fi
[ "$APPLY" = 1 ] || { say "check-only: gate MET; run with --apply to flip"; exit 0; }

# --- apply --------------------------------------------------------------------
cp -p "$ENVF" "$ENVF.bak.surface-flip.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -qE '^OPENCLAW_OPTIONS_SURFACE=' "$ENVF"; then sed -i -E 's|^OPENCLAW_OPTIONS_SURFACE=.*|OPENCLAW_OPTIONS_SURFACE=1|' "$ENVF"
else printf '\nOPENCLAW_OPTIONS_SURFACE=1\n' >> "$ENVF"; fi
say "flag set: $(grep -E '^OPENCLAW_OPTIONS_SURFACE=' "$ENVF")"

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
systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
post_discord "[surface-flip] APPLIED — OPENCLAW_OPTIONS_SURFACE=1 ($RESULT). Gate:
$(echo "$VERDICT" | tail -n +2 | head -8)
Next cycle: strategies read the v3 surface dict (iv30 = CM-30d ATM, real iv_rank, CBOE OI keys). Kill switch: OPENCLAW_OPTIONS_SURFACE=0 + user-scope johnbot restart."
exit 0
