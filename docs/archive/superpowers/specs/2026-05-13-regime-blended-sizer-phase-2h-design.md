# Phase 2H — Per-Regime Correlation Matrices for the 2G Sidecar

**Status:** Spec — 2026-05-13
**Prereqs:** Phase 2G (correlation-adjusted sidecar, single global matrix) shipped 2026-05-12
**Mode:** DRY-RUN sidecar promotion candidate (same gate pattern as 2G)

---

## 0. Why this exists, with the data substrate measured first

Phase 2G ships a single correlation matrix blended from realized-PnL Pearson + Jaccard overlap, applied to top-N orders regardless of current market regime. That's a known simplification. The classic finance result is that correlations *change with regime*: they spike during stress and decouple during low-vol environments. A book sized against low-vol correlations underestimates risk in a crisis.

2H adds per-regime matrices and a state-probability-weighted blend at runtime, using the HMM state probabilities already emitted by `market_regime.state_probabilities`.

**Data reality (measured 2026-05-13 against last 90d of `signal_pnl`):**

| regime | trades | tickers | first | last |
|---|---|---|---|---|
| TRANSITIONING | 3,732 | 352 | 2026-04-21 | 2026-05-12 |
| LOW_VOL | 677 | 266 | 2026-04-30 | 2026-05-12 |
| HIGH_VOL | 94 | 49 | 2026-04-12 | 2026-05-12 |
| **CRISIS** | **0** | **0** | — | — |

Implication: only TRANSITIONING and LOW_VOL have enough data to compute per-pair Pearson honestly. HIGH_VOL is borderline. CRISIS we have zero data.

Today's `state_probabilities` ≈ `{TRANSITIONING: 0.998}` — so the blended-by-state output today collapses to ~the TRANSITIONING matrix. **This is by design.** Per-regime fires meaningfully only when the regime detector says we're shifting; we're building the infrastructure now so when it does, no further code change is needed.

---

## 1. Three regime-coverage classifications

Each per-regime matrix gets one of:

| classification | when | source |
|---|---|---|
| `real` | n_trades ≥ MIN_TRADES_PER_REGIME (default: 200) | Pearson on signal_pnl × regime_state |
| `fallback_global` | n_trades below threshold but > 0 | The global 2G blended matrix |
| `stress_prior` | n_trades == 0 (CRISIS today) | Flat off-diagonal at `CRISIS_CORRELATION_PRIOR` (default 0.7), env-overridable via `OPENCLAW_CRISIS_CORRELATION_PRIOR` |

`stress_prior` is the *honest* label for "we made this number up". When it dominates the blend (because CRISIS state probability rose), the sidecar log records it explicitly so the operator can decide whether to trust the size adjustment.

---

## 2. Math

Let `p(r)` = `market_regime.state_probabilities[r]`, normalized to sum to 1 across the four regimes.

Per-regime correlation matrix: `σ_r` (one of three sources above).

Effective per-cycle matrix used by the sidecar:
```
σ_eff[i,j] = Σ_r p(r) · σ_r[i,j]
```

Diagonals stay 1.0. Off-diagonals are blended linearly; this is a convex combination of valid correlation matrices, so the result remains a valid correlation matrix (positive semi-definite is not guaranteed for all weighted sums but holds for convex blends of PSD matrices).

The portfolio-Kelly φ then runs against σ_eff as it does in 2G.

---

## 3. Schema

### Migration 088 — extend `correlation_adjustments`

Two new JSONB columns. Append-only per CLAUDE.md.

```sql
ALTER TABLE correlation_adjustments
    ADD COLUMN IF NOT EXISTS regime_blend_weights JSONB,
    ADD COLUMN IF NOT EXISTS regime_coverage      JSONB;
```

- `regime_blend_weights`: `{"TRANSITIONING": 0.998, "LOW_VOL": 0.0005, "HIGH_VOL": 0.0, "CRISIS": 0.0016}` — what was actually used in the blend for that cycle.
- `regime_coverage`: `{"TRANSITIONING": "real", "LOW_VOL": "real", "HIGH_VOL": "fallback_global", "CRISIS": "stress_prior"}` — classification per regime at compute time.

NULL on rows from 2G (pre-2H) so backward-compat is intact.

---

## 4. Module changes — `src/execution/correlation_matrix.py`

Public surface additions:

```python
CRISIS_CORRELATION_PRIOR = 0.7   # env-overridable via OPENCLAW_CRISIS_CORRELATION_PRIOR
MIN_TRADES_PER_REGIME = 200      # below: fallback to global

def correlation_from_pnl_by_regime(
    tickers: list[str], window_days: int = 90,
) -> dict[str, dict[str, dict[str, float]]]:
    """Returns {regime_state: matrix}. One matrix per regime, computed from
    that regime's slice of signal_pnl × execution_signals."""

def crisis_stress_prior(tickers: list[str], rho: float = None) -> dict[str, dict[str, float]]:
    """Returns flat off-diagonal correlation matrix for tickers.
    rho defaults to CRISIS_CORRELATION_PRIOR (env-overridable)."""

def current_state_probabilities() -> dict[str, float]:
    """Reads market_regime.state_probabilities for the latest row,
    normalizes to sum-to-1 across the four regimes."""

def blended_correlation_by_state(
    tickers: list[str], window_days: int = 90, alpha: float = DEFAULT_BLEND_ALPHA,
) -> tuple[dict[str, dict[str, float]], dict[str, float], dict[str, str]]:
    """Returns (sigma_eff, blend_weights, coverage_classifications).
    Used by the sidecar."""
```

Internal logic per regime:
1. Try `correlation_from_pnl_by_regime` for the regime's data → if n_trades_in_regime ≥ MIN_TRADES_PER_REGIME, classification = 'real'
2. Else if n_trades_in_regime > 0 → classification = 'fallback_global' (use the global 2G matrix)
3. Else → classification = 'stress_prior' (use `crisis_stress_prior`)

Blend: `σ_eff[i,j] = Σ_r p(r) · σ_r[i,j]` for off-diagonals; 1.0 for diagonals.

---

## 5. Sidecar — `src/execution/correlation_adjusted_sidecar.py`

`apply_and_persist()` calls `blended_correlation_by_state()` instead of `effective_correlation()`. Persists the extra columns:

```python
cur.execute("""
    INSERT INTO correlation_adjustments
        (... existing cols ...,
         regime_blend_weights, regime_coverage)
    VALUES (... existing values ...,
            %s::jsonb, %s::jsonb)
""", (..., json.dumps(blend_weights), json.dumps(coverage)))
```

---

## 6. Doctor — `regime_correlation_coverage`

- **PASS** when every regime classification is `real` OR no orders this cycle
- **WARN** when ≥1 regime is `fallback_global` (data sparsity, expected today)
- **FAIL** when CRISIS classification is `stress_prior` AND `state_probabilities[CRISIS] > 0.30` (high crisis probability + made-up correlation = high blast radius; operator should be paged)

Reads the most recent `correlation_adjustments` row.

---

## 7. Tests

| File | Coverage |
|---|---|
| `tests/test_correlation_matrix_per_regime.py` | (1) by-regime computation with synthetic PnL data per regime; (2) crisis_stress_prior shape (diagonal 1.0, off-diag = configured rho, clipped at MAX_OFF_DIAGONAL); (3) state-prob normalization; (4) sparse-fallback dispatch (n=10 trades → fallback_global; n=0 → stress_prior); (5) env-var override of CRISIS_CORRELATION_PRIOR; (6) blended math: σ_eff[i,j] = Σ p(r)·σ_r[i,j] (verify analytically) |
| `tests/test_correlation_adjusted_sidecar_2h.py` | persist captures regime_blend_weights + regime_coverage; sidecar returns OK when CRISIS uses stress_prior with low state prob |
| `tests/test_doctor_regime_correlation_coverage.py` | PASS/WARN/FAIL tiers |

---

## 8. Rollout

1. Migration 088
2. Module changes + tests
3. Sidecar wiring + tests
4. Doctor check + tests
5. Smoke against real cycle (expect TRANSITIONING dominance today; verify blend_weights persisted)
6. Spec footer + memory update

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| CRISIS_CORRELATION_PRIOR is hand-picked, not measured | Documented; env-overridable; doctor FAILs when CRISIS prob > 0.30 *and* coverage is stress_prior — forces operator decision before the math affects live behavior |
| Per-regime computation is too sparse for HIGH_VOL today | Auto-falls back to global blended matrix; classification surfaces it |
| State-prob blending hides per-regime intent on close calls | Audit columns make it inspectable; future enhancement could log per-regime φ separately |
| Hard-floor MIN_TRADES_PER_REGIME=200 is arbitrary | Defensible default; env-overridable via `OPENCLAW_REGIME_CORRELATION_MIN_TRADES` |

---

## Implementation complete — 2026-05-13

Phase 2H shipped same-day as 2E + 2F + per-memo audit:

- Migration 088 — extends `correlation_adjustments` with `regime_blend_weights` + `regime_coverage` JSONB columns (NULL on 2G-era rows)
- `correlation_matrix.py` — `crisis_stress_prior()`, `current_state_probabilities()`, `correlation_from_pnl_by_regime()`, `_per_regime_matrices_with_coverage()`, `blended_correlation_by_state()`
- `correlation_adjusted_sizer.apply_and_persist()` — now uses blended-by-state matrix; persists weights + coverage per cycle
- Doctor `regime_correlation_coverage` (PASS all-real, WARN any fallback, FAIL stress_prior + p(CRISIS) > 30%)
- 21 new tests (15 module + 6 doctor); 37/37 regression green across 2G + 2H

**Coverage classification simplification:** dropped the `MIN_TRADES_PER_REGIME` threshold from the original draft after operator-data check showed typical cycle-sized ticker sets (3-10 names) yielding < 20 trades per regime — the threshold would have made every regime fall back to global, defeating the point. Final rule: regimes with > 0 trades among requested tickers → `real` (per-regime Pearson with SPARSE_DEFAULT for pair-sparse cells); zero-trades CRISIS → `stress_prior`; zero-trades non-CRISIS → `fallback_global`.

**E2E smoke verified 2026-05-13** against a 5-ticker test universe under TRANSITIONING:
- phi = 0.805, 5 inserted, coverage = {LOW_VOL:real, TRANSITIONING:real, HIGH_VOL:real, CRISIS:stress_prior}
- blend_weights propagated from `market_regime.state_probabilities` (99.8% TRANSITIONING + 0.16% CRISIS)
- The stress_prior contribution at 0.16% weight is ~0.0011 — sub-noise, as expected

**LIVE behavior change:** none today (TRANSITIONING dominates blend; effectively identical to 2G output). Real value-add kicks in when regime probabilities shift — particularly if CRISIS probability rises, where the 0.7 stress prior pulls σ_eff toward "correlations spike" geometry, in turn driving phi down → smaller positions during stress. Doctor FAILs that scenario above the 30% probability threshold so operator decides before the math affects production.

## 10. Out of scope

- Smoothed regime transitions (today: state probability is the smoothing; HMM's role)
- Per-regime path-MC for 2E (separate effort; 2E currently regime-agnostic)
- Operator-tunable CRISIS prior per ticker (today: flat; per-ticker would need a stress-scenario library)
