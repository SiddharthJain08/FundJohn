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
