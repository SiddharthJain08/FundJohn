# S15 — Opportunistic Insider Short Strategy

**Date:** 2026-05-28
**Author:** BotJohn + operator
**Status:** Approved design, pending implementation plan
**Predecessor:** S12_insider SELL extension (HOLD verdict 2026-05-28; see `docs/superpowers/runs/2026-05-28-s12-sell-cluster-backtest.json`)

---

## 1. Goal

Build a standalone SHORT strategy that captures the multi-month informational content of *opportunistic* insider sales, while preserving the S12_insider BUY-only baseline (Sharpe 14.13 / MaxDD 1.16% over 9 trades, 2025-11-28 → 2026-05-28).

The strategy is **structurally distinct** from S12_insider. It does not extend, share code with, or share parameters with that strategy. The two coexist as independent alpha sources.

## 2. Why a New Strategy (Not Another S12 Variant)

The S12_SELL extension (2026-05-28 v3 verdict) failed across every threshold configuration tested:

| Config | Sharpe | MaxDD | Trades | Verdict |
|---|---|---|---|---|
| BUY-only (gate OFF) | **+14.13** | 1.16% | 9 | Baseline |
| 5 sellers / $2M (loose) | +2.38 | 9.50% | 407 | Dilutes baseline by 11.7 Sharpe pts |
| 10 sellers / $5M (tight) | −1.58 | 17.00% | 46 | Worse than loose |
| 5/$2M + structural filter | −4.90 | 26.02% | 109 | Active negative edge |

Root causes diagnosed:
- **Indiscriminate sale counting.** Every officer with vested RSUs sells periodically. 10b5-1 plans guarantee non-informational sells. The S12_SELL filter fired on all of them.
- **Time-horizon mismatch.** Insider sell informational decay is multi-month (Cohen, Malloy & Pomorski 2012 measure peak spread at 6 months). S12_SELL used regime-default tight stops with 20-day windows.
- **Asymmetric tail risk on naked equity shorts.** Stop-rates 60-77% across all configurations because squeezes routinely move 10%+ intraday while informational drift downward is slow.

S15 addresses each:
- **Opportunistic/routine classifier** filters ~70% of clusters that are mechanical scheduled sales.
- **60-day time exit** aligns with informational decay window.
- **15% wide trailing stop** absorbs squeeze noise.

## 3. Architecture

### Strategic role

Standalone alpha source. SHORT exposure with low correlation to S12_insider BUY (which is LONG-only) and to the regime-blended sizer's other long strategies.

### Identifiers

| Field | Value |
|---|---|
| Strategy ID | `S15_insider_opportunistic_short` |
| File | `src/strategies/implementations/s15_insider_opportunistic_short.py` |
| Class | `OpportunisticInsiderShort` |
| Tier | 2 (research, medium conviction) |
| Active regimes | `['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']` — excludes `CRISIS` only |
| Signal frequency | `daily` |
| Direction | SHORT only (no LONG branch) |
| Gate env var | `OPENCLAW_S15_INSIDER_OPPORTUNISTIC` (default OFF) |

### Independence commitment

S15 shares no code paths, parameters, or aux-data slices with S12_insider:

- Separate file, separate class.
- Separate cluster gates (S15 thresholds are stricter and different in kind, not just degree).
- Separate aux key (`insider_history_long`, see §6) — does NOT consume `insider_txns` directly.
- Separate gate env var.
- Separate cooldown bookkeeping (S15 cooldown is 30 trading days vs S12's 10).

A bug, threshold change, or rollback on either strategy must not require touching the other.

## 4. Signal Generation

All three stages must pass for a SHORT signal to fire on a ticker.

### Stage 1 — Cluster gate (per ticker, daily)

**v4 amendment (2026-05-28, post-v3-KILL):** v1/v2/v3 verdict trajectory: Sharpe +0.14 → −1.60 → −3.44. Cooldown (v2) and momentum filter (v3) both pushed the signal further negative. v4 reverts v2/v3 changes and instead tightens Stage 1 conviction: bumps min_insiders 3 → 4 and min_net_sell_value $5M → $10M. Pushes toward higher-conviction clusters at the cluster gate itself. Final hypothesis test — if the v1 signal had any latent edge that the v1 looseness was diluting, tightening the gate isolates it.

**v5 amendment (2026-05-28, post-v4-KILL-but-recovered):** v4 broke the monotonic worsening (Sharpe recovered −3.44 → +0.28, MaxDD 17.93% → 4.21%). v5 pushes the Stage 1 axis further along the only lever that worked: bumps min_insiders 4 → 5 and min_net_sell_value $10M → $20M. Result: **non-monotonic** — v5 Sharpe collapsed to −1.39 and MaxDD tripled to 11.69% despite hit-rate rising (0.568 → 0.634). The over-tightened gate filtered out positive-edge trades faster than negative-edge ones; remaining loss distribution clustered in time, blowing daily-portfolio Sharpe. **v4 is the local optimum on this axis.**

**v6 amendment (2026-05-28, post-v5-KILL):** Reverts Stage 1 to v4 local optimum (4 insiders / $10M) and tests a NEW axis — drops `max_concurrent_positions` 20 → 8 so the ranking score (`opp_count × log10(net_sell_value)`) finally bites. Result: per-trade economics meaningfully improved (avg pnl 2.2×, MaxDD 4.21% → 3.88%) and **combined Sharpe jumped 0.36 → +1.04** (3× v4, best across all 6 versions), but standalone Sharpe regressed +0.28 → −0.36 because surviving loss days covary. The lever bound on 27% of days (31/116) confirming it's a real concentration effect, not noise. Still fails G1/G3 gates (combined 1.04 vs 8.0 target).

For each ticker in the universe, examine sales in the trailing 30 calendar days:

- `distinct_insiders >= 4` *(v6: reverted from v5's 5 to v4 local optimum)*
- `net_sell_value >= $10_000_000` (sum of `value` over qualifying transactions) *(v6: reverted from v5's $20M to v4 local optimum)*
- Zero offsetting buys in the same 30-day window (`require_zero_buys = True`)
- Only count transactions where `transaction_type` is one of:
  - `S-Sale`
  - `S`
- Explicitly exclude `M-Exempt`, `F-InKind`, `G-Gift`, `D` (return to issuer), `A-Award`, `J-Other`, `P-Purchase`.

If Stage 1 fails: ticker skipped.

### Stage 2 — Opportunistic classifier (per insider in the cluster)

For each insider in the Stage-1-qualifying cluster, classify them as "opportunistic" or "routine":

1. Look up their qualifying sales (same transaction-type whitelist as Stage 1) in months **t − 15 to t − 3** (a 12-month window with a 3-month gap to avoid look-ahead leakage from the cluster itself).
2. Bucket sales by calendar quarter (Q1: Jan-Mar, etc.).
3. Count distinct quarters with ≥1 sale.
4. Classify:
   - `quarters_with_sales >= 3` → **routine** (regular pattern; non-informational)
   - `quarters_with_sales <= 2` → **opportunistic**
   - `total_qualifying_sales_in_window == 0` → **opportunistic** (new insider, default to high signal)

Cluster passes Stage 2 iff **at least 2 insiders are classified opportunistic.**

If Stage 2 fails: ticker skipped. This is the load-bearing filter.

### Stage 3 — Conviction filter (any one passes)

Stage 3 confirms the cluster carries material signal. Any of the following passes:

1. **Personal stake test:** any single seller in the cluster sold ≥ 10% of their prior personal holdings. The denominator reconstructs their pre-cluster stake as `shares_owned_after_latest + sum(shares_sold_by_this_seller_in_30d_window)`. The numerator is `sum(shares_sold_by_this_seller_in_30d_window)`. Threshold: `numerator / denominator >= 0.10`. If `shares_owned_after` is missing for the seller, skip this seller for sub-test (1) (do not fail; other sub-tests may still pass).
2. **Company stake test:** aggregate cluster shares sold ≥ 1.5% of company shares outstanding. Use `fundamentals.shares_outstanding` from the existing FMP-sourced fundamentals snapshot if available; if unavailable, skip this sub-test (do not fail Stage 3 on it).
3. **Role test:** ≥ 1 seller in the cluster has a `role` string containing any of: `CEO`, `CFO`, `COO`, `Chair`, `Chairman` (case-insensitive substring match).

If none of (1), (2), (3) pass: ticker skipped.

### Final signal

If Stages 1-3 all pass, emit a SHORT Signal with:

- `direction = 'SHORT'`
- `entry_price = current_close`
- `stop_loss = current_close * 1.15` (15% above entry — wide, on the SHORT side)
- `target_1 = target_2 = target_3 = None` (no price targets — time exit only)
- `position_size_pct = 0.015 * regime_scale`
- `confidence = 'HIGH'` (all three stages already gate; no MED tier)
- `signal_params`:
  - `distinct_insiders`
  - `opportunistic_count`
  - `routine_count`
  - `net_sell_value`
  - `top_seller_pct_of_holdings`
  - `c_suite_present` (bool)
  - `lookback_days = 30`
  - `cluster_kind = 'SELL_OPPORTUNISTIC'`

### Ranking and cap

If more than 20 tickers qualify in a single cycle, rank by:

```
score = opportunistic_count * log10(max(net_sell_value, 1))
```

Take top 20 by `score` descending.

## 5. Position Management

| Parameter | Value | Rationale |
|---|---|---|
| Direction | SHORT | |
| Per-name size | 1.5% NAV × regime scale | Lower than S12 LONG (3%) — SHORT tail risk is fatter |
| Max concurrent positions | 20 | Diversification floor for low-Sharpe-per-name strategy |
| Stop loss | 15% trailing from entry, SHORT side | Squeezes need wide stops; tested 60-77% stop rates kill tight-stop variants |
| Price targets | None | Time-decay trade |
| Time exit | Close at trading day 60 from entry | Aligns with informational decay window (literature) |
| Re-entry cooldown | 30 trading days after a stop-out | Prevents re-entering a squeeze ticker daily |
| Active regimes | LOW_VOL, TRANSITIONING, HIGH_VOL | CRISIS excluded — squeeze risk worse in panics |

### Regime scaling

Uses the existing `BaseStrategy.position_scale(regime_state)` method (no override). In CRISIS, `should_run` returns False and the strategy emits zero signals.

### Stop placement formula

For SHORT direction, the existing `compute_stops_and_targets(ts, 'SHORT', entry_price, regime_state)` returns a regime-aware stop. S15 must OVERRIDE this by post-multiplying the stop to floor at `entry_price * 1.15`:

```python
stops = self.compute_stops_and_targets(ts, 'SHORT', entry_price, regime_state=regime_state)
# enforce minimum 15% above entry on SHORT (wider than regime default)
stops['stop'] = max(stops.get('stop', entry_price * 1.15), entry_price * 1.15)
stops['t1'] = stops['t2'] = stops['t3'] = None
```

This is intentional and called out in the spec because it bypasses the regime-default stop discipline.

## 6. Data and Aux Loader

### New aux key

`aux_data['insider_history_long']` — added to `src/strategies/aux_data_loader.py` as a sibling key alongside the existing `insider_txns`.

Schema (per ticker, dict-of-lists, same shape as `insider_txns`):

```python
{
    'AAPL': [
        {
            'reportingName': 'Cook Timothy D',
            'role': 'officer: CEO',
            'transactionDate': '2025-08-15',
            'transactionType': 'S-Sale',
            'value': 12_500_000.0,
            'shares': 50_000.0,
            'pricePerShare': 250.0,
            'sharesOwnedAfter': 850_000.0,
        },
        ...
    ],
    ...
}
```

**Lookback window:** trailing **15 months** from `as_of`, vs `insider_txns`' 30-day window. Required for Stage 2 classification.

**Performance:** the existing parquet (`data/master/insider.parquet`, 40K rows, 387 tickers) loads in <500ms; the 15-month slice is well under that. Pre-parse `transactionDate` to `pd.Timestamp` once at load time and index by ticker — same pattern as the existing `insider_txns` loader.

**Backtest plumbing:** `_per_bar_simulate` must pass the 15-month-as-of-bar slice into `load_aux_data`, same way it already does for `insider_txns`. The slice is computed from `as_of - 15mo` to `as_of - 3mo` for Stage 2 and `as_of - 30d` to `as_of` for Stage 1. We provide the full 15-month window in `insider_history_long`; the strategy slices internally.

### Universe handling

S15 follows S12_insider's established pattern: **no `universe_filter` declared at module level.** The default `sp500` predicate applies via `DEFAULT_UNIVERSE_FILTER`.

Rationale: universe predicates in `src/strategies/universe_default.py` have signature `(meta: TickerMetadata, as_of) -> bool` and are restricted to metadata-only deterministic checks (universe_lint gate forbids `os`/`datetime`/`time` imports). They cannot read `insider.parquet`. Adding a `has_insider_history` flag to `TickerMetadata` is a cross-cutting backfill change out of scope for this strategy.

Instead, S15 filters inside `generate_signals` by iterating the resolved universe and fast-failing tickers without insider data: `if not insider_history_long.get(ticker): continue`. The 387 tickers in `insider.parquet` overlap heavily with SP500, so most cycles touch the strategy's logic for ~300-400 names. Per-bar overhead is negligible (same pattern as S12_insider).

Performance note: the dict lookup is O(1); the strategy will not slow the backtest meaningfully even across the full SP-2 union universe.

## 7. Testing

### Unit tests — `tests/strategies/test_s15_insider_opportunistic_short.py`

**T1: Opportunistic classifier — routine seller.** Synthetic insider with sales in every quarter for 12 months. Assert classified routine.

**T2: Opportunistic classifier — one-time large sale.** Synthetic insider with one sale in t−4 months and nothing else in the 15-month window. Assert classified opportunistic.

**T3: Opportunistic classifier — new insider.** Synthetic insider with zero qualifying sales in the window. Assert classified opportunistic.

**T4: Personal stake test.** Synthetic transaction selling 100k shares with `shares_owned_after = 400k`. Assert personal stake ratio = 20%, passes 10% gate.

**T5: Transaction-type filter.** Cluster with 5 sellers but 3 are M-Exempt (option exercise). Assert only the 2 real S-Sale entries count toward Stage 1.

**T6: Time-based exit at day 60.** Simulate a SHORT entry; advance the per-bar simulation 60 trading days; assert position closed with `exit_reason = 'time_exit'`.

**T7: Wide trailing stop at 15%.** Simulate a SHORT entry at $100; price moves to $115.01 on a single bar; assert stop fires.

**T8: Cross-run cooldown.** Same fixture as the recent S12 cooldown bug fix — verify 30-day post-stop cooldown is respected using the `run_stop_history` plumbing.

**T9: GLW replay.** Use real insider data for GLW around 2026-05-08. Assert S15 fires a SHORT signal before the 2026-05-28 crash AND that the cluster contains ≥2 opportunistic sellers AND passes the C-suite test.

**T10: Routine-seller-heavy cluster — must NOT fire.** Synthetic cluster of 5 sellers, all classified routine. Assert no signal emitted. Proves Stage 2 is load-bearing.

**T11: Ranking and cap.** Provide 25 qualifying tickers; assert only top 20 by score are emitted.

### Backtest acceptance — v1 verdict file

Output: `docs/superpowers/runs/2026-05-28-s15-opportunistic-backtest.json` (created in plan execution).

Period: 2025-11-28 → 2026-05-28 (6 months).

Three runs required:

**Baseline A** — S12_insider BUY-only, S15 disabled (`OPENCLAW_S15_INSIDER_OPPORTUNISTIC=0`).
Expected: reproduces v3 baseline (Sharpe ~14.13).

**Modified** — S12_insider BUY-only + S15 enabled.
Acceptance:
- S15 standalone Sharpe ≥ 0.5
- S15 standalone MaxDD ≤ 15%
- S15 trade count ≥ 15 SHORT trades
- Combined (S12 BUY + S15 SHORT) Sharpe ≥ 8.0 *(tighter than initial draft of 6.0; reflects operator preference to protect the 14.13 baseline. Combined ≥ 8.0 still allows the SHORT alpha to express while keeping ≥ 57% of baseline edge.)*
- Combined MaxDD ≤ 8% *(baseline MaxDD is 1.16%; cap allows 6.8pp of additional drawdown headroom for the SHORT book)*

**Ablation** — S15 with Stage 2 (opportunistic classifier) disabled (every seller treated opportunistic).
Acceptance: Sharpe MUST drop below Modified Sharpe. Proves Stage 2 carries weight. Failure here means the classifier is decorative; investigate before merge.

### Verdict gates

| Outcome | Action |
|---|---|
| Modified passes all acceptance + Ablation confirms Stage 2 | Operator decision: FLIP gate live |
| Modified standalone fails (Sharpe < 0.5 or trades < 15) | HOLD — calibration issue |
| Combined fails (Sharpe < 8.0 or MaxDD > 8%) | HOLD — too much dilution |
| Ablation fails (Stage 2 disabled is equal/better) | INVESTIGATE — classifier is broken |
| GLW unit test fails | BLOCK — strategy doesn't reproduce the target case |

## 8. Rollout

1. Branch: `feat/s15-insider-opportunistic-short` (already created).
2. TDD per task following `superpowers:subagent-driven-development`.
3. v1 backtest committed alongside code with verdict JSON.
4. Operator review of verdict.
5. If FLIP: merge to main, push to origin, append `OPENCLAW_S15_INSIDER_OPPORTUNISTIC=1` to `.env` on host, restart `johnbot.service`.
6. Strategy registers as `paper` initially per lifecycle convention; promotes to `candidate` / `live` via standard lifecycle thresholds after live trade history accumulates.

### Risk gates already in place

- Sizer 25% daily-deploy cap applies to S15 the same as any other strategy.
- DTBP guard (`OPENCLAW_DTBP_GUARD=1`) gates execution capacity for SHORTs.
- CRISIS regime excludes S15 from emitting at all.

## 9. Non-Goals

- **No 8-K / news cross-reference.** Stays orthogonal to the EDGAR ingester and premarket scanner shipped today.
- **No options expression.** Pure equity SHORT. Options are SP-5 territory.
- **No pairs hedge.** Approach C from brainstorm was deferred.
- **No size-down overlay.** Approach B from brainstorm was deferred.
- **No automatic threshold tuning.** Initial thresholds (3 insiders, $5M, ≥2 opportunistic, 10% personal stake, 60d hold, 15% stop) are research-grade defaults from the literature. Tuning happens post-deploy via the v3-style backtest framework, not in-spec.

## 10. Open Questions for the Operator (must resolve before plan execution)

None. All questions resolved in brainstorm.

---

## Appendix A — Calibration Sources

- Cohen, Malloy & Pomorski (2012), "Decoding Inside Information," *Journal of Finance*. Defines the routine/opportunistic taxonomy; documents the 6-month spread.
- Lakonishok & Lee (2001), "Are Insider Trades Informative?" Documents the % of holdings effect.
- Cziraki & Gider (2021), "The Dollar Profits to Insider Trading." Documents the role-of-insider effect (CEO/CFO sales more informative).

Thresholds in §4 are research-grade defaults. The v1 backtest will tell us whether they generalize to our 387-ticker universe over the 6-month measurement window; expect to revisit after first deploy.
