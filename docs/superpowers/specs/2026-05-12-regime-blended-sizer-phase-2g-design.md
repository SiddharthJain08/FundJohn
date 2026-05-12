# Regime-Blended Sizer — Phase 2G: Correlation-Adjusted Portfolio Sizer

**Status:** designed 2026-05-12 same session as 2A-2D, immediately after LIVE flip. Shipping in DRY-RUN sidecar mode; promotion to production behind a second env flag once parity validates.

**Scope:** add a portfolio-level correlation adjustment layer on top of the current per-(strategy, regime) sizer. Today's sizer treats each strategy as independent → when correlated strategies fire on the same ticker, total exposure stacks (Jaccard 0.71 between S25_dual_momentum variants per 2D smoke). This phase computes effective correlation, computes a portfolio-Kelly downsize factor, and **logs the proposed adjustment alongside production sizing**. Operator promotes via a second env flag once parity is validated.

**Out of scope** (Phase 2H+):
- Full mean-variance optimization (turnover cost modeling)
- Cross-asset correlation across regimes (correlation matrix interpolation)
- Live correlation matrix updates intra-cycle (we recompute once per cycle from rolling N-day window)

---

## 1. Why now (and the risk framing)

LIVE flipped today. Production behavior is now `regime_blended_sizer_live`, which treats every strategy independently. The 2D overlap smoke surfaced that the 5 momentum strategies form a Jaccard 0.71 cluster — when 3 of them fire on AAPL on the same day, the portfolio has effectively 3× AAPL exposure with corresponding tail risk.

Phase 2G can't be rolled in-place into a freshly-LIVE sizer — that would be two confounded changes at once. Standard pattern: **DRY-RUN sidecar** runs the new logic in parallel, captures the delta vs production, logs to `parity_orders` (the same mechanism Phase 1 used for the deterministic→regime-blended migration). Once the operator has a few weeks of clean parity data showing the sidecar produces sensible adjustments, second env flag promotes it to production.

---

## 2. Architecture

```
                                  signals (regime_gate filtered)
                                              │
                                              ▼
                                ┌─────────────────────────────┐
                                │ regime_blended_sizer_live   │  PRODUCTION (LIVE)
                                │ per-strategy independent     │  submits to Alpaca
                                │ Kelly + regime scalars       │
                                └──────────────┬──────────────┘
                                               │ orders
                                               │
                          ┌────────────────────┴───────────────────┐
                          │                                        │
                          ▼                                        ▼
              alpaca_submissions                  parity_orders (source='regime_blended')
                                                                  │
                                                                  ▼
            ┌─────────────────────────────────────────────────────────────────────┐
            │   correlation_adjusted_sizer (NEW, DRY-RUN ONLY in 2G)              │
            │                                                                     │
            │   inputs:  same signals + production orders                         │
            │   outputs: adjusted_orders (same shape, qty/notional scaled down    │
            │            for correlated positions) + adjustment_log               │
            │                                                                     │
            │   pipeline:                                                         │
            │     1. Read strategy_signal_overlap latest snapshot                 │
            │     2. Build per-cycle correlation matrix (1 row/col per ticker     │
            │        touched in production orders)                                │
            │     3. Compute portfolio Kelly downscale factor per ticker          │
            │     4. Apply factor → adjusted_orders                               │
            │     5. Persist to parity_orders source='correlation_adjusted'       │
            └─────────────────────────────────────────────────────────────────────┘
                                                                  │
                                                                  ▼
                                                       Daily diff: parity_diff_correlation.py
                                                       Posts {production_total, adjusted_total,
                                                              per-ticker delta} to #botjohn-log
```

**Key property:** ZERO production behavior change in 2G. Sidecar only writes to `parity_orders`. After 14-30 days of clean parity (operator review), flip `OPENCLAW_CORRELATION_ADJUSTED_LIVE=1` to promote.

---

## 3. Math

For each cycle (typed in pseudo-LaTeX):

1. **Effective correlation matrix** $\Sigma$ over tickers $\{t_1, ..., t_n\}$ touched by production orders this cycle:
   - For each pair $(t_i, t_j)$: pull realized PnL pct from `signal_pnl × execution_signals` joined-by-date over rolling window (default 90d). Compute Pearson correlation.
   - Diagonal = 1. Off-diagonal: clip to $[-0.95, 0.95]$ to avoid singular matrices.
   - Default to 0.3 ("moderately correlated") if insufficient overlap data exists for a pair.

2. **Strategy-overlap-weighted correlation**: blend $\Sigma$ with the strategy_signal_overlap Jaccard matrix:
   $$\Sigma_{\text{effective}} = \alpha \cdot \Sigma_{\text{pnl}} + (1-\alpha) \cdot \Sigma_{\text{overlap}}$$
   where $\alpha = 0.6$ (default) puts more weight on realized-PnL correlation, less on signal-overlap (which is a proxy).

3. **Portfolio Kelly downsize factor**:
   For each ticker $t_i$ with production notional $n_i$ and proposed direction (LONG/SHORT signed as $+1/-1$):
   - Build the "exposure vector" $\vec{w}_i = n_i \cdot \text{sign}_i$.
   - Compute the implied risk-contribution: $\sigma_p^2 = \vec{w}^T \Sigma_{\text{effective}} \vec{w}$.
   - Compute the **independent-baseline** variance: $\sigma_{\text{ind}}^2 = \sum_i (n_i)^2$ (treats every ticker as uncorrelated).
   - Downsize factor: $\phi = \min(1.0, \sqrt{\sigma_{\text{ind}}^2 / \sigma_p^2})$.
   - $\phi < 1$ when correlations are net-positive (concentrating risk); $\phi = 1$ when correlations cancel out; we never UPSIZE (clamp).

4. **Apply per-ticker**: adjusted notional $\tilde{n}_i = n_i \cdot \phi$. Adjusted qty rounds down.

**Caveats** (documented in code):
- Pearson correlation on sparse data is unstable; the 0.3 default + clipping is deliberately conservative.
- The model doesn't account for non-linear interactions (option positions, leveraged ETFs).
- No turnover/borrow cost — Phase 2H if pursued.

---

## 4. Schema

### Migration 084 — `correlation_adjustments`

```sql
CREATE TABLE IF NOT EXISTS correlation_adjustments (
    id                BIGSERIAL    PRIMARY KEY,
    computed_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    cycle_id          TEXT,                        -- correlate with parity_orders cycle key
    ticker            TEXT         NOT NULL,
    production_qty    NUMERIC,
    production_notional NUMERIC,
    direction         TEXT,                         -- 'LONG' | 'SHORT'
    portfolio_kelly_phi NUMERIC,                    -- the downsize factor applied
    adjusted_qty      NUMERIC,
    adjusted_notional NUMERIC,
    correlation_input JSONB                         -- captured Σ_effective slice + neighbor list
);

CREATE INDEX IF NOT EXISTS idx_corr_adj_cycle
    ON correlation_adjustments (cycle_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_corr_adj_ticker
    ON correlation_adjustments (ticker, computed_at DESC);
```

---

## 5. Components

### `src/execution/correlation_matrix.py`
- `compute_ticker_correlation_pnl(tickers, window_days=90) -> np.ndarray | dict-of-dict`
- `compute_ticker_correlation_overlap(tickers) -> dict-of-dict` (from `strategy_signal_overlap` projected to tickers via execution_signals)
- `blend(sigma_pnl, sigma_overlap, alpha=0.6) -> dict-of-dict`

No numpy required — keep as plain dict-of-dict for testability. We only need diagonal + clipped off-diagonal.

### `src/execution/correlation_adjusted_sizer.py`
- `adjust(production_orders, sigma_effective) -> (adjusted_orders, per_ticker_log)`
- `apply_and_persist(production_orders, cycle_id) -> dict` end-to-end

### `src/execution/parity_diff_correlation.py`
- Daily diff job at 21:30 UTC (after the existing 21:00 UTC parity_diff)
- Reads `parity_orders` source='regime_blended' (production) + `parity_orders` source='correlation_adjusted' or `correlation_adjustments` (sidecar) for the day
- Computes per-ticker notional delta + portfolio-level downsize summary
- Posts to `#botjohn-log` channel

### Sidecar hook in orchestrator
- After production `regime_blended_sizer_live` submits orders, invoke `correlation_adjusted_sizer.apply_and_persist(orders, cycle_id)`. Non-fatal: failure here logs WARN but doesn't affect the cycle.

---

## 6. Operator surface

### CLI
```bash
python3 -m execution.correlation_adjusted_sizer --cycle <id>      # one-off run for a past cycle
python3 -m execution.correlation_matrix --tickers AAPL,MSFT,GOOG  # debug
python3 -m execution.parity_diff_correlation                       # daily diff
```

### Dashboard
- New collapsible panel on dashboard: "Correlation Adjustment (DRY-RUN)" — shows latest cycle's per-ticker phi factor; flags tickers where phi < 0.8 (significant downsize).
- API: `GET /api/correlation-adjustments?since=2026-05-12` returns recent adjustments.

### Doctor checks
- `correlation_adjustment_freshness`: PASS within 26h of LIVE cycle, WARN 26-72h, FAIL >72h.
- `correlation_sidecar_drift`: PASS when (mean per-cycle phi) > 0.85, WARN [0.7, 0.85], FAIL < 0.7 — signals systematic over-correlation in production sizing.

---

## 7. Promotion path (post-2G)

`OPENCLAW_CORRELATION_ADJUSTED_LIVE=1` (default OFF). When enabled, orchestrator's trade step submits the **adjusted** orders to Alpaca instead of production's. Same single-env-flag pattern as the original LIVE flip.

**Recommended promotion criteria** (advisory only, not gated):
- ≥14 days of sidecar parity_diff with no spurious phi < 0.5 (anything below half is suspicious)
- Mean phi across cycles in 0.85-1.0 range (small adjustments are expected; large adjustments need investigation)
- Operator has reviewed at least 5 cycles' diffs and is satisfied

---

## 8. Testing

| File | Coverage | ~Tests |
|---|---|---|
| `tests/test_correlation_matrix.py` | PnL correlation + overlap correlation + blend; insufficient data → 0.3 default; clipping at ±0.95 | 6 |
| `tests/test_correlation_adjusted_sizer.py` | identity case (no correlation → phi=1.0); fully correlated long-only → phi=1/sqrt(N); offsetting longs/shorts → phi=1.0; clamp at 1.0 (never upsize); empty orders → empty result | 6 |
| `tests/test_parity_diff_correlation.py` | per-ticker delta computation; no production rows → no-op; summary aggregates | 4 |
| `tests/test_doctor_correlation_freshness.py` | thresholds | 3 |
| `tests/test_doctor_correlation_sidecar_drift.py` | thresholds; sparse data → INFO | 3 |

**Total new tests: ~22.**

---

## 9. Rollout

| # | Step | Risk |
|---|---|---|
| 1 | Migration 084 | none |
| 2 | `correlation_matrix.py` + tests | none |
| 3 | `correlation_adjusted_sizer.py` + tests (sidecar logic only) | none |
| 4 | Orchestrator hook (non-fatal sidecar invocation after production submit) | sidecar failure must NOT affect production submit — explicit try/except |
| 5 | `parity_diff_correlation.py` + cron units | none |
| 6 | Dashboard panel + API | UI only |
| 7 | Doctor checks | none |
| 8 | First live cycle: observe sidecar output, validate phi values are sensible | operator review |
| 9 | (Future) `OPENCLAW_CORRELATION_ADJUSTED_LIVE=1` flip after parity stable | operator decides |

Each step independently shippable. No production-behavior change anywhere in 2G's 9 steps.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Sidecar bug crashes orchestrator | try/except around the entire sidecar call; production submit completes first |
| Correlation matrix singular (perfectly correlated pair) | clip off-diagonal at ±0.95; default sparse pairs to 0.3 |
| Sidecar consumes too much CPU per cycle | cap ticker count at top-N by notional; lazy matrix construction |
| Operator promotes before sufficient parity data | promotion is explicit env flag; runbook documents minimum review window |
| Phi clamping at 1.0 prevents upsize when correlations cancel | deliberate — never increase position size from this layer; conservative bias |

---

## 11. Deferred: 2E and 2F

Both still spec'd but not pursued this session:

**Phase 2E** (intraday-path MC for stop/target): needs intraday bars + execution-cost modeling + scenario library. Multi-day effort. Spec at `docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2d-design.md` §10 (referenced).

**Phase 2F** (Mastermind prompt recalibration loop): cheap to build but needs ≥6 months of decided proposals first. Don't build until calibration data exists. Spec at `docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2c-design.md` §10 (referenced).

Both deferred per operator decision 2026-05-12 (this session).
