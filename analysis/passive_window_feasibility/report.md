# SP-6 Passive Window Feasibility Study

**Prereg**: docs/superpowers/specs/2026-06-08-sp6-passive-window-feasibility-prereg.md

Kill-only instrument: minute bars over-fill passive limits and cannot see adverse selection or queue position.  A NEGATIVE result is a decisive KILL; a POSITIVE result is INCONCLUSIVE — it would only motivate a live shadow of real passive fills, never a go-live decision.

Sessions analysed: 813  (threshold for VALID: 600)

---
## hs = 2.0 bps  (hs_close = 1.0 bps)

### Improvement metrics (bps vs close baseline, session-clustered)

| metric | mean bps | t | n_sessions |
|--------|----------|---|------------|
| sell_naive | +3.1137 | +1.150 | 813 |
| sell_oracle | +87.2563 | +51.162 | 813 |
| buy_mkt_naive | -1.8103 | -0.695 | 813 |
| buy_mkt_oracle | +29.0625 | +11.075 | 813 |
| buy_pass_naive | +1.4407 | +0.534 | 813 |
| combined_hybrid | +1.3035 | +1.731 | 813 |
| combined_all_passive | +4.5545 | +7.152 | 813 |

### Fill rates

| fill event | rate |
|------------|------|
| sell_fill_morning | 0.965 |
| sell_fill_afternoon | 0.028 |
| buy_pass_fill | 0.977 |
| sell_fill_total (morning OR afternoon) | 0.993 |

### COMBINED views

| combined view | mean bps | t | n_sessions |
|---------------|----------|---|------------|
| HYBRID (passive-sell + marketable-buy) | +1.3035 | +1.731 | 813 |
| ALL-PASSIVE (passive-sell + passive-buy) | +4.5545 | +7.152 | 813 |

---
## hs = 5.0 bps  (hs_close = 1.0 bps)

### Improvement metrics (bps vs close baseline, session-clustered)

| metric | mean bps | t | n_sessions |
|--------|----------|---|------------|
| sell_naive | +4.7460 | +1.798 | 813 |
| sell_oracle | +87.2563 | +51.162 | 813 |
| buy_mkt_naive | -4.8105 | -1.847 | 813 |
| buy_mkt_oracle | +26.0715 | +9.933 | 813 |
| buy_pass_naive | +2.2382 | +0.832 | 813 |
| combined_hybrid | -0.0645 | -0.087 | 813 |
| combined_all_passive | +6.9842 | +10.919 | 813 |

### Fill rates

| fill event | rate |
|------------|------|
| sell_fill_morning | 0.913 |
| sell_fill_afternoon | 0.063 |
| buy_pass_fill | 0.927 |
| sell_fill_total (morning OR afternoon) | 0.976 |

### COMBINED views

| combined view | mean bps | t | n_sessions |
|---------------|----------|---|------------|
| HYBRID (passive-sell + marketable-buy) | -0.0645 | -0.087 | 813 |
| ALL-PASSIVE (passive-sell + passive-buy) | +6.9842 | +10.919 | 813 |


**Calibration check (§3, hs=5):** buy_mkt_naive = -4.811 bps (expected ≈ −2 to −5 bps) → **PASS**

---
## hs = 8.0 bps  (hs_close = 1.0 bps)

### Improvement metrics (bps vs close baseline, session-clustered)

| metric | mean bps | t | n_sessions |
|--------|----------|---|------------|
| sell_naive | +5.2737 | +2.085 | 813 |
| sell_oracle | +87.2563 | +51.162 | 813 |
| buy_mkt_naive | -7.8107 | -2.998 | 813 |
| buy_mkt_oracle | +23.0805 | +8.790 | 813 |
| buy_pass_naive | +1.4061 | +0.524 | 813 |
| combined_hybrid | -2.5371 | -3.367 | 813 |
| combined_all_passive | +6.6797 | +9.860 | 813 |

### Fill rates

| fill event | rate |
|------------|------|
| sell_fill_morning | 0.837 |
| sell_fill_afternoon | 0.107 |
| buy_pass_fill | 0.856 |
| sell_fill_total (morning OR afternoon) | 0.945 |

### COMBINED views

| combined view | mean bps | t | n_sessions |
|---------------|----------|---|------------|
| HYBRID (passive-sell + marketable-buy) | -2.5371 | -3.367 | 813 |
| ALL-PASSIVE (passive-sell + passive-buy) | +6.6797 | +9.860 | 813 |

---
## Verdict

(Evaluated at hs = 5.0 bps — the realistic level per the prereg.)

- sell_naive mean: +4.7460 bps
- combined_hybrid mean: -0.0645 bps
- combined_all_passive mean: +6.9842 bps

**VERDICT: INCONCLUSIVE-LEAN-SELL**

### Decision linkage

- **PARK**: buy drag dominates even this over-optimistic model; the hybrid (passive-sell + marketable-buy) is closed with the same finality as the marketable spread study.  No further work on this hybrid.
- **INCONCLUSIVE-LEAN-SELL**: the sell-side upper bound exceeds the buy drag.  The ONLY warranted next step is a **live shadow of real passive sells** (or quote/queue data).  This is NOT a green light.  Do not go-live; do not proceed to queue-aware bar-sim (same data limit).  The all-passive variant (passive buys too) is the candidate for that shadow.
- **INVALID-DATA**: fewer than 600 eligible sessions; result is unreliable.

> **Caveat (§3 — pre-committed):** minute bars KILL-only.  positive = inconclusive, needs live shadow.  Bars cannot resolve passive provision, queue position, or adverse selection.
