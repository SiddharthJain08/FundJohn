# W2 — Strategy manifest<->registry drift: operator sign-off sheet

Generated 2026-06-29 (read-only). manifest trade-intent (state in live/monitoring) vs registry trade-reality (status=approved).
The ENGINE trades strategy_registry.status='approved'; the dashboard shows manifest .state. Each row below diverges.
**No live-book change is made without your per-row decision.** Mark KEEP (endorse engine/registry) or STOP (deprecate) per row.

Total divergent: 16  (engine-approved-but-hidden: 15 ; shown-live-but-not-trading: 1)

| # | strategy | manifest | registry | bt_sharpe | last_signal | sigs_30d | divergence | recommended decision | KEEP/STOP |
|---|---|---|---|---|---|---|---|---|---|
| 1 | S10_quality_value | candidate | approved | NULL | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 2 | S15_insider_opportunistic_short | candidate | approved | NULL | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 3 | S15_iv_rv_arb | candidate | approved | NULL | 2026-06-05 | 15 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 4 | S_HV16_gex_regime | candidate | approved | NULL | 2026-05-21 | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 5 | S_HV17_earnings_straddle_fade | candidate | approved | NULL | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 6 | S_HV7_iv_crush_fade | candidate | approved | NULL | 2026-05-21 | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 7 | S_bankruptcy_risk_anomaly | candidate | approved | NULL | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 8 | S_constrained_gmv_vcv_dynamics | candidate | approved | NULL | 2026-06-26 | 40 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 9 | S_fomc_presell_spy_long | candidate | approved | 2.7694 | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 10 | S_labor_day_week_momentum_reversal | candidate | approved | 5.9448 | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 11 | S_local_global_balance | candidate | approved | NULL | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 12 | S_price_path_convexity | live | deprecated | 0 | 2026-06-19 | 18 | SHOWN-LIVE, engine IGNORES | Decide: un-deprecate registry (resume) OR demote manifest (confirm stop) | ____ |
| 13 | S_skewness_dispersion_macro | candidate | approved | NULL | never | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 14 | S_tr_01_vvix_early_warning | candidate | approved | NULL | 2026-06-12 | 2 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 15 | S_tr_04_intraday_spy_momentum | candidate | approved | NULL | 2026-06-12 | 3 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |
| 16 | S_vp_macd_index_sensitivity | candidate | approved | 0 | 2026-05-26 | 0 | ENGINE TRADES, dashboard HIDES | Decide: promote manifest to live (endorse) OR deprecate registry (stop trading) | ____ |

## Applying decisions (deliberate gated step, AFTER sign-off — no auto-sync)
- KEEP an ENGINE-TRADES/HIDDEN row -> promote manifest to live (dashboard matches reality).
- STOP an ENGINE-TRADES/HIDDEN row -> deprecate the registry row (engine stops trading it).
- SHOWN-LIVE/IGNORES (S_price_path_convexity): un-deprecate registry to resume, or demote manifest to confirm stopped.
- C7 makes the transition->registry sync fatal/retried so this cannot silently recur.

---

## 2026-07-03 enrichment — corrected metrics (true-MTM + adverse-slippage re-backtest)

Decision-grade inputs generated 2026-07-03 (§7 re-backtest, run_at > 2026-07-01T15:52:59Z;
these are the CORRECTED risk-adjusted numbers — the old bt_sharpe column above is smeared/inflated).
Gate thresholds (equity): min_sharpe 0.5, max_dd 0.20.

| # | strategy | registry NOW | corr Sharpe | corr maxDD | corr ret | corr trades | live closed n/win/avg | open pnl rows | sigs 30d | recommendation |
|---|---|---|---|---|---|---|---|---|---|
| 1 | S10_quality_value | approved | — (0 trades) | — | — | 0 | 0 | 0 | 0 | **STOP** — corrected engine produces zero trades; never signaled live; dead weight |
| 2 | S15_insider_opportunistic_short | approved | −0.65 | 48.1% | −41.4% | 1699 | 0 | 0 | 0 | **STOP** — deeply negative corrected; fails gate on every axis |
| 3 | S15_iv_rv_arb | approved | −1.53 | 21.0% | −17.4% | 2653 | 0 | 160 | 10 | **STOP** — negative corrected AND still actively signaling |
| 4 | S_HV16_gex_regime | approved | −1.62 | 41.7% | −37.9% | 2126 | 0 | 1501 | 0 | **STOP** — negative corrected; also 1,501 stale open signal_pnl rows (U3 bloat) |
| 5 | S_HV17_earnings_straddle_fade | approved | — (0 trades) | — | — | 0 | 0 | 0 | 0 | **STOP** — zero trades corrected; never signaled |
| 6 | S_HV7_iv_crush_fade | approved | −1.26 | 37.1% | −32.7% | 1754 | 0 | 37 | 0 | **STOP** — negative corrected |
| 7 | S_bankruptcy_risk_anomaly | approved | — (0 trades) | — | — | 0 | 0 | 0 | 0 | **STOP** — zero trades corrected; never signaled |
| 8 | S_constrained_gmv_vcv_dynamics | approved | −4.52 | 15.2% | −13.9% | 51201 | 9 / 56% / +0.17% | 39 | 93 | **STOP** — worst corrected Sharpe on the sheet over 51k trades; the tiny positive live sample (n=9) is noise |
| 9 | S_fomc_presell_spy_long | approved | **+2.38** | **3.7%** | +5.0% | 5 | 0 | 0 | 0 | **KEEP** — passes gate cleanly; event strategy (FOMC), fires rarely by design (small n caveat) |
| 10 | S_labor_day_week_momentum_reversal | approved | **+2.02** | **5.1%** | +9.7% | 8 | 0 | 0 | 0 | **KEEP** — passes gate cleanly; seasonal (Labor Day week), fires ~1x/yr (small n caveat) |
| 11 | S_local_global_balance | approved | — (0 trades) | — | — | 0 | 0 | 0 | 0 | **STOP** — zero trades corrected (run completed 07-02) |
| 12 | S_price_path_convexity | deprecated | −3.02 | 85.1% | −84.6% | 123214 | 10 / 50% / −1.19% | 69 | 18 | **confirm STOP** — corrected metrics catastrophic; registry already deprecated: demote manifest live→candidate to close the drift |
| 13 | S_skewness_dispersion_macro | approved | — (0 trades) | — | — | 0 | 0 | 0 | 0 | **STOP** — zero trades corrected (run 07-03) |
| 14 | S_tr_01_vvix_early_warning | approved | — (0 bt trades ever) | — | — | 0 | 0 | 0 | 2 | **STOP (weak)** — zero backtest trades ⇒ zero sizing weight; emits rare VVIX-warning signals (2/30d); keep only if you value those pings |
| 15 | S_tr_04_intraday_spy_momentum | approved | — (0 bt trades ever) | — | — | 0 | 0 | 0 | 3 | **STOP (weak)** — same shape as #14 |
| 16 | S_vp_macd_index_sensitivity | deprecated | — | — | — | 0 | 0 | 13 | 0 | **RESOLVED** — F2 deprecated it 06-30; manifest already candidate; no action |

Notes:
- "— (0 trades)" = the corrected engine ran and produced no trades at all (not a data gap).
- Applying decisions stays the deliberate gated step from the header: STOP ⇒ deprecate the
  registry row (gated /transition path; stamps deprecated_at per the W4 backlog fix), KEEP ⇒
  promote manifest to live so the dashboard matches trading reality.
- This enrichment doubles as the F2-round-2 triage for these 16 (owed on corrected metrics).

### 2026-07-03 troubleshooting update — why the 7 "dead weight" rows were silent

Per-strategy root causes (probed by direct generate_signals invocation on real data,
backtest-style AND live-style aux):

| row | strategy | root cause | class | revised recommendation |
|---|---|---|---|---|
| 11 | S_local_global_balance | pandas-3 CoW: `np.fill_diagonal(corr.values)` raised ValueError on EVERY bar; unified_backtest's per-bar `except: continue` swallowed it silently (70-min walk, rc=0, 0 trades) + any-NaN `dropna()` row-annihilation | **CODE BUG — FIXED 07-03** | **HOLD** — re-backtest post-fix, decide on real metrics (probe now emits signals) |
| 13 | S_skewness_dispersion_macro | `pct_change().dropna()` (any-NaN) annihilated every row of the wide returns panel → dispersion history always empty → `len<6` guard fired forever | **CODE BUG — FIXED 07-03** | **HOLD** — re-backtest post-fix (history now builds: 24 pts; today z=−0.23 < 1.0 entry = legitimately quiet) |
| 14 | S_tr_01_vvix_early_warning | needs `aux['macro']['VVIX']` as a SERIES; backtest aux only has scalar day-values under `vol_indices` → backtest-blind. LIVE works (engine provides 4 macro series; 2 signals/30d) | BACKTEST AUX GAP | keep live if the VVIX warnings are valued — but sizer weight stays 0 until the aux loader exposes macro series (small enhancement: read vol_indices.parquet history) |
| 15 | S_tr_04_intraday_spy_momentum | needs 30-minute bars; backtest aux has none. LIVE works (engine loads 119-ticker 30m; 3 signals/30d) | BACKTEST AUX GAP | same as #14 but the 30m backtest support is heavier; keep-or-stop by taste (weight stays 0) |
| 1 | S10_quality_value | reads 6 FMP ratio fields; financials.parquet has 5 of them **all-NULL** (returnOnEquity, returnOnInvestedCapital, debtEquityRatio, enterpriseValueMultiple, priceToFreeCashFlowsRatio) → screens filter everything, live AND backtest | DATA GAP | **STOP** (cannot function anywhere); backlog: enrich the FMP financials collector with ratio fields, then revisit |
| 7 | S_bankruptcy_risk_anomaly | Altman-Z needs balance-sheet items (retainedEarnings, totalAssets, workingCapital…) absent from financials.parquet → "too few Z-scores: 0", live AND backtest | DATA GAP | **STOP**; same backlog (balance-sheet fetch) |
| 5 | S_HV17_earnings_straddle_fade | needs earnings_dte in [0,5] + iv_rank: earnings-calendar coverage is ~12 events (live) / 12-of-395 tickers (backtest slice) → near-zero opportunity set; docstring flagged the dependency at birth | DATA GAP | **STOP**; backlog: broaden FMP earnings-calendar coverage (also affects the cohort2026 sibling — all straddle-fade variants show 0 trades) |

Systemic fixes shipped alongside (2026-07-03): unified_backtest now COUNTS swallowed
per-bar exceptions and prints a loud WARN (+ `bars_raised` in the sim dict) — a
100%-raise strategy can no longer masquerade as a clean 0-trade run.
Post-155-run rerun list: add S_local_global_balance + S_skewness_dispersion_macro
(their 07-02/07-03 corrected rows are bogus-zero from the bug era).
