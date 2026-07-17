# Phase 2E — Intraday-Path Monte Carlo for Stop/Target/Max-Hold

**Status:** Spec — 2026-05-13
**Prereq:** Phase 2D (linear-scaling MC) shipped 2026-05-12
**Out of scope:** automatic prompt recalibration (Phase 2F), correlation-adjusted sizing (Phase 2G shipped 2026-05-12)

---

## 0. Why this exists, in one paragraph

Phase 2D's MC bootstrap resamples realized PnLs and applies a size ratio multiplicatively. That's correct for proportional-stop strategies (where bigger size → proportionally bigger loss at the stop) but **wrong for fixed-stop strategies**: a strategy with a $50 max-loss stop costs $50 regardless of position size, so doubling size doesn't double the stop loss in dollar terms — it changes the risk-reward calculus entirely. 2E adds path-dependent simulation: instead of resampling realized outcomes, simulate intraday return paths and apply the proposed (stop_pct, target_pct, max_hold_days) policy bar-by-bar to derive realized PnL under that policy. Operator sees: **"under proposed policy, 90% CI Sharpe [0.8, 1.4] with stop_hit_rate=0.32"** vs 2D's "Sharpe [1.1, 1.6]" — the gap is the cost of the size assumption.

---

## 1. The data substrate (verified 2026-05-13)

- `data/master/prices_30m.parquet`: **5 tickers** — AAPL, MSFT, NVDA, SPY, TSLA. ~2,300 rows in last 90d.
- `data/master/prices.parquet`: **443 tickers** in last 90d, daily OHLC + volume.
- `execution_signals` + `signal_pnl`: 4,503 closed trades, 363 distinct tickers in last 90d.

Implication: the 5 tickers with 30m bars get **empirical-path** MC (resample real intraday sequences). The 358 other traded tickers get **synthetic GBM-path** MC (calibrate μ and σ from daily realized vol, simulate intraday paths). A `path_source` column on each MC run records which generator produced the paths.

This is a forced architectural choice, not a placeholder. Backfilling 30m bars for the full universe is a separate multi-week ingestion project (Polygon paid tier, 358 × 90 = 32K names×days of API calls); not in scope for 2E.

---

## 2. Schema

### Migration 085 — `strategy_regime_intraday_mc_runs`

Mirrors 081 (`strategy_regime_mc_runs`) but with path-MC fields. Kept separate so the dashboard can render linear-MC vs path-MC side-by-side and the operator can compare directly.

```sql
CREATE TABLE IF NOT EXISTS strategy_regime_intraday_mc_runs (
    id                  BIGSERIAL    PRIMARY KEY,
    run_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    strategy_id         TEXT         NOT NULL,
    regime_state        TEXT         NOT NULL,
    current_size        NUMERIC,
    proposed_size       NUMERIC,
    proposed_stop_pct   NUMERIC,
    proposed_target_pct NUMERIC,
    proposed_max_hold_days INTEGER,
    n_trades_sampled    INTEGER      NOT NULL,
    n_bootstrap_iter    INTEGER      NOT NULL,
    path_source         TEXT         NOT NULL,    -- 'empirical' | 'gbm' | 'hybrid'
    sharpe_p05          NUMERIC,
    sharpe_p50          NUMERIC,
    sharpe_p95          NUMERIC,
    mean_pnl_p05        NUMERIC,
    mean_pnl_p50        NUMERIC,
    mean_pnl_p95        NUMERIC,
    max_dd_p05          NUMERIC,
    max_dd_p50          NUMERIC,
    max_dd_p95          NUMERIC,
    stop_hit_rate       NUMERIC,     -- fraction of iters that hit proposed stop
    target_hit_rate     NUMERIC,     -- fraction that hit proposed target
    max_hold_hit_rate   NUMERIC,     -- fraction that timed out at max_hold
    proposal_id         BIGINT
);

CREATE INDEX IF NOT EXISTS idx_srimcr_strategy_regime
    ON strategy_regime_intraday_mc_runs (strategy_id, regime_state, run_at DESC);
```

---

## 3. Module — `src/metrics/intraday_path_montecarlo.py`

```python
class EmpiricalPathGen:
    """Resamples 30m return sequences from data/master/prices_30m.parquet
    for tickers with coverage. Each path = N_bars contiguous returns
    starting from a random RTH bar within the window."""
    def __init__(self, ticker: str, window_days: int = 90)
    def sample_path(self, n_bars: int, rng: random.Random) -> list[float]

class GBMPathGen:
    """Synthesizes GBM intraday paths calibrated to daily realized vol.
    σ_intraday = σ_daily / sqrt(13) (13 30m bars per RTH day).
    μ_intraday = μ_daily / 13."""
    def __init__(self, ticker: str, window_days: int = 90)
    def sample_path(self, n_bars: int, rng: random.Random) -> list[float]

def _choose_gen(ticker: str, window_days: int):
    """Returns EmpiricalPathGen if ticker has ≥20 30m bars in window;
    GBMPathGen otherwise. Caches by ticker."""

def apply_policy(path: list[float], entry: float,
                  stop_pct: float, target_pct: float,
                  max_hold_bars: int, direction: str) -> tuple[float, str]:
    """Walks the path bar-by-bar applying stop/target/max-hold.
    Returns (realized_return_pct, exit_reason)
    exit_reason ∈ {'stop', 'target', 'max_hold'}"""

def run_path_mc(strategy_id: str, regime_state: str,
                 current_size: float, proposed_size: float,
                 proposed_stop_pct: float, proposed_target_pct: float,
                 proposed_max_hold_days: int,
                 n_iter: int = 1000,
                 window_days: int = 90) -> dict:
    """End-to-end MC: load this strategy's trades in regime, simulate paths,
    apply policy, collect (Sharpe, mean, max_dd) per iteration, percentiles."""

def persist_run(result: dict, proposal_id: Optional[int] = None) -> int
```

**MC loop per iteration:**
1. Sample a closed trade from the strategy×regime trade pool (uniform).
2. Get the trade's ticker; pick the appropriate path generator.
3. Generate a path of length `max_hold_bars` (= max_hold_days × 13).
4. Apply policy → (realized_return_pct, exit_reason).
5. Scale by `proposed_size / current_size`.
6. Record return.

After N iters: aggregate Sharpe / mean / max-DD percentiles + exit-reason rates.

**Path-dependent max-DD:** for each iter, the path's running min-from-peak drawdown is what counts, not just the close — captures intra-trade drawdown that would have stopped a manual operator out.

---

## 4. API + Dashboard

### `/api/regime-mc-intraday/:strategy/:regime` (POST)

Body: `{ current_size, proposed_size, proposed_stop_pct, proposed_target_pct, proposed_max_hold_days, n_iter? }`
Response: full MC result dict + `linear_mc_diff` (delta vs the latest linear-MC run for the same proposal).

### Dashboard surface

On proposal-expand: side-by-side panel showing linear-MC CI bars and path-MC CI bars. If max-DD-p95 diverges by >20% between methods, flag with a "size assumption matters" badge.

---

## 5. Doctor

### `intraday_mc_freshness`

- **PASS**: every pending proposal has a path-MC row within 26h
- **WARN**: stale 26-72h
- **FAIL**: stale >72h OR a pending proposal older than 7d that touches
  `proposed_stop_pct` / `proposed_target_pct` / `proposed_max_hold_days`
  has no path-MC row. Pure size-scalar proposals are *not* gated by this
  check — linear MC (Phase 2D) is sufficient decision support for them.

Runs piggyback on the nightly Phase 2D cron.

---

## 6. Tests

| File | Coverage | Tests |
|---|---|---|
| `tests/test_intraday_path_montecarlo.py` | GBM gen calibration, empirical gen resampling, stop-hit detection (LONG + SHORT), target-hit, max-hold sentinel, percentile aggregation, gen dispatch (empirical vs GBM), zero-trade INSUFFICIENT path | ~10 |
| `tests/test_doctor_intraday_mc_freshness.py` | threshold tiers | 3 |

---

## 7. Rollout

1. Migration 085
2. Module + tests + CLI
3. API endpoint
4. Doctor check
5. Smoke run against a recent strategy×regime with real trades
6. Spec footer + memory update

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| GBM is the wrong process for many strategies (jumps, mean-reversion, asymmetric tails) | `path_source` column makes this visible; future Phase 2E.1 can swap in stochastic-vol or jump-diffusion. Documented caveat. |
| 5-ticker empirical coverage is too narrow for sweeping conclusions | Acknowledged. 2E surfaces the path-vs-linear delta per-proposal so operator can see when it matters. |
| Path-MC at n_iter=1000 might be slow (~5s/run × 92 active strategies × 4 regimes = ~30min sweep) | Run on-demand per proposal, not all-strategies-by-default. Cron handles only proposals decided in last 30d. |
| Linear-scaling assumption is also wrong in 2D; surfacing the gap could undermine confidence in 2D values | That's the point. If gap is big, 2D's CI is noise. If small, 2D's faster method is fine. |

---

## 9. Out of scope (deferred)

- Multi-leg / spread strategies (path generator is single-ticker)
- Slippage modeling (assumes mid-price fills)
- Cross-asset correlation in path generation (Phase 2G handles portfolio-level)
- Backfilling 30m bars for full universe — separate ingestion project
