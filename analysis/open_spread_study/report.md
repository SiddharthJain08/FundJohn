# SP-6 — Open-Window Spread-Cost Study

**Spec**: docs/superpowers/specs/2026-06-08-sp6-open-spread-cost-study-prereg.md

Quantity: incremental NBBO half-spread (open 09:31-09:32 ET vs close 15:55-15:56 ET) on max_hold exits. incr = open_hs − close_hs. net = gross_edge − incr.

## Long (n=1465)

| metric | mean | median | p25 | p75 | p90 |
|---|---|---|---|---|---|
| open_hs | 12.6274 | 3.9875 | 0.6681 | 18.4549 | 35.8705 |
| close_hs | 1.7448 | 0.9334 | 0.5600 | 1.8379 | 3.7484 |
| incr | 10.8826 | 2.8894 | 0.0010 | 16.4192 | 32.5707 |

## Short (n=1566)

| metric | mean | median | p25 | p75 | p90 |
|---|---|---|---|---|---|
| open_hs | 16.9590 | 9.7838 | 1.4895 | 25.2422 | 42.8884 |
| close_hs | 2.0395 | 1.3582 | 0.6244 | 2.5814 | 4.4806 |
| incr | 14.9195 | 8.0106 | 0.3467 | 22.4199 | 39.4581 |

## Net edge (gross − incr)

Gross edges (from Probe ①): LONG=0.098 bps, SHORT=1.94 bps

| leg | net_mean | net_median |
|---|---|---|
| long_net | -10.7846 | -2.7914 |
| short_net | -12.9795 | -6.0706 |

**VERDICT: PARK**

Decision linkage: PARK -> open-exit channel closed with real cost data (spreads eat the only favorable leg); SHIP-SHORTS-CANDIDATE -> separate shorts-only live-lane design decision (NOT longs — long_net <= 0 by construction); MARGINAL -> operator call; likely not worth the 3-seam live build for sub-bp net. net = gross_edge − incremental_spread.
