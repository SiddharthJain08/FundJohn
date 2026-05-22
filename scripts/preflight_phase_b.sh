#!/usr/bin/env bash
# SP-2 Phase B — operator pre-flight checklist (spec §6.3).
#
# Run BEFORE kicking off scripts/backfill_universe_5y.py. Exits 0 if every
# gate passes; non-zero with explanation on first failure. Does NOT perform
# any state mutations.
#
# Usage:
#   bash scripts/preflight_phase_b.sh
#   bash scripts/preflight_phase_b.sh --verbose
#
# Requires (loaded from /root/openclaw/.env):
#   POSTGRES_URI
#   ALPACA_API_KEY, ALPACA_SECRET_KEY
#   FMP_API_KEY
#   DISCORD_BACKFILL_LOG_WEBHOOK  (warned if absent, not fatal)
#
# Targeted env load (NOT `source .env` — see memory `reference-alpaca-cli.md`:
# unquoted parens in .env break bash).

set -euo pipefail

VERBOSE=${1:-}
ROOT=/root/openclaw
cd "$ROOT"

ok()    { printf '\033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '\033[33m⚠\033[0m %s\n' "$1"; }
fail()  { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
note()  { [ -n "$VERBOSE" ] && printf '  %s\n' "$1" || true; }

# ── Load required env without sourcing (.env may contain unquoted parens) ───
for k in POSTGRES_URI ALPACA_API_KEY ALPACA_SECRET_KEY FMP_API_KEY; do
  v=$(grep -E "^${k}=" .env 2>/dev/null | head -1 | cut -d= -f2-)
  [ -n "$v" ] || fail "$k missing in .env"
  export "$k=$v"
done
DISCORD_BACKFILL_LOG_WEBHOOK=$(grep -E "^DISCORD_BACKFILL_LOG_WEBHOOK=" .env 2>/dev/null | head -1 | cut -d= -f2-) || true
export DISCORD_BACKFILL_LOG_WEBHOOK

# ── 1. Phase A live ───────────────────────────────────────────────────────
python3 - <<'PY'
import os, psycopg2, sys
c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='ticker_metadata_snapshots'")
if not cur.fetchone(): sys.exit("ticker_metadata_snapshots table missing — Phase A not deployed")
cur.execute("SELECT count(*) FROM ticker_metadata_snapshots WHERE snapshot_date >= CURRENT_DATE - INTERVAL '3 days'")
recent = cur.fetchone()[0]
if recent < 100: sys.exit(f"Phase A live writer not running ({recent} rows in last 3d)")
print(f"[ok] Phase A live writer healthy ({recent} rows in last 3d)")
PY
ok "Phase A deployed and live writer healthy"

# ── 2. Disk free under /root/openclaw/data/ ≥ 40 GB ────────────────────────
free_gb=$(df --output=avail -BG "$ROOT/data" | tail -1 | tr -d 'G ')
if [ "$free_gb" -lt 40 ]; then
  fail "data/ disk free = ${free_gb}G (need ≥ 40G; backfill projects ~30G)"
fi
ok "disk free ${free_gb}G under data/ (≥ 40G required)"

# ── 3. Redis reachable ────────────────────────────────────────────────────
pong=$(redis-cli PING 2>/dev/null || true)
[ "$pong" = "PONG" ] || fail "redis-cli PING did not return PONG"
ok "redis reachable"

# ── 4. Alpaca tier check (algo_trader_plus) ───────────────────────────────
ALPACA_BIN=${ALPACA_CLI_BIN:-/root/go/bin/alpaca}
[ -x "$ALPACA_BIN" ] || fail "alpaca CLI not at $ALPACA_BIN"
acct=$("$ALPACA_BIN" account info 2>/dev/null || true)
tier=$(echo "$acct" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("data_tier") or d.get("tier") or "unknown")' 2>/dev/null || echo unknown)
note "alpaca tier: $tier"
case "$tier" in
  algo_trader_plus|algo_trader|paid) ok "alpaca tier = $tier (≥ algo_trader_plus assumed)" ;;
  unknown) warn "alpaca tier field not in account payload — manual verify" ;;
  *) fail "alpaca tier = $tier; need algo_trader_plus" ;;
esac

# ── 5. FMP day-quota headroom ────────────────────────────────────────────
# FMP doesn't expose usage in headers; rely on data_provider_health rows
# from SP-1 (see CLAUDE.md). WARN if usage > 50% of 250k/day.
python3 - <<'PY'
import os, psycopg2
c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
cur.execute("""SELECT coalesce(sum(call_count),0) FROM data_provider_health
               WHERE provider='fmp' AND ts > date_trunc('day', NOW())""")
used = int(cur.fetchone()[0])
cap = 250000
pct = used * 100.0 / cap
print(f"[ok] FMP today usage: {used}/{cap} ({pct:.0f}%)")
if pct > 50:
    raise SystemExit(f"FMP daily usage > 50% ({pct:.0f}%) — defer backfill kickoff")
PY
ok "FMP quota headroom available"

# ── 6. Backfill universe artifact frozen + committed ─────────────────────
[ -s data/.backfill_universe_v1.txt ] || fail "data/.backfill_universe_v1.txt missing (Task 1 of plan)"
lines=$(wc -l < data/.backfill_universe_v1.txt)
[ "$lines" -ge 2900 ] || fail "backfill universe has only $lines tickers (expect ≥ 2900)"
git ls-files --error-unmatch data/.backfill_universe_v1.txt >/dev/null 2>&1 \
  || fail "data/.backfill_universe_v1.txt not committed to git"
ok "backfill universe artifact present and committed ($lines tickers)"

# ── 7. SP500 historical membership CSV ───────────────────────────────────
[ -s data/sp500_historical_membership_v1.csv ] || fail "data/sp500_historical_membership_v1.csv missing (Task 2 of plan)"
ok "SP500 historical membership CSV present"

# ── 8. Migration 115 applied ─────────────────────────────────────────────
python3 - <<'PY'
import os, psycopg2
c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='backfill_audit'")
if not cur.fetchone(): raise SystemExit("backfill_audit table missing — migration 115 not applied")
print("[ok] migration 115 applied (backfill_audit table present)")
PY
ok "migration 115 applied"

# ── 9. Doctor preflight green ────────────────────────────────────────────
if python3 -m src.maintenance.doctor --required-only --json >/tmp/preflight_doctor.json 2>&1; then
  ok "doctor --required-only exit 0"
else
  rc=$?; cat /tmp/preflight_doctor.json | tail -40
  fail "doctor preflight failed (rc=$rc) — see output above"
fi

# ── 10. Discord webhook (warn-only) ──────────────────────────────────────
if [ -z "$DISCORD_BACKFILL_LOG_WEBHOOK" ]; then
  warn "DISCORD_BACKFILL_LOG_WEBHOOK unset — daily digest will go to stdout/log only"
else
  ok "Discord webhook configured"
fi

# ── 11. Dry-run smoke (5-ticker × 1-year) ────────────────────────────────
if python3 scripts/backfill_universe_5y.py --target prices \
     --tickers AAPL,MSFT,NVDA,GOOG,AMZN --years $(date +%Y) --dry-run \
     >/tmp/preflight_dryrun.log 2>&1; then
  ok "backfill driver dry-run clean (5 tickers × current year)"
else
  rc=$?; tail -40 /tmp/preflight_dryrun.log
  fail "dry-run smoke failed (rc=$rc)"
fi

printf '\n\033[1;32mAll Phase B preflight gates green — safe to kick off backfill.\033[0m\n'
printf 'Recommended next command:\n'
printf '  nohup python3 scripts/backfill_universe_5y.py --target prices --resume \\\n'
printf '    > /var/log/backfill_prices.log 2>&1 &\n'
