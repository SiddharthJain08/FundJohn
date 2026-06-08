# Probe ① — Intraday-Session Return (longs-only open-exit gate)

**Spec**: docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md

Quantity: intraday_return=(close-open)/open on max_hold-LONG exit days. Open-exit edge for a long = -E[intraday_return]. Asymmetric veto.

## Headline (day-clustered)

- PRIMARY (max_hold-long): mean -0.0977 bps | t -0.0996 | n_days 2509
- SECONDARY (equity universe): mean +0.6647 bps | t +0.3806 | n_days 2556
- M2 relative (PRIMARY - same-day universe mean): mean -0.5962 bps | t -0.4361 | n_days 2509
- PRIMARY rows (ticker x exit_date): 45530

## By regime (PRIMARY)

| regime | mean bps | t | n_days |
|---|---|---|---|
| CRISIS | -1.472 | -0.227 | 150 |
| HIGH_VOL | -3.194 | -1.077 | 411 |
| LOW_VOL | +0.042 | +0.035 | 887 |
| TRANSITIONING | +1.172 | +0.783 | 1060 |

## By half-year (PRIMARY)

| bucket | mean bps | t | n_days |
|---|---|---|---|
| 2016H1 | +10.551 | +1.315 | 17 |
| 2016H2 | -4.845 | -1.154 | 127 |
| 2017H1 | +1.706 | +0.639 | 125 |
| 2017H2 | +1.314 | +0.637 | 126 |
| 2018H1 | +0.216 | +0.047 | 125 |
| 2018H2 | -6.061 | -1.542 | 126 |
| 2019H1 | +7.843 | +2.940 | 124 |
| 2019H2 | -2.643 | -0.713 | 128 |
| 2020H1 | -1.127 | -0.192 | 125 |
| 2020H2 | -1.439 | -0.368 | 128 |
| 2021H1 | +2.014 | +0.438 | 124 |
| 2021H2 | -2.172 | -0.463 | 128 |
| 2022H1 | -4.060 | -0.672 | 124 |
| 2022H2 | -4.266 | -0.803 | 127 |
| 2023H1 | +3.541 | +0.665 | 124 |
| 2023H2 | +2.094 | +0.383 | 126 |
| 2024H1 | +1.377 | +0.412 | 124 |
| 2024H2 | -3.107 | -0.766 | 128 |
| 2025H1 | +5.042 | +0.950 | 123 |
| 2025H2 | -0.583 | -0.148 | 128 |
| 2026H1 | +2.855 | +0.731 | 102 |

Decision rule (spec §1.4): NO-GO iff PRIMARY pooled t>=+3.0, OR any of the two most-recent half-years t>=+2.0. Else CLEAR (CAUTION if pooled mean>0). INVALID-DATA iff n_days<500.

**VERDICT: CLEAR-TO-SHIP-GATED**

Decision linkage: NO-GO -> close-exit stands for longs, question closed. CLEAR(-WITH-CAUTION) -> proceed to the gated live-structure spec/plan (longs-only open-exit, >=9:31 marketable-limit/TIF=day + close fallback, forward-confirm on live fills). Net cost ratified by live fills only.
