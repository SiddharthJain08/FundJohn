# SP-6 Deep-Dip Buy-Leg Feasibility Study

**Prereg**: docs/superpowers/specs/2026-06-08-sp6-deep-dip-buy-feasibility-prereg.md

Deep-THROUGH buy limit (50 bps below the open). Unlike the at-touch passive sell, a fill here is genuine (the market crosses the level), so a positive is NOT inherently live-shadow-only — the over-fill artifact is confined to the touch-boundary fraction, reported below.

Sessions analysed: 813  (threshold for VALID: 600)

Decision number = blended improvement at DIP = 50 bps.

---
## DIP = 25 bps

| quantity | mean bps | t | n_sessions |
|----------|----------|---|------------|
| **improvement (blended)** | -7.8450 | -3.312 | 813 |
| improvement \| fill | -5.5178 | -1.971 | 813 |
| improvement \| no-fill | -1.0932 | -1.120 | 813 |
| open→close drift (context) | -1.3293 | -0.469 | — |

- fill_rate: 0.779
- touch_fraction (over-fill-ambiguous share of fills): 0.015

---
## DIP = 50 bps  ⟵ PRIMARY (verdict)

| quantity | mean bps | t | n_sessions |
|----------|----------|---|------------|
| **improvement (blended)** | -4.7226 | -2.207 | 813 |
| improvement \| fill | -0.1310 | -0.046 | 813 |
| improvement \| no-fill | -1.1693 | -1.210 | 813 |
| open→close drift (context) | -1.3293 | -0.469 | — |

- fill_rate: 0.632
- touch_fraction (over-fill-ambiguous share of fills): 0.018

---
## DIP = 100 bps

| quantity | mean bps | t | n_sessions |
|----------|----------|---|------------|
| **improvement (blended)** | -2.8798 | -1.624 | 813 |
| improvement \| fill | +5.5251 | +1.813 | 813 |
| improvement \| no-fill | -1.1524 | -1.183 | 813 |
| open→close drift (context) | -1.3293 | -0.469 | — |

- fill_rate: 0.390
- touch_fraction (over-fill-ambiguous share of fills): 0.020

---
## Verdict

(DIP = 50 bps — the operator's 0.5%.)

- blended improvement: -4.7226 bps (t -2.207, n=813)
- fill_rate: 0.632
- touch_fraction: 0.018

**VERDICT: KILL**

### Decision linkage

- **KILL**: even with the realized open as a free reference, the deep-dip buy loses to the close baseline — continuation after the dip and/or too-rare fills dominate. The buy leg of the final structure is closed WITH REAL DATA.
- **MEASURED-POSITIVE**: a genuine, bar-measurable conditional edge (t≥3, fills predominantly clear-through). Unlike the at-touch passive sell, NOT inherently live-shadow-only → warrants a realizability follow-up (PM-spread HS_C∈{1,3}, touch-band discount, open-estimation error). NOT an automatic go-live.
- **MARGINAL**: positive but t<3 OR touch_fraction≥0.5 (over-fill-tainted) → operator call.
- **INVALID-DATA**: < 600 eligible sessions.
