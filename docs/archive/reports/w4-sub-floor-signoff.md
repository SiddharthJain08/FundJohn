# W4 — Sub-Floor Live-Strategy Sign-Off Sheet (operator KEEP/STOP)

- **Date:** 2026-06-29 (W4 research recon)
- **Status:** ✅ **EXECUTED 2026-06-30** (operator sign-off: "STOP all 14"). The 14 STOP rows (12 Tier-A + 2 zero-trade Tier-C) were **deprecated** — 13 live via the C7 registry-first `/transition` route (`live→deprecated`, manifest+registry both synced), and `S_vp_macd_index_sensitivity` (manifest=candidate/registry=approved drift) via a direct audited registry deprecate (`status='deprecated'` + `deprecated_at` + `deprecation_reason`; the route 422s `candidate:deprecated`). End-state verified: 14/14 registry `deprecated`; the 2 KEEP rows (`S_prism_vq_cross_section_factor`, `S_visibility_graph_rsi`) stay `approved`. **Deprecate halts NEW signals only** — existing open positions ride to their own exits (no flatten requested). RESIDUAL: `S_vp_macd_index_sensitivity` now manifest=candidate/registry=deprecated (stopped, but a manifest/registry inconsistency the W2 drift badge will surface; cosmetic — it won't trade). FOLLOW-UP OWED: the 2 KEEP + 38 Tier-B still need the metric backfill.
- **What this is:** every `strategy_registry.status='approved'` (engine-traded) strategy that fails the candidate→live quality floor (Sharpe ≥ 0.5, MaxDD ≤ 20%, trades > 0, backtest present). These reached live via the dashboard `/transition` force-override or the **ungated `/approve-strategy` Discord path** (W4-F2) — neither enforced the floor.
- **55 total** sub-floor approved: **12 with a real sub-0.5 backtest Sharpe (Tier A — the concern)**, **38 null-backtest (Tier B — metric gap)**, **4 zero/degenerate-trade (Tier C)**.
- **KEY CAVEAT:** `live_days` = 0 and `live_sharpe` = NULL for **all 12 Tier-A** rows — there is **no accrued live track record** to offset the negative backtest. The registry's live-performance columns are largely unpopulated across the book (only a few strategies e.g. `S9_dual_momentum` have live data) — a **separate metric-completeness finding** (relates to W2 U1 "P&L not reconciled to broker fills"). So these decisions rest on backtest + DD, not live performance.

---

## Tier A — real sub-0.5 backtest Sharpe (the concern; recommend STOP unless you have a specific thesis)
| Strategy | bt Sharpe | bt MaxDD% | bt trades | live | approved | note |
|---|---|---|---|---|---|---|
| S_pca_etf_stat_arb_reversion | **−2.82** | 0.2 | 430 | none | 05-29 dashboard | strongly negative |
| oxf_adaptive_ma | −0.48 | 12.9 | 61856 | none | 06-16 dashboard | Oxford set |
| oxf_keltner | −0.35 | 12.6 | 42281 | none | 06-16 dashboard | Oxford set |
| oxf_macd_zero | −0.20 | 13.6 | 62875 | none | 06-16 dashboard | Oxford set |
| oxf_linreg_slope | −0.12 | **22.8** | 53398 | none | 06-16 dashboard | neg + DD>20 |
| oxf_frama | −0.12 | 14.5 | 36523 | none | 06-16 dashboard | Oxford set |
| S_btc_gold_dual_momentum_rotation | −0.01 | 0.3 | 23 | none | 05-19 dashboard | thin (23 tr) |
| S_bppp_bayesian_parametric_weights | +0.01 | 5.9 | 28000 | none | 05-29 dashboard | ~zero edge |
| oxf_heikin_ashi | +0.01 | 5.8 | 63125 | none | 06-16 dashboard | ~zero edge |
| oxf_zero_lag_ma | +0.03 | 16.0 | 48279 | none | 06-16 dashboard | ~zero edge |
| S_growth_inflation_sector_timing | +0.34 | **42.7** | 2192 | none | 05-29 dashboard | DD 42.7% |
| oxf_false_breakout | +0.46 | **29.2** | 4039 | none | 06-16 dashboard | DD 29.2% |

**Decision (mark per row): KEEP / STOP.** _Default recommendation: STOP all 12 (negative/near-zero backtest edge, several with DD far above the 20% floor, none with live evidence). The 8 `oxf_*` are the "Oxford" trend-following set committed 06-16 — if you want a trend sleeve, keep at most the least-bad and size it small; otherwise STOP._

## Tier C — zero/degenerate backtest trades (recommend STOP the 0-trade ones)
| Strategy | bt Sharpe | bt trades | approved | note |
|---|---|---|---|---|
| S_3d_pca_characteristic_factors | 0.00 | **0** | 05-16 dashboard | 0 backtest trades = degenerate |
| S_vp_macd_index_sensitivity | 0.00 | **0** | 05-19 dashboard | 0 backtest trades = degenerate |
| S_prism_vq_cross_section_factor | +0.67 | null | 05-19 dashboard | trade_count not recorded — backfill |
| S_visibility_graph_rsi | +3.00 | null | 05-19 dashboard | strong Sharpe, trade_count not recorded — backfill, likely KEEP |

**Decision: STOP** the two 0-trade rows (a strategy with zero backtest trades should not trade live). `S_prism_vq` / `S_visibility_graph_rsi` are likely KEEP — their trade_count is just unrecorded (backfill metric).

## Tier B — null backtest_sharpe (metric-completeness gap, NOT evidence of a bad strategy)
38 approved strategies have **no `backtest_sharpe` recorded in the registry** (mostly older, approved 04-09 → 05-12; many `operator`/`reconcile`-approved). These were likely backtested elsewhere; the null is a **data gap, not poor performance** — e.g. `S9_dual_momentum` shows null backtest but **live Sharpe +22.10 / +128% return**. Sample: `S_btc_momentum, S9_dual_momentum, S10_quality_value, S15_iv_rv_arb, S21_iv_hv_spread, S_markov_frontier_regimes, S25_dual_momentum, S_extreme_intraday_reversal_nasdaq, …` (full list available on request).

**Decision: KEEP (default) + BACKFILL metrics.** Recommend a one-shot `backfill_candidate_metrics` / re-backtest pass to populate `backtest_sharpe` for these 38 so the registry is honest and the candidate→live gate has data — rather than per-row sign-off. Flag any that come back sub-floor for a later pass.

---

## Recommended disposition
1. **Tier A + the 2 zero-trade Tier-C rows** → operator marks KEEP/STOP; STOP rows get `status='deprecated'` (engine stops trading them next cycle). 14 rows to decide.
2. **Tier B (38)** → no per-row sign-off; backfill metrics, then re-triage.
3. **Prevent recurrence** (W4 remediation Tier 3, if pursued): route the ungated `/approve-strategy` through the same Sharpe/DD gate as the dashboard, so sub-floor strategies can't reach `approved` without an explicit force.
