# SP-6 B-flow Phase-1f — Intraday Drift Atlas Report

**Spec**: docs/superpowers/specs/2026-06-07-sp6-bflow-phase1f-drift-atlas-prereg.md

**Decision linkage**: descriptive; nothing goes live; feeds the open-fill backtest variant.

## Summary

- n_sessions (eligible): 805
- n_sessions (skipped/missing): 0
- MIN_VALID_MINUTES: 300  |  N_XS_MIN: 50  |  MIN_SESSIONS: 700

**Note on the uniform-accrual null**: Uniform-accrual systematic: dev_sys(m) ≈ −(3/389)·m·b, b = pooled curve_gross(0)/386 — a monotone ramp, typically sub-0.1bps, but its t-significance is INDEPENDENT of magnitude (dev is collinear in m across sessions), so FLAT/TIMING-STRUCTURE must be read against the printed dev_sys column, not against dev≡0.

## Pre-named test minutes

| minute | mean gross (bps) | t(gross) | mean net (bps) | mean cost (bps) | mean dev (bps) | t(dev) | dev_sys (bps) |
|---|---|---|---|---|---|---|---|
| 5 | -0.8953 | -0.3229 | -5.5889 | +4.6937 | +0.4321 | +0.7856 | +0.0001 |
| 15 | -1.3034 | -0.4778 | -3.3301 | +2.0267 | -0.0106 | -0.0127 | +0.0004 |
| 30 | -1.4286 | -0.5650 | -2.4623 | +1.0337 | -0.1877 | -0.1770 | +0.0008 |
| 60 | -0.7356 | -0.3074 | +0.3325 | -1.0681 | +0.4016 | +0.3201 | +0.0016 |
| 120 | -0.5664 | -0.2681 | +1.9371 | -2.5036 | +0.3634 | +0.2669 | +0.0032 |
| 180 | -0.8197 | -0.4157 | +2.3734 | -3.1931 | -0.0973 | -0.0700 | +0.0048 |
| 240 | -0.5615 | -0.3942 | +2.8470 | -3.4085 | -0.0464 | -0.0390 | +0.0064 |
| 300 | -1.5269 | -1.4204 | +1.7648 | -3.2917 | -1.2192 | -1.2308 | +0.0081 |
| 330 | -2.0768 | -2.3273 | +1.1238 | -3.2006 | -1.8729 | -2.2957 | +0.0089 |

## Named shapes (descriptive)

### (i) Shorts at the open — m ∈ {0..5}

| minute | t(gross) |
|---|---|
| 0 | -0.4706 |
| 1 | -0.5113 |
| 2 | -0.5443 |
| 3 | -0.6614 |
| 4 | -0.6210 |
| 5 | -0.3229 |

- Significantly negative (t ≤ −3.0 at ≥4 of 6): NO (n=0)

### (ii) Longs slightly after the open — local minimum m ∈ [10, 45]

- argmin minute: 31
- min gross value: -1.6598 bps
- curve(5) value: -0.8953 bps
- deeper than curve(5): YES
- |t(dev)| ≥ 3.0 at argmin: NO

## Bucket diagnostics — mean dev at TEST_MINUTES (non-gating)

| bucket | 5 | 15 | 30 | 60 | 120 | 180 | 240 | 300 | 330 |
|---|---|---|---|---|---|---|---|---|---|
| 2023H1 | +1.0448 | +0.1933 | -1.0780 | -1.5947 | +0.1397 | -0.8669 | +0.2453 | +0.1392 | -3.3802 |
| 2023H2 | -1.8706 | +0.2041 | -1.5083 | +0.2353 | -2.9527 | -4.0401 | -2.2591 | -2.2475 | -4.1541 |
| 2024H1 | +1.5150 | +1.0722 | +3.5705 | +2.4640 | +3.5989 | +3.8027 | +2.5459 | +1.5447 | +1.3764 |
| 2024H2 | +1.4982 | +0.3958 | -2.1050 | +1.3001 | -0.1605 | -2.1550 | -3.0776 | -1.1829 | -0.4534 |
| 2025H1 | +3.0301 | -0.1903 | +2.9577 | -0.0402 | +0.9001 | +2.1362 | +2.4506 | -2.9809 | -0.5689 |
| 2025H2 | -3.1345 | -2.0064 | -4.4838 | -1.5165 | +0.9121 | -1.5008 | -2.1375 | -2.5584 | -3.3482 |
| 2026Q1 | +1.5947 | +0.5539 | +3.1091 | +3.5784 | -0.1419 | +4.1800 | +4.0915 | -1.3155 | -3.2696 |

## Verdict

**VERDICT: FLAT**

- TIMING-STRUCTURE ⟺ ≥2 ADJACENT pre-named points with |t(D)| ≥ 3 and the same sign.
- FLAT = no such adjacent pair.
- INVALID-DATA = n_sessions < 700.

Decision linkage (prereg §2): NOTHING goes live from this. TIMING-STRUCTURE feeds the open-fill backtest variant design; FLAT means fixed-time tweaks are not worth backtest-variant complexity beyond the plain open[t+1] case.