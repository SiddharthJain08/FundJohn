# §7 Metric-Source Reconciliation — Recon Findings (2026-07-01, READ-ONLY)

Verified live against the production DB (read-only). Supersedes the pre-compaction
"31/62 sub-floor / +0.63 / 0.47" remembered figures.

## Sources
- **Canonical** = `strategy_backtest_runs` (primary_window=TRUE, latest run_at per strategy).
  **All 189 rows are `full_history`** (2016-04→2026-06, oos ~3718d, t+1 fill model).
  DD (`total_max_dd_pct`) is **consistent percent** (121/122 > 1.0). → the authoritative source.
- **Mirror** = `strategy_registry.backtest_sharpe / backtest_max_dd_pct / backtest_trade_count`.
  **Stale AND unit-corrupt:** DD column has MIXED units — 44 rows percent (>1.0), 16 rows
  fraction (0–1]; Sharpe ranges to an implausible **14.03**. Feeds NO live sizing (see below).

## Registry status
62 approved · 106 pending_approval · 33 deprecated. (F2 took approved 76→62.)

## Divergence over the 62 approved
- 54 have canonical, 8 missing canonical (all candidate-state 0-trade shells).
- 24 have a registry Sharpe, 38 null (the "Tier-B" gap).
- **17** diverge >0.5 Sharpe · **5** verdict-flip (all registry-OK → canonical-sub-floor)
  · **19** sub-floor on canonical Sharpe · **24** fail the canonical DD gate.

## Blast radius — the mirror feeds NO live sizing
Sizer `strategy_weights._load_backtest_sharpe` is 3-tier: Tier-1 per-regime canonical
(`strategy_backtest_regimes`), Tier-2 legacy, Tier-3 registry mirror. Only **1** approved
strategy (`S15_insider_opportunistic_short`) reaches Tier-3, and its registry Sharpe is null
→ **0 approved strategies size off the mirror.** Reconciliation is display + gate-fallback +
governance only. Dashboard `strategy_row.js` already reads canonical for return/DD.

## CANONICAL Sharpe METHODOLOGY DEFECT (root cause CONFIRMED)
NOT a crash/sign bug. The Sharpe/Sortino/Calmar in `strategy_backtest_runs` are
**systematically distorted** by the daily-return reconstruction in
`unified_backtest._portfolio_daily_returns`:
- It smears each trade's TOTAL realized `pnl_pct` flat across its holding days (a constant
  per-day value), then equal-weight-averages across concurrently open trades. This is NOT a
  mark-to-market daily return — it strips intra-hold price volatility. Residual "vol" comes only
  from day-to-day trade-composition changes, which SHRINK as trade overlap grows.
- `aggregate_metrics`: `sharpe = (daily_returns.mean() − 0.05/252) / std_dr × √252`.
  With the smeared series, `std_dr` is 1-2 orders too small (strategies show 0.2-0.9%/yr vol
  vs a realistic ~10%/yr) → |Sharpe| is inflated AND overlap-dependent (non-comparable across
  strategies). SIGN is set by the correct mean vs the **5% annual RF hurdle**.
- Confirmed by repro (same per-trade economics, vary only overlap): ann_vol 0.65→0.39→0.23%,
  Sharpe −4.43→−9.40→−10.40. And arithmetic: low_volatility_us mean_d 7.24e-5 < rf_d 1.98e-4
  → negative; implied std_d 2.6e-4 = 0.41%/yr → impossibly smooth.
- `return_pct` roughly OK. **`max_dd` is ALSO corrupted** — same smeared equity curve
  (`eq=cumprod(1+daily_returns)`); flat-smearing hides mid-trade drawdowns → DD understated
  AND mis-timed. Decisive: low_volatility_us shows **2.99% max DD across a 2016→2026 window
  that contains the March-2020 COVID crash** — impossible for a long-equity low-vol book
  (true DD 15-30%+). Since DD is a **promotion-gate input**, smooth/high-overlap strategies
  pass the DD floor spuriously. Sharpe/Sortino/Calmar corrupted; DD understated.

Effect examples (all metric-artifact, not the strategy being "broken"):
| strategy | canon return | canon Sharpe | canon DD | reading |
|---|---|---|---|---|
| low_volatility_us | +30.96% (~2.7%/yr) | −7.70 | 2.99% | sub-5%-RF return + smeared-tiny vol |
| S_constrained_gmv_vcv_dynamics | +25.94% | −6.68 | 1.71% | same |
| (inflated positives, e.g. registry Sharpe up to 14.03) | — | implausibly high | — | above-RF + smeared-tiny vol |

**Live impact:** the sizer ranks/weights on these Sharpes (Tier-1 per-regime canonical) →
over-weights high-overlap strategies with inflated positive Sharpes, under-weights/excludes
sub-RF ones. Real but subtle — NOT "a +31% winner unfairly floored" (2.7%/yr is genuinely
below cash). Fix is a **methodology decision** (trade-level Sharpe, true daily-MTM marks from
the simulator, or guard/NULL implausible-vol Sharpes) + a compute-gated full re-backtest —
must preserve the "Fix A" drawdown behavior. → OPERATOR-GATED, do not unilaterally patch.

## Gate registry-fallback DD is latently dead
`promotion_service.evaluatePromotionGate` compares registry `backtest_max_dd_pct > 20`; for the
16 fraction-unit rows (0.xx) that is never true → 100%-DD strategy passes on the fallback path.
Narrow (canonical DD read first) but real. Mooted by retiring the mirror.

## F2 re-validation (RESOLVED — low-stakes)
- **S_btc_gold_dual_momentum_rotation** (deprecated): canon DD **39.4%** (≫20% floor) →
  **deprecation STANDS**. Robust to the DD defect: DD error is toward *understatement*, so true
  DD ≥ 39.4% → still fails regardless. (Sharpe +0.63 is itself unreliable, but moot here.)
- **S_prism_vq_cross_section_factor** (KEEP): canon **2.57** (stronger than registry 0.67) → KEEP confirmed.
- **S_visibility_graph_rsi** (KEEP): canon **0.467** vs registry 3.00, DD 6.9%, 123k trades →
  marginally sub-floor. Recommend **KEEP + confirming re-backtest**, not STOP on the 0.467-vs-0.5 hair.

## Surfaced governance finding (SEPARATE, per-strategy sign-off)
Under canonical, ~19 approved are sub-floor Sharpe / ~24 fail DD — a possible "F2 round 2."
**MUST sequence AFTER the metric fix.** Both metrics are unreliable in the SAME direction on
these rows: the "24 fail DD" is a **conservative undercount** (true DD ≥ shown; more strategies
likely fail), and a canon-sub-floor Sharpe may be a low-vol/sub-RF artifact, not bad alpha.
Re-triaging now would deprecate on numbers we know are wrong.

## HONEST HEADLINE for the operator
There is currently **no trustworthy risk-adjusted metric (Sharpe/Sortino/Calmar/DD) for ANY
strategy** — the live sizer is weighting on the distorted Sharpes right now. low_volatility_us
is NOT "bad"; its true risk-adjusted performance is **unknown** until a corrected re-backtest.
"Fix the canonical bug" = a **backtest-engine methodology change + full re-backtest of ~189
strategies** on the 2-core/8GB no-swap VPS (OOM-prone — see weekend-refresh-OOM memory). Not a
quick patch.

## Recommendation
1. **Fix the metric methodology first**, then re-backtest. Fix options (operator picks):
   (a) compute Sharpe from per-TRADE returns (mean/std of pnl_pct, annualized) — no daily series;
   (b) emit true daily mark-to-market marks from `_per_bar_simulate` (correct, bigger change);
   (c) plausibility guard — NULL Sharpe/DD when implied ann-vol < a floor (note: current
       `std<1e-9→None` guard does NOT catch the 2.6e-4 artifact; needs a min-vol floor).
   Must preserve the "Fix A" real-drawdown behavior for return_pct/curve shape.
2. **Then reconcile the mirror = Option B** (operator-chosen): retire `registry.backtest_*`,
   point gate-fallback + forensics + mastermind snapshot + research routes at canonical
   (dashboard already there). Retiring only pays off once canonical Sharpe/DD is trustworthy.
3. **Then** produce the "F2 round 2" governance sheet on corrected metrics for per-strategy sign-off.
