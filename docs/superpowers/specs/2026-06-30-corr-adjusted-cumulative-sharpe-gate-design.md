# Correlation-Adjusted Cumulative-Sharpe Conviction Gate — Design

**Date:** 2026-06-30
**Branch:** `feat/corr-adjusted-cumsharpe-gate` (off `feat/intraday-regime-15min-prefetch` @ `ad56acd`)
**Status:** Design approved (operator), pre-implementation
**Touches:** `src/execution/orthogonalization.py`, `src/execution/regime_blended_sizer.py`, `src/channels/api/server.js`, new migration `140_*`, tests.

---

## 1. Problem & intent

Today the production sizer (`regime_blended_sizer._sharpe_cadence_path`) computes **two different** per-ticker quantities:

- **Conviction gate** (ticker selection): `ticker_net_sharpe[tkr] = Σ effective_sharpeᵢ · dᵢ` — a *naive signed sum* of raw effective Sharpe. When `OPENCLAW_STRATEGY_CORR_WEIGHT=1` (live) it is passed through `orthogonalization.deflated_net_sharpe`, a **heuristic** within-factor-block deflation (`block_conviction` floor-preserving toward the max via `k_eff`), full additive credit across blocks. Gated vs the per-regime floor `min_cumulative_sharpe` (default 3.0, `regime_sizer_params`, migration 108, bound [1.0, 10.0]).
- **Sizing weight** (position $): `ticker_w[tkr] = Σ daily_weightᵢ · dᵢ` where `daily_weight = effective_sharpe/√cadence`. `target_usd ∝ ticker_w`.

The operator wants:
1. Replace the naive signed sum / block heuristic with a **mathematically consistent, correlation-adjusted cumulative Sharpe** — the *Sharpe-weighted combination Sharpe*.
2. Make that **one** quantity drive **both** the conviction gate (ticker selection) **and** the sizing weight.
3. Keep the conviction gate as the **ticker-selection authority**, with its per-regime floor **controlled/reflected on the dashboard** exactly as `min_cumulative_sharpe` is today.

## 2. The quantity (`S_adj`)

Per ticker, over its post-fold contributing strategies `i` (each with signed direction `dᵢ ∈ {+1,−1}` and **cadence-normalized weight** `wᵢ = effective_sharpeᵢ / √cadenceᵢ` = `daily_weight`):

```
S_adj(tkr) = ( Σᵢ wᵢ²·dᵢ )  /  den
where  q   = Σᵢⱼ wᵢ·wⱼ·dᵢ·dⱼ·ρᵢⱼ          (ρᵢᵢ = 1; ρᵢⱼ from the per-regime
                                            strategy_similarity_matrix; missing → 0.05)
       den = √q                if q > ε    (no floor — full diversification credit)
       den = √( Σᵢ wᵢ² )       if q ≤ ε    (non-PSD backstop → "assume independent"; warn + count)
```

This is the **Sharpe-weighted** (`weight ∝ wᵢ`) combination Sharpe — MV-optimal-*flavored* but **inverse-free** (no `ρ⁻¹`, which is unsafe on a heuristic, non-PSD similarity matrix). It is **signed** (numerator carries direction, so opposing strategies cancel and conflict tickers gate out — the operator's "conflicting information cancels" invariant is preserved).

**Verified limit behavior** (`wᵢ` shorthand `w`):

| Case | `S_adj` | Meaning |
|---|---|---|
| Single strategy | `w₁·d₁` | **= today's solo `ticker_w` exactly** — solo tickers unchanged in sizing |
| N equal-`w`, same dir, ρ=0 | `√N·w` | √N diversification credit |
| N equal-`w`, same dir, ρ=1 | `w` | duplicate gets **zero** extra credit |
| Unequal uncorrelated (5,1) | `√26 ≈ 5.10` | `= √(Σwᵢ²)` — Sharpe quadrature (MV-optimal for ρ=I) |
| Two opposing, equal `w` | `0` | perfect cancellation → gated out |
| Opposing (5 long / 4 short) | `9/√41 ≈ 1.41` | stronger strategy dominates more than linear |

**Properties to note (intended, will be quantified in shadow):**
- **Solo-ticker invariance.** Single-strategy tickers size identically to today; only multi-strategy tickers receive diversification deflation. The change is surgical.
- **Quadratic conviction concentration.** The `wᵢ²` numerator weights high-conviction strategies super-linearly: a ticker carried by one Sharpe-5 strategy now outweighs one carried by five Sharpe-1 strategies far more sharply than the linear sum did.

### 2.1 No deflating floor — only an inert non-PSD backstop

Per operator decision, there is **no** deflating floor (the earlier `max_i wᵢ²` idea is dropped): a legitimately-decorrelated sum gets full diversification credit.

A **safety backstop remains** because the similarity matrix is a heuristic Jaccard⊕clipped-Pearson blend (`strategy_similarity.py`: `FOLD_THRESHOLD=0.85` removes only > 0.85 pairs; `RETURN_CORR_ALPHA_CEIL=0.6` caps the return-corr *weight*, not ρ) with **no PSD guarantee**. A pairwise bound does **not** keep the quadratic form `q` non-negative for N ≥ 3. Concrete reachable failure (all sims under the 0.85 fold cut, so all survive):

> 3 co-firing strategies, sims `{ρ₁₂=0.8, ρ₁₃=0.8, ρ₂₃=0.05}`, dirs `(long, short, short)`, equal `w`:
> `q = 3 + 2·[(−0.8) + (−0.8) + (0.05)] = 3 − 3.10 = −0.10` → `√(negative)` → **NaN** → sizer crash / garbage orders.

The backstop (`q ≤ ε` → `den = √(Σwᵢ²)`, the diagonal "assume independent" denominator) is **inert** whenever `q` is well-behaved (it changes nothing), never negative, never explosive. The shadow **counts** how often it fires: zero across the soak ⇒ the decorrelation premise is empirically validated (document as effectively-dead code); non-zero ⇒ a production NaN was caught pre-flip. `ε` is a tiny absolute (e.g. `1e-9`), NOT a deflating floor.

## 3. Pure function (`orthogonalization.py`)

New pure, I/O-free function:

```python
def corr_adjusted_net_sharpe(
        contribs_by_ticker: dict[str, list[tuple[str, int]]],  # {tkr: [(sid, dir_int), ...]} post-fold
        sim: dict[str, dict[str, float]],                      # per-regime similarity matrix
        weight_by_strat: dict[str, float],                     # cadence-normalized daily_weight (signed-magnitude basis)
        eps: float = 1e-9,
) -> tuple[dict[str, float], int]:
    """Returns ({ticker: signed S_adj}, n_backstop_fires).
    Sharpe-weighted combination Sharpe; inverse-free; non-PSD backstop -> diagonal denominator."""
```

- ρ lookup mirrors `_mean_pairwise`: `sim.get(a,{}).get(b, sim.get(b,{}).get(a, SPARSE_DEFAULT=0.05))`; diagonal 1.0.
- Returns the backstop-fire count so the caller can log/aggregate it.
- Docstring explicitly labels the result **approximate (similarity-proxy)**, not a true return-correlation combined Sharpe.

`deflated_net_sharpe` / `block_conviction` / `k_eff` stay intact (rollback path).

## 4. Wiring into `_sharpe_cadence_path`

New gate flag **`OPENCLAW_STRATEGY_CORR_CUMSHARPE`** (default OFF ⇒ byte-identical to today).

**When ON:**
- Build `S_adj` once from the cadence-normalized basis using **raw** `weight_by_strat` (daily_weight) → this is the **gate** quantity `gate_net_sharpe = S_adj`. It **supersedes** `deflated_net_sharpe`: if `CORR_WEIGHT` is also set, the new path wins and we `logger.info` that the deflated path is bypassed.
- The **sizing weight** `ticker_w[tkr]` is rebuilt as the *same* `S_adj`, but computed from `eff_weight_by_strat` (i.e. with `size_scalarᵢ` folded into `wᵢ` when `OPENCLAW_STRATEGY_SIZE_SCALAR=1`). **`size_scalar` stays gate-exempt** (matches today's "gate stays raw" intent). When all scalars = 1, gate and sizing are the *identical* number — the operator's "one number" requirement holds in the base case; `size_scalar` is the documented allocation-only perturbation on top.
- Gate (unchanged mechanism, new quantity): drop `tkr` where `abs(gate_net_sharpe[tkr]) < min_corr_cum_sharpe` (§5). This remains the **ticker-selection authority**.
- `target_usd ∝ ticker_w` (= `S_adj` with scalars), normalized so `Σ|target_usd| = λ·NAV`, exactly as today (no other sizing-path change).
- The existing **per-ticker cap** (`PER_TICKER_CAP_SHARPE_FRAC·|gate_net_sharpe|·λ·NAV`, EOD/intraday lanes) automatically uses the new `gate_net_sharpe = S_adj`. **Not pre-tuned** — cap-binding frequency under the new scale is measured in shadow; retune `PER_TICKER_CAP_SHARPE_FRAC` only if the data says so.

**When OFF:** every code path is byte-identical to current production.

## 5. Per-regime floor `min_corr_cum_sharpe` (dashboard-controlled)

The new gate needs its **own** per-regime floor — the legacy `min_cumulative_sharpe` `[1.0, 10.0]` bound is calibrated to the inflated naive-sum scale and would clamp the new (smaller, cadence-normalized + diversification-deflated) floor up and **freeze the book**.

**Migration 140** — `ALTER TABLE regime_sizer_params ADD COLUMN IF NOT EXISTS min_corr_cum_sharpe REAL NOT NULL DEFAULT 1.0` with `CHECK (min_corr_cum_sharpe >= 0.0 AND min_corr_cum_sharpe <= 10.0)`. (Wider, lower-bounded range because the value's scale is set empirically by the shadow; `0.0` lets the operator effectively open the gate in a regime, e.g. CRISIS. Additive column, master-DB invariant honored — no DELETE.)

**Resolver** — new `_resolve_min_corr_cum_sharpe(params)` mirroring `_resolve_min_cumulative_sharpe`: per-regime `params['min_corr_cum_sharpe']` bound `[0.0, 10.0]`, fallback `pipeline_config['min_corr_cum_sharpe']`, then default. The sizer selects which floor to apply by flag: `CORR_CUMSHARPE=1` → `min_corr_cum_sharpe`; else legacy `min_cumulative_sharpe`.

**Dashboard (`src/channels/api/server.js`)** — mirror the existing `min_cumulative_sharpe` contract:
- **GET `/api/config/regime-sizing`**: add `min_corr_cum_sharpe` to the `regime_sizer_params` SELECT + per-regime response object; add a top-level `active_conviction_gate: 'corr_cumsharpe' | 'legacy'` derived from `process.env.OPENCLAW_STRATEGY_CORR_CUMSHARPE` (verify the node server loads the same `.env`; if not, fall back to a `pipeline_config` mirror key).
- **PUT `/api/config/regime-sizing/:regime`**: accept `min_corr_cum_sharpe`, validate finite ∈ `[0.0, 10.0]`, include in the `UPDATE … RETURNING`.
- **UI** (the existing "🎯 Conviction Gates" card, server.js ~4098 / ~7916): render a **second per-regime slider** for the corr-adjusted floor (range `0–10`, step `0.05`) alongside the legacy one, and **badge the active gate** ("LIVE GATE") from `active_conviction_gate`. Debounced PUT → `_corrSharpeGatePut` mirroring `_sharpeGatePut`. The legacy slider stays visible (rollback transparency). The operator thus still controls the live gate's per-regime floor from the dashboard.

## 6. Rollout — shadow → measure → flip

**Deploy 1 — shadow.** Flag **`OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW`** computes `S_adj` alongside the live gate, **routes nothing**, and logs per cycle:
1. `|S_adj|` distribution across tickers (min / p25 / median / p75 / max).
2. **would-drop set vs today** (tickers the new gate drops that the live gate keeps, and vice-versa) at the *current* `min_corr_cum_sharpe`.
3. **cross-sectional Δ$ allocation** — `target_usd` under `S_adj` vs live `ticker_w` (surfaces the √N down-weighting of crowded multi-strategy tickers).
4. **per-ticker-cap binding frequency** under the new scale.
5. **non-PSD backstop fire count** (from §2.1).
6. a **recommended floor** per regime — e.g. the `min_corr_cum_sharpe` that preserves ≈ today's surviving-ticker count (so day-1 selection breadth is comparable, then the operator tunes).

Shadow reuses the established `_ortho_enabled` gating idiom and logs under a stable prefix (e.g. `corr_cumsharpe.shadow:`).

**Measure** for a short operator-defined window (N cycles).

**Deploy 2 — flip.** Operator sets per-regime `min_corr_cum_sharpe` from the shadow (dashboard sliders), turns `OPENCLAW_STRATEGY_CORR_CUMSHARPE=1`, retires the shadow flag. Monitor would-drop/Δ$/cap-binding live for ≥1 cycle.

## 7. Testing (TDD, in worktree; never touch live `/root/openclaw`)

**Pure-function unit tests** (`tests/test_corr_adjusted_net_sharpe.py`):
- single strategy → `S_adj = w·d` (solo invariance);
- N independent same-dir → `√N·w`;
- ρ=1 same-dir → `w` (no double-count);
- unequal uncorrelated → `√(Σwᵢ²)`;
- two opposing equal → 0; opposing unequal → signed dominance;
- missing pair → 0.05 sparse default; diagonal=1;
- **non-PSD config** (the `{0.8,0.8,0.05}` L/S/S example) → `q ≤ ε` → diagonal backstop, fire-count = 1, no NaN;
- sign preservation (net-short ticker → negative `S_adj`).

**Sizer integration tests** (extend `tests/` for `_sharpe_cadence_path`):
- flag OFF → byte-identical gate + `ticker_w` to current behavior;
- flag ON → gate and `ticker_w` both rebuilt from the same `S_adj`; `size_scalar` folds into sizing only, gate stays raw;
- gate drops tickers below `min_corr_cum_sharpe`; solo-ticker `target_usd` unchanged vs OFF.

**Dashboard**: GET returns new field + `active_conviction_gate`; PUT validation rejects out-of-range; PUT persists.

**Migration**: 140 applies idempotently; CHECK rejects out-of-range; default 1.0 present on all four regime rows.

## 8. Rollback

- `OPENCLAW_STRATEGY_CORR_CUMSHARPE=0` (or unset) → instant revert to the legacy gate + sizing (code byte-identical when OFF). Legacy `deflated_net_sharpe` + `min_cumulative_sharpe` untouched.
- Migration 140 is additive; no rollback needed (column simply unused when flag OFF).
- Dashboard legacy slider remains authoritative while flag OFF.

## 9. Open items / risks (carried into the plan)

- **Floor scale unknown until shadow.** Do **not** flip before setting `min_corr_cum_sharpe` per regime from shadow data — flipping on the DEFAULT 1.0 may over/under-gate. This is the primary live-flip gate.
- **`active_conviction_gate` source.** Confirm the node dashboard process sees `OPENCLAW_STRATEGY_CORR_CUMSHARPE`; if it doesn't load `.env`, add a `pipeline_config` mirror the server can read.
- **`PER_TICKER_CAP_SHARPE_FRAC` coupling** — measured in shadow, retuned only if needed.
- **Quadratic conviction concentration** (§2) is a real behavioral shift toward dominant-strategy tickers; the operator accepts it pending shadow Δ$ evidence.
- VPS 2-core/8GB: the new computation is O(Σ per-ticker pairs²) on already-loaded in-memory data — negligible; no new parquet/DB reads on the hot path.

## 10. Build-time deltas (surfaced by the ON-path integration smoke)

Two corrections made during implementation, not in the original §4 (commit `9869735`):

1. **Similarity-matrix load trigger.** `_ortho_groups` (the `strategy_similarity` matrix) was loaded only when FOLD/CORR_WEIGHT/ORTHO_SHADOW/BRACKET_STACK was enabled. With *only* `CORR_CUMSHARPE` (or its shadow) on, the matrix never loaded and the corr path silently never activated. Fix: added `_corr_cumsharpe_on or _corr_cumsharpe_shadow` to the load trigger. (Fail-safe preserved: if `load_groups` raises, `_ortho_groups` stays None and the path falls back to the legacy gate+floor.)

2. **Equity gate floor isolated from the option/legacy floor.** `min_cum_sharpe` is overloaded: besides the equity gate it also drives `_consolidate_option_orders` (which gates a **naive raw-Sharpe** `u_net_sharpe` — correctly calibrated to the legacy `[1,10]` floor) and the legacy ortho-shadow. The corr floor must not leak there (options aren't in the equity similarity matrix). Fix: `min_cum_sharpe` stays legacy (option/legacy-shadow path); a new `equity_gate_floor` is the corr floor **only when the corr path is actually taken** (`_ortho_groups` present AND flag on) — so a missing matrix fails safe to the legacy floor *and* legacy quantity together. The equity gate-drop and the shadow's `legacy_floor` arg read `equity_gate_floor`.

Both are covered by `tests/test_corr_cumsharpe_integration.py` (corr path activates; `S_adj` drives gate+cap; corr floor selects tickers).
