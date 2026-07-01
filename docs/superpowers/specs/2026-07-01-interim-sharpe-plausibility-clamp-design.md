# Interim Backtest-Sharpe Plausibility Clamp — Design

**Date:** 2026-07-01
**Status:** Design approved (operator: "Approve A as designed", CAP default 3.0)
**Type:** Interim live-sizing guard (superseded by the true-MTM backtest fix + full re-backtest)

## Problem

The canonical backtest metrics in `strategy_backtest_runs` / `strategy_backtest_regimes`
have a confirmed methodology defect: `unified_backtest._portfolio_daily_returns` smears each
trade's *total* realized `pnl_pct` flat across its holding days, so the reconstructed daily
return series has almost no volatility (residual variation only from day-to-day trade-composition
changes, which shrink as trade overlap rises). Consequently `sharpe = (mean − 0.05/252)/std × √252`
has an **inflated, overlap-dependent magnitude** (real strategies show 0.2–0.9%/yr vol vs a
realistic ~10%/yr; |Sharpe| runs to ~14). See `scratchpad/metric_recon_findings.md`.

The production sizer (`strategy_weights.rebuild`) sets each strategy's regime `weight =
effective_sharpe` directly. An inflated positive Sharpe is therefore **over-weighted in
proportion to the artifact** (a distorted +14 gets ~14× the weight of a true +1), and the
corr-adjusted conviction gate consumes the same persisted `daily_weight`. This guard bounds that
harm **now**, without waiting for the (compute-gated) true-MTM re-backtest.

## Goal

Clamp each per-regime backtest Sharpe to a plausibility band `[−CAP, +CAP]` at the sizer's
rebuild, so no strategy's weight (or corr-gate contribution) is driven by an implausible Sharpe
magnitude — while preserving every current inclusion/exclusion decision.

## Non-goals (separate workstreams)

- The true-MTM engine fix (emit per-day marks from `simulate_trade` → `_portfolio_daily_returns`)
  + full ~189-strategy re-backtest. **This is the real fix; this clamp is interim.**
- The promotion-gate / dashboard guard (they read `strategy_backtest_runs` directly; the
  re-backtest corrects their inputs).
- The registry-mirror Option-B reconciliation (deferred, operator-chosen, after the metric fix).

## Design (Approach A — symmetric magnitude clamp)

**Architecture:** one clamp at a single choke point in the sizer's backtest-Sharpe loader, driven
by a live-tunable `pipeline_config` value, leaving the genuine live-fill Sharpe untouched.

### Component 1 — config helper `_get_bt_sharpe_cap(cur) -> float`
New function in `src/execution/strategy_weights.py`, mirroring
`oue_classifier._get_sigma_gate`:
```python
def _get_bt_sharpe_cap(cur) -> float:
    """Read bt_sharpe_plausibility_cap from pipeline_config (interim guard for the
    backtest-Sharpe methodology defect); default 3.0. Set very high to disable."""
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key='bt_sharpe_plausibility_cap'")
        row = cur.fetchone()
        if row:
            return float(row[0])
    except Exception:
        pass
    return 3.0
```
- Absent key → default 3.0 → **clamp is ON by default** once the code deploys (no migration; the
  KV row is optional). Tune or disable by `INSERT`/`UPDATE`ing the key (e.g. `999` = off).
- Fail-safe: any read error → default 3.0 (guard stays ON, never crashes the rebuild).

### Component 2 — clamp in `_load_backtest_sharpe(conn, strategy_ids)`
`_load_backtest_sharpe` already builds `out[(sid, regime)] = {'bt_sharpe': .., 'bt_n': ..}` across
its three tiers and holds a cursor `cur`. After the tiers populate `out`, before `return out`:
```python
cap = _get_bt_sharpe_cap(cur)
clamped = []
for key, v in out.items():
    s = v.get('bt_sharpe')
    if s is not None and math.isfinite(s):
        c = max(-cap, min(cap, s))
        if c != s:
            clamped.append((key, s, c))
            v['bt_sharpe'] = c
if clamped:
    logger.info('bt_sharpe plausibility clamp: %d/%d entries clamped to ±%s (e.g. %s %.2f->%.2f)',
                len(clamped), len(out), cap, clamped[0][0], clamped[0][1], clamped[0][2])
```
(`strategy_weights.py` already has `import math` and `logger = logging.getLogger(__name__)`.)
- Symmetric: clamps both inflated positives (bounds over-weight) and inflated negatives (bounds
  the signed corr-gate contribution); a clamped negative stays negative → still excluded by
  `_is_sizeable_sharpe`, so no strategy changes inclusion/exclusion state.
- Non-finite (`NaN`/`Inf`) values are left for the existing `_is_sizeable_sharpe` guard to drop.

### Data flow
clamped `bt_sharpe` → `_effective_sharpe` (blends with untouched `live_sharpe`) → `weight` /
`daily_weight` → persisted `strategy_weights_by_regime` → consumed by both `load_current` (sizer)
and the corr-adjusted conviction gate.

### Invariants (must hold)
1. **Sign preserved** — clamp never crosses zero → no inclusion/exclusion flip (e.g.
   `low_volatility_us` −8.57 → −3.00 stays excluded; it is NOT accidentally funded).
2. **Live Sharpe untouched** — only the corrupted `bt_sharpe` is clamped.
3. **Auto-demote unaffected** — it keys off Sharpe *negativity* (and is gated OFF by default); a
   clamped-but-still-negative value is unchanged in sign.
4. **Reversible, no restart** — `pipeline_config` read every rebuild.

### Error handling
- Config read failure → default 3.0 (guard ON).
- Operator guard-rail (documented, not enforced): do **not** set CAP below ~1.0 — a very low CAP
  clamps genuine positives toward 0 and, at CAP≈0, would flatten the book (all positives → 0 →
  excluded). Sane range ~[2.0, 999].

## Testing
Unit (`tests/test_bt_sharpe_clamp.py`, mock cursor / inject `out`):
1. `_get_bt_sharpe_cap`: key present → parsed float; absent → 3.0; malformed → 3.0; query raises → 3.0.
2. clamp: +14 → +3.0; −8.5 → −3.0 (still negative); +1.2 → +1.2 (untouched); None → untouched;
   NaN/Inf → untouched (left for `_is_sizeable_sharpe`); CAP=999 → nothing clamped (disable path).
3. sign-preservation assertion: no clamped value crosses zero.
Integration (existing rebuild test harness): a strategy with a per-regime bt_sharpe > CAP persists
`effective_sharpe` ≤ CAP in `strategy_weights_by_regime` (bt-only strategy); a strategy with live
data blends the clamped bt with the untouched live value.

## Rollout
1. Deploy code (git push + VPS pull); Python change → no `johnbot` restart needed (the rebuild
   process re-imports).
2. Apply this week: run a manual `python3 -m execution.strategy_weights --rebuild` (DB-only, fast)
   post-deploy — operator-gated — so clamped weights take effect before the Sunday cron.
3. Verify: rebuild log shows the clamp summary line; spot-check `strategy_weights_by_regime` that
   the previously-inflated strategies now cap at `effective_sharpe` ≤ 3.0; confirm no strategy
   changed inclusion (row-count of positive-weight strategies per regime unchanged apart from the
   clamp not removing any).
4. Reversible: `UPDATE pipeline_config SET value='999' WHERE key='bt_sharpe_plausibility_cap'` (or
   delete the key) + next rebuild.
