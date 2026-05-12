# Regime-Blended Sizer — Phase 2D: Bootstrap MC + Calibration + Correlation

**Status:** designed + shipping 2026-05-12 same session as 2A/2B/2C.

**Scope:** three research-grade additions, each shipped at **data-layer + reporting** depth (not yet wired into automated decisions):

1. **Bootstrap Monte Carlo for size_scalar proposals** — given (strategy, regime) historical realized PnL, what's the CI on aggregate Sharpe / mean / max-DD under a proposed size_scalar?
2. **Mastermind confidence calibration** — track stated confidence on past proposals vs realized outcomes; report calibration error.
3. **Cross-strategy signal overlap** — detect when multiple strategies fire on the same (date, ticker, regime) so future sizing can account for it.

**Explicit non-goals (rules-of-engagement):**
- **No intraday-path simulation** for stop/target/max-hold. Doing that properly needs intraday bars + scenario generation + execution-cost modeling — separate research project (Phase 2E if pursued).
- **No automatic prompt recalibration** from calibration tracking — just measure + surface. Operator decides whether to act.
- **No correlation-adjusted sizing** — just measure overlap + surface. Sizer integration is a future spec.

---

## 1. Why each piece

**MC bootstrap**: Phase 2B/2C surface a proposed `size_scalar=0.7` from Mastermind. Operator sees "20 trades, conf 0.85" but no quantitative range. Bootstrap CIs (resampling realized trade PnLs with replacement, applying the size ratio linearly, computing aggregate metrics) turn that into "under proposed 0.7x: 90% CI [Sharpe 1.1, 1.6]". Decision support, not decision automation.

**Confidence calibration**: Mastermind emits `confidence: 0.85` on every proposal. Without tracking whether high-confidence proposals actually outperform, the number is decoration. Calibration measures: among proposals Mastermind called "0.8 confidence", what fraction were materially correct in retrospect (i.e., live performance post-approval matched the proposal's direction)? Brier score / ECE.

**Cross-strategy overlap**: today's per-(strategy, regime) sizer treats every strategy as independent. If `momentum_12_1` and `S9_dual_momentum` fire on AAPL on the same day, the portfolio has 2x AAPL exposure. Measuring this overlap is the prerequisite for any future correlation-adjusted sizing.

---

## 2. Schema

### Migration 081 — `strategy_regime_mc_runs`
Cached MC results so the dashboard doesn't re-bootstrap on every load.

```sql
CREATE TABLE IF NOT EXISTS strategy_regime_mc_runs (
    id                BIGSERIAL    PRIMARY KEY,
    run_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    strategy_id       TEXT         NOT NULL,
    regime_state      TEXT         NOT NULL,
    current_size      NUMERIC,                  -- baseline size_scalar for delta calc
    proposed_size     NUMERIC,                  -- what was simulated
    n_trades_sampled  INTEGER      NOT NULL,
    n_bootstrap_iter  INTEGER      NOT NULL,
    sharpe_p05        NUMERIC,                  -- bootstrap 5th percentile
    sharpe_p50        NUMERIC,
    sharpe_p95        NUMERIC,
    mean_pnl_p05      NUMERIC,
    mean_pnl_p50      NUMERIC,
    mean_pnl_p95      NUMERIC,
    max_dd_p05        NUMERIC,                  -- worst case (deepest DD)
    max_dd_p50        NUMERIC,
    max_dd_p95        NUMERIC,                  -- best case
    proposal_id       BIGINT                    -- FK to strategy_regime_param_proposals (NULL = ad-hoc)
);

CREATE INDEX IF NOT EXISTS idx_srmcr_strategy_regime
    ON strategy_regime_mc_runs (strategy_id, regime_state, run_at DESC);
```

### Migration 082 — `mastermind_proposal_outcomes`
Tracks what actually happened to each Mastermind proposal post-decision.

```sql
CREATE TABLE IF NOT EXISTS mastermind_proposal_outcomes (
    proposal_id        BIGINT       PRIMARY KEY REFERENCES strategy_regime_param_proposals(id),
    outcome_window_days INTEGER      NOT NULL,  -- e.g. 30
    decided_at         TIMESTAMPTZ  NOT NULL,
    decision_status    TEXT         NOT NULL,   -- 'approved' | 'rejected' | 'modified'
    confidence         NUMERIC,                  -- snapshot from proposal
    live_sharpe_pre    NUMERIC,                  -- (strategy, regime) Sharpe in the N days BEFORE decision
    live_sharpe_post   NUMERIC,                  -- ... in N days AFTER decision
    live_pnl_delta     NUMERIC,                  -- pre vs post mean PnL %
    direction_match    BOOLEAN,                  -- did live behavior match proposal's intent?
    computed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mpo_decided_at
    ON mastermind_proposal_outcomes (decided_at DESC);
```

### Migration 083 — `strategy_signal_overlap`
Nightly-computed pairwise overlap from execution_signals.

```sql
CREATE TABLE IF NOT EXISTS strategy_signal_overlap (
    computed_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    window_days       INTEGER      NOT NULL,
    strategy_a        TEXT         NOT NULL,
    strategy_b        TEXT         NOT NULL,    -- always strategy_a < strategy_b (canonical order)
    regime_state      TEXT,                      -- NULL = any regime; otherwise the regime where overlap occurred
    overlap_count     INTEGER      NOT NULL,    -- shared (date, ticker, regime) signal pairs
    a_signal_count    INTEGER      NOT NULL,
    b_signal_count    INTEGER      NOT NULL,
    jaccard_idx       NUMERIC,                   -- overlap / (a + b - overlap)
    PRIMARY KEY (computed_at, window_days, strategy_a, strategy_b, regime_state)
);

CREATE INDEX IF NOT EXISTS idx_sso_strategy_a
    ON strategy_signal_overlap (strategy_a, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_sso_jaccard
    ON strategy_signal_overlap (jaccard_idx DESC, computed_at DESC);
```

---

## 3. Components

### `src/metrics/regime_param_montecarlo.py`

```python
def bootstrap_size_scalar(
    strategy_id: str,
    regime_state: str,
    current_size: float,
    proposed_size: float,
    n_iter: int = 1000,
    window_days: int = 365,
) -> dict
```

**Algorithm:**
1. Load realized PnL pct from `signal_pnl × execution_signals` for (strategy, regime), realized within `window_days`. Drops open positions.
2. Compute the size ratio `r = proposed_size / max(current_size, 0.001)`.
3. For each of `n_iter` iterations:
   - Sample N PnLs with replacement (N = len of input).
   - Multiply by `r`. (Linear-in-size assumption — explicit caveat in docstring.)
   - Compute aggregate: mean, std, Sharpe, max-DD on cumulative PnL.
4. Return `{p05, p50, p95}` for each of `sharpe`, `mean_pnl`, `max_dd`.

**Caveats** (docstring):
- Linear scaling assumes proportional size scales proportional outcome — true for delta-1 stops; not for fixed-stop strategies (gets worse with size).
- Bootstrap CI doesn't account for parameter uncertainty beyond what's in realized data.
- max-DD computed on per-bootstrap cumulative path, not real intraday path.

### `src/metrics/mastermind_calibration.py`

```python
def compute_outcome(proposal_id: int, window_days: int = 30) -> dict
def backfill_outcomes(since_days: int = 90) -> int
def calibration_report() -> dict   # Brier + confidence-bucket aggregates
```

**Outcome computation** (per proposal):
- For approved/modified: live Sharpe in `(decided_at - window_days, decided_at)` vs `(decided_at, decided_at + window_days)`.
- For rejected: just track pre/post for counterfactual.
- `direction_match`: did live performance change in the direction the proposal predicted? Boolean. Inputs: proposal's stated `confidence` + `reasoning_one_line`, post-decision Sharpe delta.

**Calibration report:**
- Buckets confidence into [0-0.2, 0.2-0.4, …, 0.8-1.0].
- Per bucket: count, fraction `direction_match`, Brier score.
- Surfaces "Mastermind says 0.8 confidence; matches 0.55 of the time" → operator knows to discount.

### `src/metrics/strategy_overlap.py`

```python
def compute_overlap(window_days: int = 90) -> int   # returns rows inserted
def latest_overlaps(top_n: int = 20) -> list[dict]
```

**Algorithm:**
1. Load `execution_signals (strategy_id, signal_date, ticker, regime_state)` within `window_days`.
2. For each `(date, ticker, regime)` group, enumerate pairs of strategies firing.
3. Aggregate to `(strategy_a, strategy_b, regime_state)` with overlap count + Jaccard.
4. Insert into `strategy_signal_overlap`.

Nightly cron (or one-off) populates. Dashboard shows top-N overlapping pairs.

---

## 4. Operator surface

### API
- `POST /api/regime-mc/:strategy/:regime` body `{current_size, proposed_size, n_iter?}` → returns CIs and persists to `strategy_regime_mc_runs`.
- `GET /api/mastermind/calibration` → calibration report (bucketed).
- `GET /api/strategy-overlap/top?limit=20` → top overlapping pairs.

### Dashboard
- **MC CI on proposals panel**: when expanding a proposal, show inline "MC simulate" button → on click POSTs to `/api/regime-mc/...` and renders Sharpe / mean / max-DD ranges.
- **Calibration sparkline**: small tile on strategies page header showing latest Brier score (info-only).
- **Overlap matrix**: collapsible panel on strategies page showing top-N pairs (informational).

### CLI
```bash
python3 -m metrics.regime_param_montecarlo --strategy <id> --regime <regime> --current 1.0 --proposed 0.7
python3 -m metrics.mastermind_calibration --backfill 90 --report
python3 -m metrics.strategy_overlap --compute --window 90
python3 -m metrics.strategy_overlap --top 20
```

### Cron
New systemd timer: `openclaw-mc-and-overlap-nightly.timer`, fires daily at 03:00 ET:
1. Computes signal overlap for window=90d.
2. Backfills any new proposal outcomes (proposals decided ≥30d ago whose outcome hasn't been computed).
3. (Optional) batch-MC-simulates pending proposals' current-vs-proposed_size.

---

## 5. Doctor checks

- **`mastermind_calibration_brier`**: PASS if Brier < 0.10, WARN [0.10, 0.20], FAIL > 0.20 (with ≥10 sample size). INFO if insufficient data.
- **`strategy_overlap_freshness`**: PASS within 26h, WARN 26-72h, FAIL >72h or empty.

---

## 6. Testing

| File | Coverage | ~Tests |
|---|---|---|
| `tests/test_regime_param_montecarlo.py` | bootstrap on canned PnL → expected medians; n_iter=0 returns empty; trade_count<10 → INSUFFICIENT; size ratio applied linearly | 5 |
| `tests/test_mastermind_calibration.py` | outcome computation: approved proposal pre vs post; direction_match logic; report bucket aggregates | 5 |
| `tests/test_strategy_overlap.py` | empty signals → 0 rows; canonical strategy_a < strategy_b ordering; jaccard arithmetic; window filter | 4 |
| `tests/test_doctor_mastermind_calibration_brier.py` | bucketed thresholds | 4 |
| `tests/test_doctor_strategy_overlap_freshness.py` | thresholds | 4 |

**Total new tests: ~22.**

---

## 7. Rollout

1. Migrations 081 + 082 + 083
2. MC bootstrap module + tests + CLI
3. Calibration module + tests + CLI
4. Overlap module + tests + CLI
5. API endpoints
6. Cron timer
7. Doctor checks
8. Dashboard tiles (MC CI on proposal expand, calibration sparkline, overlap top-N)
9. Spec footer + runbook + memory

Each step independently green-lightable.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Linear-in-size MC assumption breaks for fixed-stop strategies | Documented caveat. Phase 2E (intraday path sim) revisits. |
| Calibration sample size too small to be useful for ~6 months | INSUFFICIENT severity until we have ≥10 decided proposals with ≥30d outcome windows. |
| Overlap table grows large (98 strategies × 97 / 2 × 4 regimes ≈ 19K pairs/day) | Append-only with run timestamp; doctor warns on freshness; consumer queries latest run only. Not a real space concern for years. |
| Cron failure leaves stale overlap data | `strategy_overlap_freshness` doctor surfaces it. |
