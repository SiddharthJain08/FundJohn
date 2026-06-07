# SP-6 B-flow Phase 1b — Order-Flow Predictability Kill-Test Report

**VERDICT: GO**
- sign_regime: reversion
- primary survivors (|t|>=3 on ret_to_dump): ofi_5, ofi_15, vwap_disp_30
- sign_regime=reversion (shared primary sign negative)
- coherence met: >=1 survivor has |t|>=2 with the shared sign in >=2 of 4 secondary horizons

## Pre-registration framing
Pre-registration framing: a null here = KILL at minute scale, the tick thesis is inconclusive-and-discouraged (not disproven). This verdict was committed before any eval run (spec §2/§3/§5).
Test A is the ONLY gate (the clustered across-session t on the minute-flow features vs forward returns). Test B is supportive, NOT gating: it feeds only the KILL conjunct (reversion-variant across-session mean delta <= 0).

## PRIMARY grid — feature vs ret_to_dump (GATING)
| feature | mean IC | t | n_sessions | sign-vs-prereg |
|---|---|---|---|---|
| ofi_5 | -0.024 | -30.663 | 813 | reversion (matches prereg) |
| ofi_15 | -0.035 | -27.541 | 813 | reversion (matches prereg) |
| vwap_disp_30 | -0.047 | -23.362 | 813 | reversion (matches prereg) |

## SECONDARY grid — t at 4 forward horizons x 3 features
| feature | ret_fwd_5 | ret_fwd_15 | ret_fwd_30 | ret_fwd_60 |
|---|---|---|---|---|
| ofi_5 | -16.316 | -14.749 | -14.978 | -16.189 |
| ofi_15 | -12.171 | -11.172 | -11.347 | -13.516 |
| vwap_disp_30 | -14.686 | -11.883 | -10.846 | -12.687 |

## OOS confirmation grid (sessions >= 2027-01-01)
Headline OOS — post-arming, physically leak-proof (spec AMENDMENT 2026-06-05). n_oos_sessions = 0.
no OOS sessions yet (first expected 2027-01-01)

## Contemporaneous supplement (sessions after 2026-06-02 and before 2027-01-01)
supplement, not headline OOS: harness was built 2026-06-05, these sessions cannot claim leak-proof status. n_supplement_sessions = 0.
no supplement sessions (none after 2026-06-02 and before 2027-01-01)

## Test-B economics (causal proxy policy; SUPPORTIVE, NOT GATING)
### Reversion variant (the pre-registered rule — HEADLINE)
- across-session mean delta vs P_eod_dump: n/a (clustered t = n/a, n_sessions = 0)
- trigger rate: n/a  |  fallback rate: n/a  (n_intents = 0)
- entry-minute histogram (triggered entries, minutes from 09:30):
  - 0-29: 0
  - 30-89: 0
  - 90-329: 0
  - 330-374: 0
  - 375-389: 0

### Momentum variant (DIAGNOSTIC ONLY — NON-GATING)
Enter WITH the burst (arm OFI sign flipped). Pre-registered as diagnostic, never gating; reported for completeness only.
- across-session mean delta vs P_eod_dump: n/a (clustered t = n/a, n_sessions = 0)
- trigger rate: n/a  |  fallback rate: n/a

## Data-quality
- Test-A cache sessions: 813 present, 0 missing
- Test-A per-session tickers passing the 60-valid-bar floor (median): 489
- Test-B intents: 3867 included primary (grain=='primary' AND exclude_reason is null); expected 3867 on real data (MATCH).
- Test-B intents with no cached bars (counted, not dropped): 3867
- Test-B missing cache sessions: 31 (2026-04-13, 2026-04-14, 2026-04-15, 2026-04-17, 2026-04-20, 2026-04-21, 2026-04-22, 2026-04-23, 2026-04-24, 2026-04-27, 2026-04-28, 2026-04-29, 2026-04-30, 2026-05-01, 2026-05-04, 2026-05-05, 2026-05-06, 2026-05-07, 2026-05-08, 2026-05-11, 2026-05-13, 2026-05-14, 2026-05-15, 2026-05-18, 2026-05-21, 2026-05-22, 2026-05-26, 2026-05-27, 2026-05-28, 2026-05-29, 2026-06-02)
- per-session-cell observation floor: NONE (spec §3 fixes no per-cell minimum; every finite (feature,target) pair in the session counts toward its cell's IC). A cell is NaN only when its pool is empty or its ranked feature/target is constant (intrinsically-undefined correlation).
- 60-valid-bar floor: a (ticker, session) participates in Test A only with >= 60 valid bars (vw>0, v>0, h>=l); the Phase-1 floor reused verbatim.
- Test-B fallback rates — reversion n/a, momentum n/a (a fallback intent entered AT the dump -> delta 0bps, honestly diluting the mean toward 0).
