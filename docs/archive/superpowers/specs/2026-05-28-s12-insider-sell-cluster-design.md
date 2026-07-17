# S12 Insider Sell-Cluster Extension — Design

**Status:** spec approved, plan pending
**Date:** 2026-05-28
**Motivating incident:** The 2026-05-28 GLW open-drop investigation revealed that
the live `S12_insider` strategy is BUY-cluster only — structurally incapable of
emitting any negative signal on a ticker where insiders are selling. GLW had ten
distinct officers sell $35M over 30 days (CFO, CTO, COO, Vice Chairman,
Controller, GC, etc., zero purchases) and none of it was visible to the strategy
stack. Meanwhile eight momentum strategies were actively LONG GLW based on its
+13.6% 20-day trailing return, so the system was loading the position the
insiders were unloading. This spec closes the gap with a surgical, env-gated
addition to S12.

---

## 1. Goals & non-goals

### Goals

- Extend `src/strategies/implementations/s12_insider.py` so it can emit
  `direction='SHORT'` signals on tickers exhibiting an insider sell-cluster
  pattern.
- Keep the existing BUY-cluster path byte-identical to today when the new gate
  is off — zero risk to the live strategy.
- Symmetric architecture: SHORT signals flow through the existing regime-blended
  sizer the same way LONG signals do (no sizer changes, no new tables, no new
  systemd timers).
- Validate the modified strategy through a backtest pass before any live flip.
- Catch the GLW-class pattern (≥5 officers, zero buys, ≥$2M net) without firing
  on routine 10b5-1 / option-exercise selling.

### Non-goals

- Pre-market panic scanner integration (covered by separate spec
  `2026-05-28-premarket-panic-scan-design.md`).
- A separate `S12_insider_sell` strategy with its own lifecycle. Rejected
  during brainstorming — single-file in-place modification preferred.
- A SELL-cluster veto / attenuator on other strategies' longs (rejected —
  direct SHORT signal is symmetric to existing LONG and uses the existing
  sizer math).
- Role weighting (CFO sells > SVP sells). Considered, dropped for MVP — adds
  complexity without clear edge over the breadth+dollar gate.
- Cross-cluster gating beyond the "zero buys in window" rule.
- Threshold auto-tuning. The constants are tunable via code change only.

---

## 2. Architecture summary

A single, surgical change to `src/strategies/implementations/s12_insider.py`:

- Existing BUY-cluster branch untouched.
- New SELL-cluster branch added in parallel, guarded by
  `OPENCLAW_S12_SELL_CLUSTER=1` (default OFF). When OFF, the strategy is
  byte-identical to today.
- When ON, the SELL branch emits `direction='SHORT'`, `position_size_pct=0.03`
  on tickers where the sell-cluster threshold is met.
- The strategy's manifest entry stays `state='live'`; only the env gate flips.
- A backtest validation pass with the gate ON runs before the operator flips
  the live env var.

---

## 3. Sell-cluster trigger

Mirror the existing BUY-cluster math symmetrically but with tighter gates,
because routine insider selling is much noisier than insider buying.

### Locked thresholds

- `SELL_MIN_DISTINCT_INSIDERS = 5` (vs 3 on the BUY side — sells are noisier)
- `SELL_REQUIRE_ZERO_BUYS = True` (no equivalent on the BUY side — any insider
  selling alongside buying signals ambiguity; we'd rather pass than guess)
- `SELL_MIN_NET_VALUE_USD = 2_000_000` (vs $500K on the BUY side — 4× the bar)
- `SELL_LOOKBACK_TRADING_DAYS = 20` (same as BUY — keeps the strategy's
  internal window unified)

These live as module-level constants in `s12_insider.py` so they are
test-pinnable and tunable without touching the algorithm.

### Pseudocode (additive — new branch only)

```python
# Inside generate_signals(); runs only when OPENCLAW_S12_SELL_CLUSTER=='1'
import os

if os.environ.get('OPENCLAW_S12_SELL_CLUSTER') == '1':
    recent_txns = aux_data['insider_txns'].filter(
        date >= as_of - SELL_LOOKBACK_TRADING_DAYS
    )
    per_ticker = recent_txns.group_by('ticker')

    for ticker, txns in per_ticker:
        sells = [t for t in txns
                 if 'SALE' in t.transaction_type or 'S-Sale' in t.transaction_type]
        buys  = [t for t in txns
                 if 'BUY' in t.transaction_type or 'PURCHASE' in t.transaction_type]

        distinct_sellers = len({t.insider_name for t in sells})
        net_sell_value   = sum(t.net_value for t in sells)

        if (distinct_sellers >= SELL_MIN_DISTINCT_INSIDERS
                and len(buys) == 0
                and net_sell_value >= SELL_MIN_NET_VALUE_USD):
            emit Signal(
                ticker=ticker,
                direction='SHORT',
                position_size_pct=0.03,
                reason=(f'insider_sell_cluster: {distinct_sellers} officers, '
                        f'${net_sell_value/1e6:.1f}M, 0 buys, '
                        f'{SELL_LOOKBACK_TRADING_DAYS}d window'),
            )
```

The BUY-cluster constants remain at their current values
(`MIN_DISTINCT_INSIDERS=3`, `MIN_NET_VALUE_USD=500_000`,
`MIN_PER_INSIDER_USD=50_000`) in their own constants — no
cross-pollination of buy and sell parameters.

---

## 4. Backtest validation pass (operator-driven, pre-flip)

Before flipping `OPENCLAW_S12_SELL_CLUSTER=1` in production `.env`, the operator
runs:

```bash
OPENCLAW_S12_SELL_CLUSTER=1 python3 -m src.backtest.run_backtest \
    --strategy S12_insider
```

### Acceptance criteria (matches PROMOTION_THRESHOLDS for equity)

- Modified S12 produces `sharpe_ratio >= 0.5` AND `max_drawdown <= 0.20` over
  the lifetime window. (Same gates `lifecycle.py` applies to any equity
  strategy at promotion time.)
- Modified S12's Sharpe is **>= the current buy-only Sharpe**. The SELL path
  must add edge, not subtract. If it degrades the unified strategy, we don't
  ship — tighten thresholds further, or drop the path entirely.
- The operator writes a verdict file
  `docs/superpowers/runs/2026-05-XX-s12-sell-cluster-backtest.json`
  containing the before/after metrics (sharpe, max_drawdown, trade_count,
  per-direction breakdown of contribution) and the decision (FLIP / HOLD /
  REJECT).

If the verdict is REJECT, the SELL path code stays committed but the gate
stays OFF — operator can iterate on thresholds in a follow-up commit.

---

## 5. Gates

| Gate | Default | Effect |
|------|---------|--------|
| `OPENCLAW_S12_SELL_CLUSTER` | `0` | Master gate for the SELL branch. OFF = strategy behaves identically to today (BUY-only). ON = SHORT signals additionally emitted on sell-cluster tickers. |

No other env vars needed. Thresholds are code constants; the manifest
lifecycle state stays `live` throughout.

---

## 6. Edge cases and behavior

- **`aux_data['insider_txns']` missing or empty:** existing BUY-cluster code
  already returns `[]`; the new SELL branch short-circuits the same way.
  No new failure mode.
- **Net value exactly `$2M`:** `>=` is inclusive — fires.
- **Mixed buys and sells in the window** (e.g., 6 sells + 1 buy): the
  `zero buys` gate blocks. Intentional — a single insider buy alongside
  cluster sells signals ambiguity; pass rather than guess.
- **Multiple transactions by the same insider** (e.g., one CFO selling 5
  separate tranches across 20 days): `distinct_sellers` uses
  `set(insider_name)`, so 5 transactions by 1 person counts as 1 distinct
  seller. Prevents a single liquidating insider from passing the breadth
  gate.
- **Existing LONG signal from another strategy on the same ticker:** the
  sizer's existing math sums signed positions across strategies. 8
  momentum strategies emitting `+0.03` LONG plus S12 emitting `-0.03`
  SHORT nets to `+0.21` — still long but reduced. This is the
  symmetric-SHORT behavior the brainstorm explicitly chose; documented and
  accepted, not a defect.
- **Strategy fires on tickers we don't currently hold (broader universe):**
  the sizer routes new SHORT signals to entry like any other signal,
  subject to the existing leverage / DTBP / position-count limits. Same
  as any newly fired LONG. No special handling needed.
- **DTBP guard blocks new SHORT:** falls through to the existing
  `OPENCLAW_DTBP_GUARD` behavior — the SHORT is sized down or skipped
  with a logged reason. Not a new failure mode.

---

## 7. Testing strategy

`tests/strategies/test_s12_insider_sell_cluster.py` — eight unit tests:

1. **Gate OFF baseline (regression):** identical output to current S12 on a
   synthetic insider fixture. Locks current behavior so the SELL branch can't
   accidentally affect BUY semantics.
2. **Gate ON, 5 sellers $3M net, 0 buys:** emits one SHORT signal with the
   expected `reason` string.
3. **Gate ON, 4 sellers** (one short of breadth threshold): no SHORT emitted.
4. **Gate ON, 5 sellers + 1 buy:** zero-buys gate blocks; no SHORT.
5. **Gate ON, 5 sellers but $1.5M net** (below dollar gate): no SHORT.
6. **Gate ON, 1 seller with 5 transactions** (no breadth): `distinct_sellers`
   gate blocks; no SHORT.
7. **Gate ON, GLW historical fixture** (10 sellers, $35M, 0 buys, May 6-22
   transactions from the real `insider.parquet`): emits SHORT with reason
   including officer count and dollar value.
8. **Gate ON, simultaneous buy-cluster + sell-cluster in same 20-day window**
   (contrived ticker with both conditions): asserts neither LONG nor SHORT
   fires (zero-buys gate blocks SELL; existing BUY logic also fails on the
   sells side if symmetric — confirm interaction).

Existing S12 unit tests (BUY-cluster paths) must continue to pass unchanged
with the gate OFF.

Target: ~8 new unit tests + zero regression failures.

---

## 8. Files touched

| File | Status | Responsibility |
|------|--------|----------------|
| `src/strategies/implementations/s12_insider.py` | CHANGED (additive — new SELL branch + 4 constants, existing BUY logic untouched) | Strategy implementation. |
| `tests/strategies/test_s12_insider_sell_cluster.py` | NEW | 8 unit tests. |
| `docs/superpowers/runs/2026-05-XX-s12-sell-cluster-backtest.json` | NEW (operator-generated during validation step) | Verdict + before/after metrics. |

**No** migration, **no** new table, **no** new script, **no** new systemd
timer, **no** manifest changes, **no** lifecycle state change.

---

## 9. Rollout plan

1. **Land code + tests** on a new branch
   `feat/s12-insider-sell-cluster` off `main` (or stacked onto the current
   `feat/premarket-panic-scan` branch — decision at implementation time).
   All tests green. Gate stays OFF in committed `.env.example`.
2. **Run backtest validation** with the gate ON in a sandbox shell:
   ```bash
   OPENCLAW_S12_SELL_CLUSTER=1 python3 -m src.backtest.run_backtest \
       --strategy S12_insider
   ```
   Verify acceptance criteria (Sharpe >= 0.5 and >= current; MaxDD <= 0.20).
3. **Write the verdict file** with before/after metrics; commit it.
4. **If verdict is FLIP:** edit production `.env` to set
   `OPENCLAW_S12_SELL_CLUSTER=1`, restart relevant services, monitor the
   next daily cycle for SHORT signals emitted by S12.
5. **If verdict is REJECT:** leave the gate OFF, open a follow-up issue to
   iterate on thresholds.

Each step has a clean revert: set the gate back to `0` and restart. The
code stays committed regardless of the gate state.

---

## 10. Open questions (to be resolved in the plan)

- Exact pattern for the BUY-cluster constants in the current `s12_insider.py`:
  are they module-level or function-local? The plan must read the file and
  match the existing style for the new SELL constants.
- The `aux_data['insider_txns']` shape: confirmed by earlier investigation
  that columns are `(ticker, date, transaction_date, insider_name, role,
  transaction_type, shares, price_per_share, net_value, ...)`. The plan must
  verify the `transaction_type` enum values include both `'S-Sale'` and any
  variants seen in `insider.parquet` so the `'SALE' in txn_type or 'S-Sale'
  in txn_type` filter catches all sell-direction rows.
- The exact path for the backtest runner CLI (`python3 -m
  src.backtest.run_backtest --strategy S12_insider`): confirm against the
  actual runner module name and CLI flags before writing the validation step.
