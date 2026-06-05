# SP-6 B-flow Phase 1b — Order-Flow Predictability Kill-Test (PRE-REGISTRATION)

Date: 2026-06-05. Status: **PRE-REGISTERED — committed before any eval run** (this commit's timestamp is the lock). All features, targets, constants, signs, and verdict bars below are frozen; no tuning after first eval.

## 1. What Phase 1 tested — and what it did not

- Phase 1's sign-flip oracle excess tested the **direction→path channel**: does the daily
  alpha's direction correlate with intraday drift? Verdict KILL stands **for that channel only**.
  This *vindicates* the operator's architecture: the engine must not treat daily direction as a
  path prediction (daily information, daily horizon — structurally cannot predict intraday).
- The sign-flip excess for a long is `(2·P_eod − P_min − P_max)/P_eod` — a pure function of
  where the terminal price sits in the day's range. It **differences away any
  direction-symmetric timing edge** (longs buying local dips and shorts selling local rips
  accrue to BOTH the directed and flipped oracle). That symmetric edge is exactly what an
  order-flow engine exploits. A hindsight oracle — however null-adjusted — cannot test
  predictability; only a **causal estimator** can.
- Operator's actual hypothesis (B-flow thesis): minute-scale order flow, plus only the intended
  direction (to know which side to optimize), predicts forward intraday returns well enough to
  beat EOD-dump submission. **Null hypothesis**: intraday prices are a martingale w.r.t. the
  minute-flow filtration ⇒ by optional stopping, NO causal entry rule beats EOD in expectation,
  and net of costs it loses. Phase 1b tests this null directly.

## 2. Data and scope

- Existing minute-bar cache only (`data/cache/min_bars/`, 34 sessions 2026-04-13..2026-06-02,
  fields o/h/l/c/v/vw). **No new data fetch.**
- Test-A universe: all cached (ticker, session) pairs passing the Phase-1 60-valid-bar floor
  (valid bar: vw>0, v>0, h>=l, NaN-guard idiom `not (x > 0)`).
- Test-B universe: the Phase-1 primary-grain included intents (3,867).
- **Pre-stated conclusion asymmetry**: minute OHLCV signed flow (tick rule) is a *proxy* for
  true signed order flow, but minute granularity is the operator's stated engine input. A
  positive result ⇒ strong GO for Phase 2 (tick + Hawkes = sharper estimator of the same
  object). A null result ⇒ KILL at minute scale; tick-scale thesis INCONCLUSIVE-and-discouraged,
  not disproven.

## 3. Test A — predictive structure (GATING)

Features at minute t (trailing-only; NaN if any required trailing bar invalid or window
incomplete; sign(0)=0; minute 0 uses Δc = c−o of the same bar):

- **f1 `ofi_5`**  = Σ_{i=t−4..t} sign(c_i − c_{i−1})·v_i / Σ_{i=t−4..t} v_i   ∈ [−1, +1]
- **f2 `ofi_15`** = same with a 15-minute window
- **f3 `vwap_disp_30`** = (c_t − VWAP_{t−29..t}) / VWAP_{t−29..t}, VWAP = Σ v·vw / Σ v

Targets (forward simple returns from c_t):

- **PRIMARY**: `ret_to_dump` = P_eod_dump / c_t − 1, evaluated for t ∈ [30, 330]
  (P_eod_dump = Phase-1 dump benchmark: minute≥385 vol-weighted vw)
- SECONDARY: `ret_fwd_k` = c_{t+k} / c_t − 1 for k ∈ {5, 15, 30, 60}, t ∈ [30, 389−k]

Statistic: per-session pooled Spearman rank IC (all valid (ticker, minute) observations in the
session → one IC per session per cell), then across-session mean IC and
t = mean(IC) / (sd(IC)/√n_sessions). Session = the cluster; no pooled SEs.

**Pre-registered sign (reversion thesis — the operator's mechanism "wait for high-volume sells
before going long")**: IC < 0 for f1, f2, f3 on all targets (net sell flow / price below
recent VWAP ⇒ positive forward return). If significance arrives with the OPPOSITE sign
(flow momentum), that is **not a kill** — it means flow timing works with the rule inverted
(buy *into* buy-flow); reported as a first-class finding.

## 4. Test B — causal proxy policy (SUPPORTIVE, NOT GATING; all constants frozen here)

LONG intent: scan t ≥ 30. **Arm** at minute b where `ofi_5(b) ≤ −0.6` AND
Σ_{b−4..b} v ≥ 1.5 × 5 × (trailing-30-min mean minute volume). **Trigger** = first
t ∈ (b, b+10] with c_t > c_{t−1}; if none within 10 minutes, disarm and keep scanning.
**Entry fill = vw_{t+1}** (decision at end of minute t, fill in the next minute — causal).
**Forced fallback**: dump fill (P_eod_dump) if no entry by minute 384. SHORT mirrors
(`ofi_5 ≥ +0.6`, buy-burst, first down-minute, sell at vw_{t+1}).

Metric: per-intent Δbps vs P_eod_dump, net of the Phase-1 differential spread cost (entry
minute's spread vs dump-window spread, same min(0.5·(h−l)/vw·1e4, 50)+2bps parity model);
per-session means; clustered t across sessions. Also report: trigger rate, fallback rate,
entry-minute histogram. **Momentum variant** (enter WITH the burst) = DIAGNOSTIC ONLY, never
gating, pre-registered as such.

## 5. Verdict bar (PRE-COMMITTED)

- **GO** (Phase 2 tick + Hawkes authorized): ≥2 of 3 features show |t| ≥ 3 on the PRIMARY
  target with consistent sign across all significant features, AND the same sign appears with
  |t| ≥ 2 at ≥2 secondary horizons (coherence requirement — a lone max-t survivor does not GO).
  Sign = reversion ⇒ operator's rule architecture as designed; sign = momentum ⇒ GO with the
  rule inverted.
- **KILL** (minute scale; tick scale = inconclusive/discouraged): all PRIMARY |t| < 2 AND
  Test-B Δ session-mean ≤ 0.
- Otherwise **WEAK** ⇒ operator call.

## 6. Outputs

- `analysis/bflow_phase1b_report.md` — verdict + full IC grid (mean IC, t, n_sessions per
  cell) + sign-vs-prereg match + Test-B economics + data-quality table.
- `analysis/bflow_phase1b_ic_grid.parquet` (per-session per-cell ICs),
  `analysis/bflow_phase1b_policy.parquet` (per-intent policy results).

## 7. Module layout

`src/research/bflow/flow_features.py` (pure features/targets), `predictability.py` (Test A),
`flow_policy.py` (Test B), `run_phase1b.py` (runner + report). Reuses `oracle.dump_benchmark`,
spread/cost machinery, `minbar_cache.get_session_bars` (no-fetch mode), `order_set` modules.
TDD; suite runs sequential `nice -n 19` (2-core box).
