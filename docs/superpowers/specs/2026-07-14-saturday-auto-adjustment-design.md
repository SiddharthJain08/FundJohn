# Saturday Auto-Adjustment + Sharpe-Weighted Brackets + Per-Position S_adj — Design

Date: 2026-07-14 · Operator directive (Discord): make the Saturday strategy-adjustment
process fully automatic; switch stacked TP/SL from max-take/min-stop to
effective-Sharpe-weighted; add per-position corr-adjusted cumulative Sharpe to the
dashboard portfolio tiles.

## Context (as-built today)

The Saturday adjustment chain is `openclaw-weekend-saturday.service` →
`src/maintenance/weekend_saturday.sh`:
review (memos) → critique → position-recs → **backtest-coupling** → backtest refresh →
candidate tuner → weights → panels → ladder sentinel. The timer is currently **dead**
(stopped 2026-06-27 for the §7 re-backtest; re-backtest + flush complete 2026-07-14).

- Bracket/hold deltas (incl. stop widening) already route through
  `execution/backtest_coupled_recs.py` (gate ON in prod), but only apply on
  ΔSharpe ≥ **0.10** — smaller genuine improvements are rejected.
- Live stop-replacement of open positions (`alpaca_replace_stop.py`, gate ON) fires in
  `position_recommender.js` **before** the backtest validates the delta, and multiplies
  the stop **price** by (1+δ) — which *tightens* a long stop for a positive "widen" δ
  (semantic bug vs. the memo contract "δ is relative to stop distance").
- Size/eligibility deltas land in `strategy_regime_param_proposals` and wait for
  operator clicks; `proposal_manager.auto_approve()` exists but is env-gated OFF and
  has no caller.
- Size deltas in `strategy_sizing_recommendations` flow to Monday's handoff regardless
  of memo confidence.
- Dashboard (Strategies → "⚙️ Strategy Adjustments") already shows Applied-this-week
  (coupling, with ΔSharpe) + a Pending queue with approve/reject.
- `bracket_stacking.stacked_bracket` combines across factor blocks with
  tp = max(blocks), stop = min(blocks).
- Per-ticker S_adj (`gate_net_sharpe`) is computed in the sizer each cycle and
  **discarded** (log-only).

## W1 — Fully-automatic Saturday adjustments

### 1a. Coupling accepts any measured Sharpe improvement
`backtest_coupled_recs`: `MIN_DELTA 0.10 → 0.0` with **strict** comparison
(`candidate_sharpe − baseline_sharpe > 0`); `MIN_TRADES = 30` unchanged. The
backtest is the sole arbiter for stop/target/max-hold — confidence is irrelevant here.

### 1b. Stop replacement moves *after* coupling, with correct geometry
- `position_recommender._applyStopReplacements` becomes permanently report-only
  (digest preview retained).
- `backtest_coupled_recs.run` — after a rec APPLIES and `cand_stop` is not None:
  for each recent filled `alpaca_submissions` row of that strategy
  (14d, filled/partial, has stop + entry), re-anchor the stop to the **validated
  distance from the original entry**: long → `entry × (1 − cand_stop)`;
  short → `entry × (1 + cand_stop)`. Calls `alpaca_replace_stop.replace_stop_for_coid`
  (still honours `OPENCLAW_ALPACA_LIVE_REPLACE`). Broker rejections are logged, never
  fatal. Skipped under `--dry-run`.

### 1c. Proposal auto-apply at confidence > 0.8
New `proposal_manager.auto_apply_batch()` + CLI `--auto-apply-batch`, invoked as step
4b of `weekend_saturday.sh` (before weights rebuild so new scalars/eligibility flow
into the same weekend's weights):
- pending proposals with `confidence > OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE`
  (set to **0.8**, strict >) → `auto_approve()` (existing rails; applied via the same
  `set_params` path as dashboard approval, `source='auto-approval'`).
- confidence ≤ 0.8, NULL, or rail-skipped → `status='noted'` with the reason.
- `supersede_pending` widens to `status IN ('pending','noted')` — next Saturday's
  fresh proposal for the same (strategy, regime) supersedes the noted one =
  the "re-evaluated later" loop.
- `_lock_for_decision` accepts `('pending','noted')` so the operator can still act on
  noted items from the dashboard.
- Env adds: `OPENCLAW_PROPOSAL_AUTOAPPROVE=1`, `…_MIN_CONFIDENCE=0.8`,
  `…_MAX_SIZE_DELTA=2.0` (non-binding given the 0..2 scalar domain — full automation
  per directive; anything a rail skips degrades to 'noted', visible on the tab).

### 1d. Sizing recs carry confidence; low-confidence size deltas are noted
Migration 142 on `strategy_sizing_recommendations`:
- `confidence NUMERIC` (from the memo recommendations block),
- `coupling_outcome TEXT` ('applied'/'rejected'/NULL),
- CHECK on `action_taken` widened with `'noted'`.

`position_recommender` inserts `action_taken = confidence > 0.8 ? 'pending' : 'noted'`.
`trade_handoff_builder.load_mastermind_rec` already filters
`action_taken IN ('pending','applied')` → noted size recs never reach Monday's handoff.
Coupling reads `action_taken IN ('pending','noted') AND coupling_outcome IS NULL`
(bracket deltas are backtested **regardless of confidence**) and writes
`coupling_outcome` + a reasoning note **without** touching `action_taken` — this
decouples the size flow from the bracket decision (previously a coupling reject
silently killed the size rec too).

### 1e. Dashboard tab = Recent Changes + Noted
Keep the "⚙️ Strategy Adjustments" panel; reframe:
- **Recent changes**: `/api/regime-proposals/applied` widens from
  `source='saturday_coupling'` to `source IN ('saturday_coupling','auto-approval',
  'dashboard','cli')`, returns `source` + size/eligibility before/after; UI badges the
  source and shows ΔSharpe where present (coupling rows).
- **Noted**: new `GET /api/regime-proposals/noted` returning noted proposals +
  noted sizing recs (unified shape); UI table titled "Noted (low-confidence —
  re-evaluated next Saturday)" keeps approve/reject buttons for proposals.

### 1f. Re-activation
`systemctl enable --now openclaw-weekend-saturday.timer` (Sat 12:00 UTC). The
Saturday standalone candidate re-backtest also runs Saturdays; both are `nice -19`
and internally serialized — acceptable contention, noted for observation.

## W2 — Effective-Sharpe-weighted TP/SL (`bracket_stacking.stacked_bracket`)

Within-block representative selection is unchanged (top-effective-Sharpe member —
prevents double-counting near-duplicate strategies). Across block representatives
`r = 1..k` with `s_r = max(eff_sharpe_r, 0)` and `ω_r = s_r / Σ s_r`
(equal weights if `Σ s_r = 0`):

```
stop_pct* = Σ ω_r · stop_pct_r      tp_pct* = Σ ω_r · tp_pct_r
t2_pct*   = Σ ω'_r · t2_pct_r  over reps with finite t2 (ω' renormalized);
            None if none; clamped beyond t1 in the trade direction.
```

Levels rebuilt around the top-Sharpe rep's entry anchor (unchanged).

**Why this formulation** (mathematical consistency): the merged position is the sum of
sub-positions the sizer would allocate ∝ effective Sharpe (`strategy_weights`:
`weight = effective_sharpe`). If sub-position `r` (dollar size `q_r ∝ s_r`) exits at
level `L_r`, aggregate exit proceeds are `Σ q_r L_r = Q · Σ ω_r L_r` — the single
bracket that **replicates the aggregate exit value** is exactly the size-weighted mean
of levels. Because all levels share one entry anchor, weighting pct-gaps ≡ weighting
levels. Gaps are already horizon-normalized (÷√cadence) upstream when
`OPENCLAW_STRATEGY_CADENCE_STOP_NORM` is on, so the mean operates on comparable
single-day-equivalent geometry. Negative-Sharpe contributors get zero exit influence,
matching their (deflated) entry influence.

Consumers unchanged (same dict shape). `_select_bracket` fallback (stacking OFF / no
substrate) unchanged. Tests pinning max/min combine are rewritten to pin the weighted
combine; the stale-failing `test_sizer_cadence_stop_norm` float-equality asserts move
to isclose and its outdated sum-cap comments are corrected.

## W3 — Per-position corr-adjusted cum Sharpe on portfolio tiles

- Sizer (`sharpe_cadence` path) attaches `corr_cum_sharpe = gate_net_sharpe[ticker]`
  (signed S_adj) to each emitted equity order.
- Migration 143: `cycle_contributing_strategies.corr_cum_sharpe NUMERIC` (additive);
  `_persist_contributing_strategies` upserts it per (run_date, ticker).
- `/api/portfolio/positions` enriches each row with the latest
  `corr_cum_sharpe` per ticker (`DISTINCT ON (ticker) … ORDER BY ticker, run_date DESC`).
- UI: tile face gets an `S_adj` badge; the expanded alpha-bars panel shows it beside
  the `net contrib` badge. Value = conviction at the most recent sizing cycle that
  included the ticker (daily-fresh while held; last-sized value once dropped).

## Out of scope / follow-ups
- Backtest engine does not use `bracket_stacking` — no backtest change.
- Option consolidation path untouched (inert; no live option strategies).
- Memo prompt (`stop_delta_pct` semantics) unchanged — coupling interprets it as a
  distance-relative delta, as originally specified.

## Testing
- `test_bracket_stacking.py` rewritten for weighted combine (incl. zero/negative
  Sharpe fallback, t2 renormalization, short mirror).
- `test_sizer_cadence_stop_norm.py` updated (weighted expectations + isclose).
- `test_backtest_coupled_recs*` updated for strict >0 gate, coupling_outcome writes,
  noted-row inclusion, stop-replacement anchoring (CLI stubbed).
- New `proposal_manager` tests: batch split at 0.8 (strict), noted supersede,
  noted lockable.
- JS: `node --check` on touched files; dashboard endpoints smoke-tested via curl.

## Rollback
- Brackets: revert `bracket_stacking.py` (pure module).
- Coupling gate: restore `MIN_DELTA=0.10`.
- Auto-apply: `OPENCLAW_PROPOSAL_AUTOAPPROVE=0` (step 4b becomes a no-op).
- Tiles: column is additive; UI badge renders only when the value exists.
