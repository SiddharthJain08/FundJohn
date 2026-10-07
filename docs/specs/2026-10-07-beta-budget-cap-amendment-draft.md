# Beta budget — SPY NAV cap: DRAFT amendment (2026-10-07, operator asked "will it be static or dynamic"; controller recommendation: NOT applied)

## Facts from the live book (2026-10-05 … 10-07, TRANSITIONING, S_m ≈ 0.57–0.59, λ = 1.8)
| date | alpha signals | tickers in play | dropped by rule C | pool (conviction redirected) | SPY share of Σ|w| after budget | SPY target → clamp | alpha gross target |
|---|---|---|---|---|---|---|---|
| 10-05 | 14 | 11 | 6 | 5 | 0.44 | 111,689 → 95,437 (1.00×NAV) | — |
| 10-06 | 11 | 12 | 8 | 5 | 0.49 | 115,937 → 96,036 | — |
| 10-07 | 10 | 10 | 6 | 4.7 | 0.50 (0.88 under budget) | 113,858 → 95,710 | 15,350 (16 % NAV) |
Through 2026-10-02 the pool was 24–28 tickers and SPY's share 5–15 %; the collapse to 4–5 is signal flow (6–8 alpha strategies emit
10–14 signals on an ordinary day; calendar strategies add 50+ on their days), not the universe changes or lazy aux loading (no drift
lines; the engine ran 105/105 strategies each day).

The `alpha_nav = equity − SPY market value` figure (~$48 on 10-07) is NOT the alpha book: with SPY at 1.00×NAV the alpha positions
(~$15k) sit on margin above it. The account breaker measures alpha P&L from the positions themselves, so it is unaffected.

## What the cap is and is not
`pipeline_config.benchmark_max_nav_frac` (migration 152, static, 1.0) is a CEILING on the SPY sleeve. SPY's share below it is already
dynamic — it is whatever conviction rule C redirects. Lowering the ceiling to 0.78 would reduce SPY to 78 % NAV and leave the
difference in CASH; it would not add alpha, because alpha is bounded by excess conviction (today $15k against 0.8×NAV of unused
leverage headroom). The 77.7 % in the 08-30 spec was the simulated outcome of that day's conviction, not a target.

## Option kept for the record — a regime-dependent SPY ceiling (dynamic in the only sense that matters)
cap(regime) = {LOW_VOL: 1.00, TRANSITIONING: 1.00, HIGH_VOL: 0.80, CRISIS: 0.50}, applied where the 1.00×NAV clamp is applied today,
read from pipeline_config (`benchmark_max_nav_frac_by_regime`, JSON; fallback to the scalar). Effect: in HIGH_VOL/CRISIS the sleeve is
cut regardless of conviction and the remainder is cash; in calm regimes nothing changes. Rationale: the sleeve's own protection is a
regime exit hook + a −40 % stop; a regime-tiered ceiling adds a pre-emptive de-risk without touching rule C. Cost: cash drag whenever
the regime model is wrong. Shadow-able: log `bench_cap[regime]=…` beside the clamp line for two weeks before applying.

## Recommendation
Leave 1.00 in place. The lever on alpha participation is activation/conviction — the universe corrections (10-05..07) and the parity
epoch re-gate (10-07 → ~10-12) act on exactly that; read the epoch report first. Revisit a regime-tiered ceiling after that, as a
separate operator decision.
