# Asset-Correlation Cluster Cap — Design Spec

**Date:** 2026-06-23
**Status:** Approved design; pending implementation plan.
**Author:** BotJohn (operator-directed)
**Companion modules (new):** `src/execution/asset_correlation.py`, `src/execution/asset_correlation_filter.py`
**Wiring:** `src/execution/regime_blended_sizer.py` (one insertion point)

---

## 0. Purpose & scope

Bound each **correlated asset cluster's** net directional exposure at `≤ cap_pct` of equity, **releasing** (not redistributing) the trimmed budget. This de-concentrates the live book and frees day-trading buying power (DTBP) for uncorrelated names that currently get dropped at the executor.

**Motivation (measured 2026-06-23):** the live book was 72.6% of equity in ~10 correlated semis/memory names (SNDK alone 15%) at 1.82× gross. A single sector catalyst (overnight gap, Jun 22→23) cost ~$8–10k because the book was one concentrated bet. Separately, the executor's `_compute_dtbp_skips` funds highest-conviction opens first and **hard-cuts everything below** when DTBP is exhausted — so redundant correlated opens consume the budget uncorrelated names need.

**In scope:** measuring asset correlation, clustering held+candidate tickers, capping per-cluster gross by releasing low-conviction redundancy, wiring into the sizer behind a default-OFF gate, a shadow/measurement path, tests.

**Out of scope:** signal generation, the cumulative-Sharpe qualification, bracket levels (separate `bracket_stacking` work), the executor's DTBP logic itself (this feature reduces the *demand* on DTBP; it does not change the executor). No master-data writes. No new DB migration required (correlation computed on-demand; audit logged + optionally persisted to an existing audit channel — see §7).

---

## 1. Design decisions (operator-approved 2026-06-23)

| # | Decision | Choice |
|---|---|---|
| D1 | Correlation source | **Price returns** — daily close-to-close return correlation from `prices.parquet` (≈63d), NOT the dormant PnL/overlap `correlation_matrix.py`. Directly captures "these names move together." |
| D2 | Trimmed-budget handling | **Release / de-gross** — keep top-conviction winners at THEIR OWN sizing; trimmed names' notional is released (not redistributed). Gross drops when the book is redundant → DTBP frees + leverage falls on a correlated book. |
| D3 | Keep rule | **Per-cluster gross cap** — keep names in descending conviction until cumulative cluster gross hits `cap_pct×NAV`; release the rest. Bounds THEME exposure regardless of name count. |
| D4 | Scope | **Full target book** — the cap applies to the intended book each cycle, so an already-over-concentrated cluster generates REDUCE/SELL deltas via the existing broker netting. Gated + shadow-first + measure before live. |

---

## 2. Architecture

Two small, pure, independently-testable modules + one wiring point. Both modules are pure (no I/O except `asset_correlation` reading the parquet via the existing sliced loader); deterministic given inputs.

```
candidates
  → cum-sharpe gate                       (existing, regime_blended_sizer.py ~L1051-1064)
  → target_usd scaled to λ×NAV            (existing, ~L1093-1117)
  → [NEW] asset-correlation cluster cap   (insert here, BEFORE broker netting)
        asset_correlation.price_return_corr(tickers) → corr matrix
        asset_correlation_filter.cap_correlated_clusters(target_usd, dirs, conviction, corr, cap_pct, corr_thr)
            → capped target_usd (gross ≤ before; released budget NOT renormalized) + audit
  → broker netting / _classify_position_deltas  (existing, ~L1197 — now sees lower targets → emits the sell-downs)
  → TradeJohn confirmer → emit → executor (now has DTBP headroom)
```

**Why insert AFTER the λ×NAV scaling (dollar terms), not before:** the sizer scales weights so `Σ|target_usd| = λ×NAV`. If we capped *weights* pre-scale, the scale step would re-inflate the survivors back to λ×NAV (gross unchanged → no DTBP freed — the exact footgun). Capping in **post-scale dollar terms and NOT renormalizing** is what makes the release real (gross drops below λ×NAV by the released amount).

---

## 3. Component: `asset_correlation.py`

```
def price_return_corr(tickers, window=63, as_of=None) -> dict[str, dict[str, float]]:
    """Pairwise Pearson correlation of daily close-to-close returns over the last
    `window` trading days up to `as_of` (default: latest available bar).
    - Reads prices.parquet via the existing sliced loader (predicate pushdown by
      ticker+date; NEVER loads the full panel).
    - returns[a][b] = corr; diagonal 1.0; symmetric.
    - Pairs with < MIN_OBS (e.g. 20) overlapping returns -> 0.0 (treated as
      uncorrelated; we never trim on thin evidence). Off-diagonal clipped to [-1, 1].
    - Tickers with no/short history are returned with corr 0.0 to all others (never clustered)."""
```

- **Window:** ≈63 trading days (~3 months). Calibratable. (Pearson over a fixed window for v1; EWMA/DCC deferred.)
- **Returns basis:** simple daily close-to-close pct change. (Overnight-gap-inclusive, which is the risk we care about.)
- **Caching:** compute once per sizer cycle for the union of (candidate ∪ held) tickers; memoize within the process.
- **Fail-open:** any load/compute error → return an empty/identity matrix so the caller applies no capping (never block a trading cycle on correlation data).

---

## 4. Component: `asset_correlation_filter.py`

```
def cap_correlated_clusters(
    target_usd: dict[str, float],        # ticker -> signed target notional (post λ×NAV scaling)
    directions: dict[str, int],          # ticker -> +1 / -1
    conviction: dict[str, float],        # ticker -> ticker_net_sharpe (signed); rank by |conviction|
    corr: dict[str, dict[str, float]],   # from asset_correlation.price_return_corr
    nav: float,
    cap_pct: float = 0.22,               # per-cluster gross cap, fraction of equity (calibratable)
    corr_thr: float = 0.70,              # cluster membership threshold (calibratable)
    single_name_cap_pct: float | None = None,  # optional per-singleton cap; None = no singleton cap (calibration §6)
) -> tuple[dict[str, float], dict]:
    """Returns (capped_target_usd, audit). Pure."""
```

**Algorithm:**
1. **Cluster within direction.** Split tickers by `directions` sign. Within each side, build distance `1 - corr[a][b]` and agglomerative average-linkage cluster, cut at distance `1 - corr_thr` (reuse the proven pattern in `strategy_similarity.cluster_two_cuts`; generalize to ticker keys or copy the ~10-line core). Only positively-correlated (≥ `corr_thr`) same-direction names land in a multi-member cluster. Negatively/uncorrelated names and opposite-direction correlated names (hedges) stay separate → **never penalized**.
2. **Cap each cluster.** Sort members by `|conviction|` descending (tie-break: larger `|target_usd|`, then ticker for determinism). Walk in order accumulating `|target_usd|`:
   - Keep a name **at its full target** while cumulative ≤ `cap_pct×NAV`.
   - The **boundary name** that would cross the cap is **trimmed to exactly fill the cap** (partial fill; remainder released).
   - All **lower-conviction names** beyond the cap are **released → target_usd set to 0**.
3. **Release, never redistribute.** Released/trimmed dollars are NOT moved to survivors or other clusters. Total `Σ|capped_target_usd| ≤ Σ|target_usd|` (gross monotonically non-increasing).
4. **Singletons:** clusters of size 1 are left unchanged unless `single_name_cap_pct` is set, in which case the same trim-to-cap applies per name (a per-name max). Default None for v1 (decide in calibration §6).
5. **Audit:** return `{clusters: [{members, direction, kept:[(tkr,usd)], trimmed:[(tkr,from,to)], released:[(tkr,usd)], gross_before, gross_after}], total_gross_before, total_gross_after, dtbp_freed_est}`.

**Invariants (runtime-guarded / tested):**
- INV-1 Gross never increases: `Σ|out| ≤ Σ|in|` for every input.
- INV-2 No redistribution: a survivor's `|target_usd|` is never larger than its input.
- INV-3 Direction integrity: signs unchanged; opposite-direction correlated pairs never co-clustered.
- INV-4 Determinism: identical inputs → identical output (sorted tie-breaks).
- INV-5 Fail-open: empty/degenerate `corr` → output == input (no capping).

---

## 5. Wiring (`regime_blended_sizer.py`) + gating

**Insertion:** immediately after `target_usd` is finalized (post λ×NAV scale + dust floor, ~L1117) and before `_load_broker_positions_usd` / `_classify_position_deltas` (~L1197).

**Inputs available at that point:** `target_usd`, `ticker_meta` (directions, contributing strategies), `ticker_net_sharpe` (conviction), `nav`. Build `directions` and `conviction` from these; tickers = `set(target_usd)` (the intended book — already nets candidates against the desired holdings).

**Gates (default-OFF; mirrors the bracket-stack `ORTHO_SHADOW` convention):**
- `OPENCLAW_ASSET_CORR_CAP=1` → **apply** the capped `target_usd`.
- `OPENCLAW_ASSET_CORR_CAP_SHADOW=1` → **compute + log** the audit (would-be caps, gross/DTBP freed, sell-downs) but DO NOT change `target_usd`.
- Both unset → **byte-identical** to today (the audit is not even computed).
- `cap_pct` / `corr_thr` / `window` overridable via env for calibration.

When applied, the existing broker netting consumes the lowered `target_usd` and naturally emits the reduce/sell deltas for existing over-concentration (D4) and simply omits the released names from opens — freeing DTBP at the executor with no executor change.

---

## 6. Parameters & measure-first calibration (before any live flip)

Defaults are placeholders; final values come from a shadow measurement:
- `window` ≈ 63d, `corr_thr` ≈ 0.70, `cap_pct` ≈ 0.20–0.25, `single_name_cap_pct` = TBD (likely None or ≈ `cap_pct`).

**Calibration deliverable (one-shot report, run in shadow):**
1. On the **current book**: the clusters it finds, what it would trim/sell, gross before→after, estimated DTBP freed, and which previously-DTBP-dropped uncorrelated names would now fill.
2. **Jun 22→23 counterfactual:** what the loss would have been with the cap applied (book de-concentrated pre-gap).
3. **Threshold/cap sweep:** `corr_thr ∈ {0.6,0.7,0.8}` × `cap_pct ∈ {15,20,25%}` → gross reduction, # names trimmed, # uncorrelated names freed. Operator picks the cell.
4. **Sell-down realism:** how much of the trim is selling *currently-down* names (loss realization / selling weakness) — the key risk in D4.

Compute discipline: per-ticker parquet slices, serial, `nice -n 19`, RSS < 1 GB (the 2-core box runs a backtest sweep).

---

## 7. Rollout (phased, mirrors bracket-stack)

1. Build `asset_correlation.py` + `asset_correlation_filter.py` + tests (TDD).
2. Wire into the sizer behind both gates default-OFF (byte-identical).
3. **Shadow** (`OPENCLAW_ASSET_CORR_CAP_SHADOW=1`): log the audit each cycle + run the §6 one-shot report. Measure for a few cycles.
4. Operator picks `cap_pct`/`corr_thr`/`window` from the measurement.
5. Flip `OPENCLAW_ASSET_CORR_CAP=1` (operator-gated; the sizer is a fresh subprocess so it takes effect next cycle — no johnbot restart). Audit persisted to logs (and optionally an existing audit table; no new migration in v1).

---

## 8. Testing

- **Unit (`asset_correlation_filter`):** cluster cap math (keep-until-cap + boundary partial + release); INV-1..5; direction netting (long+short correlated pair untouched); singleton behavior (both settings); empty/degenerate corr = no-op; determinism/tie-breaks.
- **Unit (`asset_correlation`):** Pearson correctness on a known series; MIN_OBS thin-pair → 0.0; missing ticker → 0.0; fail-open on bad data; never loads full panel (sliced read asserted).
- **Wiring regression:** gates OFF → `target_usd` byte-identical (the load-bearing safety test); shadow ON → `target_usd` unchanged, audit emitted; apply ON → `target_usd` capped + gross reduced.
- **Counterfactual harness:** the §6 report is reproducible.

---

## 9. Risks & caveats

- **Selling weakness (D4):** with full-book scope, hitting the cap can mean selling currently-down correlated semis (realizing losses / selling the bottom). Mitigations: shadow + measure first; gated; the **rate-limited variant** (max cluster reduction %/day) stays available if the one-cycle sell-down is too sharp.
- **Backward-looking correlation:** a 63d Pearson lags regime shifts; correlations spike in crashes (exactly when concentration hurts). The cap is a standing risk limit, not a forecast — acceptable, but note it under-reacts to sudden co-movement.
- **Conviction = `ticker_net_sharpe`** inherits any bias in effective_sharpe; within a cluster it only decides *ordering*, so modest mis-rank just changes which correlated name is kept.
- **Interaction with leverage:** releasing budget means running below λ×NAV on redundant books — intended (lines up with the "cut leverage on a correlated book" finding), but it does mean target gross is no longer constant; downstream consumers reading "gross == λ×NAV" must not assume that when the gate is on.

---

## 10. Open items for the implementation plan

- Generalize vs copy `cluster_two_cuts` for ticker keys (avoid importing strategy-keyed assumptions).
- Exact audit sink (stdout log vs an existing audit table) — no new migration in v1.
- `single_name_cap_pct` default (calibration).
- Confirm the precise insertion line after the dust-floor block in the current `regime_blended_sizer.py`.
