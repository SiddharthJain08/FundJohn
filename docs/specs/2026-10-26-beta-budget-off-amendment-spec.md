# Amendment — Beta budget OFF: SPY competes on its own conviction (2026-10-26)

**Operator directive (2026-10-26 20:40Z):** "spy is getting too much weight consistently. It should be treated in the
same way as all other names with the only exception being that its conviction is not reduced by beta_spy sharpes. This
keeps it as the full book when no other positions qualify while reducing its weight when other names are signaled."

## 1. What was measured (live, 2026-10-13 … 10-26)
Every cycle the beta budget (spec 2026-08-30) redirected the conviction rule C removed from the alpha names into SPY and
the result exceeded NAV, so SPY clamped to 1.00×NAV every day regardless of breadth:

| date | regime | alpha names after gate | SPY share of Σ|w| WITHOUT budget (`beta_share_after`) | WITH budget | SPY target → clamp |
|---|---|---|---|---|---|
| 10-13 | TRANSITIONING | 62 (58 dropped) | 0.494 | 0.970 | 123,772 → 94,522 (1.00×NAV) |
| 10-14 | LOW_VOL | 92 | 0.027 | 0.626 | 104,652 → 92,911 |
| 10-20 | LOW_VOL | 88 | 0.027 | 0.620 | 102,765 → 92,109 |
| 10-23 | HIGH_VOL | 109 | 0.072 | 0.613 | 49,742 (under cap) |
| 10-26 | LOW_VOL | 40 | 0.046 | 0.569 | 92,946 → 90,801 |

The `beta_share_after` column is exactly the book the operator describes: SPY sized on its own un-hurdled S_adj inside
the same λ·NAV normalizer as every alpha name (rule C alone). Realized: `bench_realized` 2026-10-26 book −8.8 % vs SPY
−0.6 % since 09-08 — recorded, not a reason to keep the budget; the operator's call is explicit.

## 2. Decisions
- **D-A** `OPENCLAW_BENCH_BETA_BUDGET=0` (rule C stays `OPENCLAW_BENCH_RELATIVE_SIZING=1`). No redirect pool; SPY's weight
  = its own S_adj (the sleeve's weight := S_m, Amendment 1) — never hurdled by S_m (spec 2026-08-29 §2.5, unchanged).
  Alpha names stay hurdled: w = sign·(|S_adj| − S_m). The shadow line keeps printing `beta_budget=shadow pool=… beta_share_budget=…`
  so the counterfactual stays visible.
- **D-4′ (supersedes D-4 "budget-only")** The `benchmark_max_nav_frac` clamp (default 1.0) applies whenever rule C applied
  this cycle, budget on or off. Without it, a day on which rule C drops every alpha name hands SPY the whole λ·NAV gross
  (1.8×NAV levered); the directive's "full book" means 1.00×NAV — the reference portfolio is unlevered. Shaved, not
  redistributed. QP-shadow cascade (`bench_clamp`) follows the same key.
- **D-B** Benchmark exemptions from the per-ticker cap, cluster cap and D9 similarity are KEPT (still gated on rule C).
  "Treated the same as all other names" is read as *same normalizer, own conviction*; applying the 0.10·(|S|+1)·λ·NAV
  per-ticker cap to SPY would contradict "full book when nothing else qualifies" (it would bound SPY near 37 % NAV).
- **D-C (flagged, NOT changed)** `bench_corr_removal` (operator directive 2026-09-04: drop alpha names with |ρ(t,SPY)| ≥ 0.60
  outright) was premised on "the budget holds the market through SPY". With the budget off that premise is gone and the rule
  only deletes alpha (1–7.6 % NAV/day in the last two weeks: 10-22 17 names, 10-23 17 names, 10-26 VRT $5.4k). Recommended
  follow-up: `OPENCLAW_BENCH_CORR_REMOVAL=0`. Separate operator decision.
- **Backtest side** never had a beta budget (no mirror in `src/backtest`), so this change moves live TOWARD the backtest book.

## 3. Expected effect (from the shadow column above)
LOW_VOL with 40–90 alpha names: SPY ≈ 3–5 % of gross ≈ 5–8 % of NAV (λ 1.8); alpha gross rises from ≈$15–30k toward
λ·NAV. TRANSITIONING with most names hurdled out: SPY ≈ 50 % of gross ≈ 90 % NAV. Every alpha name dropped: SPY = 1.00×NAV
(D-4′). First live cycle: 2026-10-27 14:00Z intraday redeploy (if a regime flip) or 19:00Z compute; the book moves at the
19:55Z execute. Watch the `bench_sizing.apply[…]` line (`beta_budget=shadow`), the `clamped` line (should appear only on
dropped-everything days) and `bench_realized`.

## 4. Rollback
`OPENCLAW_BENCH_BETA_BUDGET=1` in `.env` + `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service`. D-4′ is
behaviour-preserving under the budget (the budget always implied rule C), so no code revert is needed.

## 5. Code / tests
`src/execution/regime_blended_sizer.py`: `_bench_hurdle_applied` keys the §3.4 clamp and the QP cascade `bench_clamp`;
INFO line `rule C dropped every alpha name … benchmark alone carries the book, clamped at f×NAV` (budget-off analogue of
"beta budget moved the whole book to beta"). Tests: `tests/execution/test_sizer_benchmark_hurdle_wiring.py` (D-4′ cases,
15 pass), `test_sizer_benchmark_cap_exemptions.py` (now pins the budget flag; was red since 09-01 via .env leak).
