#!/usr/bin/env bash
# SP-5.0 grounding-gate cleanup + completion (one-shot, RTH-only).
# Fired by openclaw-sp5-cleanup.timer at 2026-05-28 13:35 UTC (09:35 ET).
#
# Steps:
#   1. Close test-4 short call vertical (SPY260618C00750000 short + SPY260618C00760000 long).
#   2. Dry-run iron condor (construction-only re-test; live confirmation deferred).
#   3. L1 hierarchy test: 1-contract cash-secured short put, non-marketable limit, then cancel.
#   4. Verify clean state (no leftover sp5* orders/positions).
#   5. Append results to docs/archive/superpowers/specs/2026-05-27-sp5-alpaca-options-snapshot.md.
#   6. Best-effort Discord post if DISCORD_GROUNDING_WEBHOOK is set (no-op otherwise).
#
# Idempotent: re-running with positions already flat just logs no-ops.
# Safety: live submit uses non-marketable limit then cancels by client-order-id.

set -u  # not -e: we want to capture per-step failures and continue

cd /root/openclaw || { echo "[sp5-cleanup] FATAL: cd /root/openclaw failed"; exit 2; }

BIN=/root/go/bin/alpaca
SNAPSHOT=/root/openclaw/docs/archive/superpowers/specs/2026-05-27-sp5-alpaca-options-snapshot.md
LOG_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

log()  { echo "[sp5-cleanup $(date -u +%H:%M:%SZ)] $*"; }

# Step 0: market open check — options orders are RTH-only.
CLOCK=$("$BIN" clock --jq '.is_open' 2>&1)
if [ "$CLOCK" != "true" ]; then
    log "ABORT: market not open (is_open=$CLOCK). Re-arm the timer for next RTH."
    {
        echo ""
        echo "## SP-5.0 cleanup attempt — $LOG_TS — ABORT"
        echo "Market closed when timer fired (\`is_open=$CLOCK\`). No actions taken. Re-arm or invoke manually during RTH."
    } >> "$SNAPSHOT"
    exit 1
fi

# ── Step 1: close test-4 short call vertical ────────────────────────────────
log "Step 1: close test-4 short call vertical (short leg first, then long wing)"
CLOSE1=$("$BIN" position close --symbol-or-asset-id SPY260618C00750000 2>&1)
log "close short 750C: $CLOSE1"
sleep 3
CLOSE2=$("$BIN" position close --symbol-or-asset-id SPY260618C00760000 2>&1)
log "close long 760C: $CLOSE2"
sleep 5
REMAIN=$("$BIN" position list --jq '[.[] | select(.symbol | test("SPY2606")) | {symbol, qty, side}]' 2>&1)
log "remaining SPY260618 positions: $REMAIN"

# ── Step 2: dry-run iron condor (construction confirmation) ─────────────────
log "Step 2: dry-run iron condor (no submission)"
R5=$("$BIN" order submit --order-class mleg --type limit --limit-price 1.00 --qty 1 --time-in-force day --dry-run \
    --legs '[{"symbol":"SPY260618C00760000","side":"sell","ratio_qty":"1","position_intent":"sell_to_open"},{"symbol":"SPY260618C00770000","side":"buy","ratio_qty":"1","position_intent":"buy_to_open"},{"symbol":"SPY260618P00740000","side":"sell","ratio_qty":"1","position_intent":"sell_to_open"},{"symbol":"SPY260618P00730000","side":"buy","ratio_qty":"1","position_intent":"buy_to_open"}]' \
    2>&1)
R5_SUM=$(echo "$R5" | jq -c '{order_class, leg_count:(.legs|length), code, error}' 2>/dev/null || echo "$R5")
log "iron condor dry-run: $R5_SUM"

# ── Step 3: L1 cash-secured short put test ──────────────────────────────────
log "Step 3: L1 hierarchy test — cash-secured short put"
# Discover a viable deep-OTM put: strike low enough that strike*100 fits cash budget.
# Account cash ≈ buying_power / multiplier. From account get: BP $59k, mult 4 → cash ≈ $14.8k.
# Try strike <= 145 first (collateral <= $14.5k); fall back to wider strike range with log.
PUTS_LOW=$("$BIN" option contracts --underlying-symbols SPY --expiration-date 2026-06-18 --type put --strike-price-lte 145 --limit 10 --jq '[.option_contracts[] | .symbol] | sort' 2>&1)
TARGET_PUT=$(echo "$PUTS_LOW" | jq -r '.[-1] // empty' 2>/dev/null)
if [ -z "$TARGET_PUT" ]; then
    log "no SPY put listed at strike<=145; widening to <=250"
    PUTS_WIDE=$("$BIN" option contracts --underlying-symbols SPY --expiration-date 2026-06-18 --type put --strike-price-lte 250 --limit 30 --jq '[.option_contracts[] | .symbol] | sort' 2>&1)
    TARGET_PUT=$(echo "$PUTS_WIDE" | jq -r '.[0] // empty' 2>/dev/null)
fi
log "L1 test target: $TARGET_PUT"

L1_RESULT='SKIPPED — no viable strike'
if [ -n "$TARGET_PUT" ]; then
    TSL1=$(date +%s)
    # Non-marketable: sell at limit 999 (real put premium << 999 → rests, no fill).
    RL1=$("$BIN" order submit --symbol "$TARGET_PUT" --qty 1 --side sell --type limit --limit-price 999.00 \
        --time-in-force day --position-intent sell_to_open --client-order-id sp5L1-$TSL1 2>&1)
    L1_RESULT=$(echo "$RL1" | jq -c '{id, status, code, error}' 2>/dev/null || echo "$RL1")
    log "L1 result ($TARGET_PUT): $L1_RESULT"
    IDL1=$(echo "$RL1" | jq -r '.id // empty' 2>/dev/null)
    [ -n "$IDL1" ] && "$BIN" order cancel --order-id "$IDL1" >/dev/null 2>&1 && log "cancelled $IDL1"
fi

# ── Step 4: verify clean state ─────────────────────────────────────────────
sleep 3
FINAL_POS=$("$BIN" position list --jq '[.[] | select(.symbol | test("SPY2606")) | {symbol, qty}]' 2>&1)
FINAL_ORD=$("$BIN" order list --status open --limit 50 --jq '[.[] | select((.client_order_id // "") | startswith("sp5")) | {coid:.client_order_id, sym:.symbol, status}]' 2>&1)
log "final SPY260618 positions: $FINAL_POS"
log "final open sp5* orders: $FINAL_ORD"

# ── Step 5: append durable results to snapshot doc ─────────────────────────
{
    echo ""
    echo "## SP-5.0 cleanup execution — $LOG_TS"
    echo ""
    echo "**Step 1 — close test-4 vertical:**"
    echo '```'
    echo "close 750C (short): $CLOSE1"
    echo "close 760C (long):  $CLOSE2"
    echo "remaining SPY260618 positions: $REMAIN"
    echo '```'
    echo ""
    echo "**Step 2 — iron condor dry-run (construction-only re-test):**"
    echo '```'
    echo "$R5_SUM"
    echo '```'
    echo ""
    echo "**Step 3 — L1 hierarchy test (cash-secured short put):**"
    echo "Target: \`${TARGET_PUT:-none}\`"
    echo '```'
    echo "$L1_RESULT"
    echo '```'
    echo ""
    echo "**Final state:** positions=$FINAL_POS open-orders=$FINAL_ORD"
} >> "$SNAPSHOT"

log "complete. Results appended to $SNAPSHOT"

# ── Step 6: best-effort Discord notify ─────────────────────────────────────
if [ -n "${DISCORD_GROUNDING_WEBHOOK:-}" ]; then
    MSG="SP-5.0 cleanup @ $LOG_TS — closes: 750C=$CLOSE1; 760C=$CLOSE2; condor-dryrun=$R5_SUM; L1=$L1_RESULT; final-pos=$FINAL_POS"
    curl -sS -X POST -H "Content-Type: application/json" \
         -d "$(jq -nc --arg msg "${MSG:0:1800}" '{content:$msg}')" \
         "$DISCORD_GROUNDING_WEBHOOK" >/dev/null 2>&1 || true
fi

exit 0
