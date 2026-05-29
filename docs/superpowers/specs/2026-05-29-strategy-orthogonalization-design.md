# Strategy Orthogonalization Engine — Design

- **Date:** 2026-05-29
- **Status:** Design approved (brainstorm); plan pending
- **Scope:** Equity strategies (the abundant class). Both tiers in one spec.
- **Owner:** BotJohn
- **Supersedes/relates:** builds on the Phase 2G/2H correlation infra (`src/execution/correlation_matrix.py`) and the regime-blended sizer (`src/execution/regime_blended_sizer.py`).

---

## 1. Problem

Strategies are ingested liberally (Sharpe > 0.5 + other gates) and combined to produce directional conviction. The combination is purely **additive and signed**, in `regime_blended_sizer._sharpe_cadence_path` (`src/execution/regime_blended_sizer.py:376–385`):

```python
ticker_w[tkr]          += weight_by_strat[sid] * d      # → position size
ticker_net_sharpe[tkr] += sharpe_by_strat[sid] * d      # → conviction gate
```

The conviction gate then drops any ticker with `|ticker_net_sharpe| < min_cumulative_sharpe` (default 3.0; line 409).

**The bug this creates:** five strategies that are really *the same factor* (e.g. all price momentum), each Sharpe ≈ 1, all firing LONG AAPL, sum to `net_sharpe = 5`, clear the 3.0 gate, and stack into ~5× weight — when they constitute **~1 independent bet**. The system cannot distinguish *five independent confirmations* from *one idea counted five times*. Highly-correlated strategies manufacture false confidence.

**The constraint that rules out the naive fix:** forcing strategies to *decorrelate* is equally bad — anti-correlated strategies mean one is always wrong when the other is right, which destroys diversification value rather than creating it. The goal is therefore **not** decorrelation. It is **correctly counting independent evidence**: two ρ = 0.9 strategies should count as ≈ 1.1 bets — not 2, and not 0.

**Why a single gate-discount is insufficient (operator insight):** scaling `ticker_net_sharpe` after the sum cannot un-pollute it — the sum is already dominated by the duplicated strategy, starving genuinely-unique signal. De-duplication must happen **upstream of the sum**, not as a discount on it. Hence two tiers.

---

## 2. Goals / Non-goals

**Goals**
- Fold near-identical strategies so a duplicated bet is counted once (Tier 1).
- Discount partially-correlated strategy *sets* so within-factor agreement stops inflating conviction (Tier 2), without forcing decorrelation.
- Robust on the current data reality: ~2–3 weeks of live PnL (sizer live 2026-05-12) + ragged backtest history. No full-matrix inversion, no mean-variance optimizer.
- Reversible, per-regime, gated default-OFF, byte-identical to today when OFF.
- Emit an audit trail so the operator can *manually* retire chronic duplicates.

**Non-goals (explicit follow-ons, not this spec)**
- **Options corroboration** (put/call, IV skew) and **sector/ETF flow** confirmation — data-blocked today (options history stale post-2026-04-22 Polygon cutover; live chains ~36 days; sector-level put/call/IV aggregations do not exist). Separate spec.
- **Information ratio** — *not currently computed anywhere* (system computes Sharpe/Sortino/Calmar/MaxDD/hit-rate only). This spec lands the per-strategy return series IR would need, but does not add IR. Separate follow-on.
- **Continuous eigenvalue-based deflation** (no discrete groups) — the principled end-state, but fragile on sparse/ragged data. Revisit when live history is rich.
- **Sizing de-concentration** — Tier-2 deflates the conviction **gate only**; the deliberate "honest expression of conviction" sizing behavior (`regime_blended_sizer.py:428–432`) is preserved. (A future sizing sub-gate is noted in §10.)
- Crypto/futures/options/etp classes — equity only for now.

**Known near-term limitation (set expectations):** the lead signal is *holdings co-firing overlap*, which catches **same-names** redundancy (literal duplicates trading the same tickers). It is **blind to same-factor / different-names** redundancy — e.g. a large-cap momentum strategy and a mid-cap momentum strategy that are economically the same bet on different baskets. That harder case is plausibly the dominant form of the motivating "5 momentum strategies" problem, and it is caught **only by return-correlation**, which is exactly the component that is data-starved early (and correctly shrunk toward 0 by the adaptive α). So in the near term this engine mainly folds literal co-firing duplicates; the same-factor-different-holdings case improves as live history accrues. Overlap-led is still the right *robust* choice now — but the operator should not expect day-one coverage of the harder case.

---

## 3. Architecture

Two halves with a clean seam: an **offline** weekly grouping engine and a **live** per-cycle consumer.

```
OFFLINE (weekly, after strategy_weights.rebuild)        LIVE (every cycle, inside _sharpe_cadence_path)
──────────────────────────────────────────────         ─────────────────────────────────────────────────
signal_pnl daily marks + backtest trades                load fold-groups + factor-blocks for current regime
  └─ per-strategy daily return series ─┐                  │
execution_signals co-firing sets ──────┤                 ├─ TIER-1 fold: collapse same-group/same-dir/same-
                                        ▼                 │   ticker contributions to representative
  strategy×strategy similarity (per regime)               │   (BEFORE the ticker_w / ticker_net_sharpe sums)
   = overlap-Jaccard ⊕ return-corr(shrinkage)             ▼
        │ hierarchical clustering                          ticker_w  (post-fold, NOT k_eff-deflated → sizing)
        ├─ tight cut 0.85 → strategy_fold_groups ──────┐  ticker_net_sharpe_eff (post-fold + TIER-2 k_eff → gate)
        └─ loose cut 0.40 → strategy_factor_blocks ────┘  │
  └─ chronic-fold audit → #strategy-memos                 ▼ existing gate(409) + sizing(433), unchanged shape
```

All new persistence is **operational** (current/historical rows like `strategy_weights_by_regime`), never master parquets — the append-only invariant is untouched.

---

## 4. Offline engine (weekly)

Computed in a new function invoked from `src/agent/curators/weekly_live_sharpe.js`, immediately **after** `strategy_weights.rebuild` (Sun 06:00 ET `weekly_cron`; same cadence, decoupled concern). Also runnable as a CLI for backfill/manual runs.

### 4.1 Per-strategy daily return series — `strategy_daily_returns` (new table)

The foundational artifact that does not exist today. Reconstructed two ways and unioned:
- **Live:** `signal_pnl` daily marks joined to `execution_signals.strategy_id`. **`unrealized_pnl_pct` is a cumulative-since-entry *level*, not a daily return** — it must be **differenced**: per `signal_id`, the day's contribution = `unrealized_pnl_pct[pnl_date] − unrealized_pnl_pct[prior pnl_date]` (first mark = entry-day Δ from 0; close day = `realized_pnl_pct − last unrealized_pnl_pct`). Correlating the raw levels would manufacture spurious trend-correlation between any two strategies with rising cumulative PnL. The per-signal daily Δ's are then aggregated equal-weight across the strategy's open signals to a per-`(strategy_id, date)` daily return. `signal_pnl` has `UNIQUE(signal_id, pnl_date)` so the mark series is well-defined.
- **Historical:** `strategy_backtest_trades` (`entry_date`, `exit_date`, `pnl_pct`, `entry_regime`) run through the existing `unified_backtest._portfolio_daily_returns(trades)` equal-weight marking (`src/backtest/unified_backtest.py:273`).

Stored per `(strategy_id, date)` with a `source ∈ {live, backtest}` tag and the `regime_state` live on that date (for per-regime slicing). Append-only-friendly; recomputable.

### 4.2 Similarity substrate — `src/execution/strategy_similarity.py` (new module)

The **transpose** of `correlation_matrix.py`: a per-regime **strategy×strategy** similarity matrix, reusing that file's scaffolding (`SPARSE_DEFAULT`, ±0.95 clip, per-regime coverage classification `real`/`fallback_global`/`stress_prior`, and `current_state_probabilities()` for the state-probability blend).

Two component signals, blended:

1. **Holdings co-firing Jaccard — the lead signal (available today).** For each strategy, the set of `(ISO-week, ticker, direction)` tuples it emitted to `execution_signals` over a trailing window (default 90 days, matching the 2G/2H `DEFAULT_WINDOW_DAYS`), sliced by `regime_state`. Similarity(a,b) = Jaccard of these sets. Week-bucketing tolerates differing cadences so two strategies that both went LONG AAPL the same week register as concurrent. This directly measures the redundancy that causes false confidence and needs no return history.

2. **Return correlation — shrinkage component.** Pearson on the §4.1 series over the regime slice, entered with a **data-adaptive blend weight** `α(n)` that rises from ≈0 (pure overlap when little joint history exists) toward a target ceiling as the count of overlapping observations grows. Implements "lead with overlap; add return-corr where data allows" rather than a fixed α. Diagonal 1.0; off-diagonals clipped ±0.95; sparse pairs → `SPARSE_DEFAULT`.

Per-regime matrices are blended by current HMM `state_probabilities` exactly as `blended_correlation_by_state` does, yielding one effective similarity matrix for the live regime mix.

### 4.3 Clustering → two cuts

`scipy.cluster.hierarchy` (confirmed available on the VPS) — agglomerative, average linkage, on distance `1 − similarity`. Cut at two heights:
- **tight cut (default 0.85)** → `strategy_fold_groups`: near-identical strategies.
- **loose cut (default 0.40)** → `strategy_factor_blocks`: same-factor families.

Fold-groups nest inside factor-blocks by construction (tighter cut ⊂ looser cut). Both per-regime. Both thresholds config-driven (env/config-table), tunable without a code change.

**Cold-start safety:** a strategy with insufficient joint history clusters as a singleton — we never fold or block what we cannot measure. New strategies join groups only after accruing signals.

### 4.4 Representative selection

Per fold-group, the **representative = the member with the highest `effective_sharpe`** (from `strategy_weights_by_regime`, the same figure the sizer uses). Persisted on the group row so the live path needs no recomputation.

---

## 5. Live engine — inside `_sharpe_cadence_path`

Two transforms applied to the `active` signal list before / during the existing aggregation. Both fail-safe: if the groups table is empty/unreadable, behave exactly as gates-OFF.

### 5.1 Tier-1 fold — `OPENCLAW_STRATEGY_FOLD`

Applied **before** the `ticker_w` / `ticker_net_sharpe` accumulation (before `regime_blended_sizer.py:384`). For each `(ticker, direction, fold_group)` bucket among the firing contributions, keep only **one** contribution — the representative if it fired, else the highest-`effective_sharpe` member that *did* fire — and drop the rest.

- Affects **both** sums (`ticker_w` and `ticker_net_sharpe`). This is **not** in tension with the "honest conviction" sizing choice: a near-identical duplicate is not honest conviction, it is the same bet counted twice.
- **Opposite-direction** members of the same fold-group on the same ticker are left intact (they are not identical *there*); the existing signed sum handles them.
- The representative's bracket flows through the existing direction-leader bracket pick (`regime_blended_sizer.py:388–399`) unchanged.

### 5.2 Tier-2 k_eff — `OPENCLAW_STRATEGY_CORR_WEIGHT` (gate only)

After folding, compute a **deflated conviction** `ticker_net_sharpe_eff` and feed it to the existing `min_cumulative_sharpe` gate (line 409) in place of the raw `ticker_net_sharpe`. **Sizing (`ticker_w`) is untouched** (operator decision: preserve honest-conviction concentration for partially-correlated, non-duplicate blocks).

Per ticker, partition the post-fold same-direction survivors into factor-blocks. For each block *b* firing direction *d*:

```
members  = post-fold survivors in block b firing direction d on this ticker
k        = len(members)
if k == 1:                                          # single member — no deflation
    net_eff += effective_sharpe[members[0]] * d
    continue
rho_bar  = mean pairwise similarity among members   (from §4.2 matrix; clamp [0,1])
k_eff    = k / (1 + (k - 1) * rho_bar)              # effective independent bets, ∈ [1, k]
s        = [effective_sharpe[m] for m in members]   # all ≥ 0; direction d carried separately
block_conviction = max(s) + (sum(s) - max(s)) * (k_eff - 1) / (k - 1)
net_eff += block_conviction * d                     # within-block deflation
```

`ticker_net_sharpe_eff[tkr] = Σ_blocks net_eff` (signed; **cross-block agreement keeps full credit** — different factors confirming each other is real independent evidence).

**Floor property (this form is chosen to satisfy it):** a block's deflated conviction is always **≥ its strongest single member**. A redundant *confirming* strategy can lower the block's *marginal* credit but must never drag conviction below what the best member clears on its own. The simpler multiplicative form `Σsharpe · k_eff/k` floors at the *mean* (a strong+weak correlated pair can fall below the strong member alone) — **rejected**.

- Endpoints: ρ̄→1 ⇒ k_eff→1 ⇒ `block_conviction → max(s)` (one bet, the best member — consistent with Tier-1's max representative). ρ̄→0 ⇒ k_eff→k ⇒ `block_conviction → Σs` (full independent credit; the gate is then identical to today — the byte-identical property).
- Worked (heterogeneous): strong Sharpe 3.5 + correlated weak Sharpe 1, ρ̄=0.5, k=2 → k_eff=1.33 → `3.5 + (4.5−3.5)·0.33 = 3.83` (> 3.5; clears the 3.0 gate). Homogeneous: k=5, all Sharpe 1, ρ̄=0.9 → k_eff≈1.09 → block_conviction≈1.09 (≈ 1 effective bet).

`ticker_w` (sizing) remains the post-fold un-deflated signed sum and continues to drive `target_usd` via the unchanged normalization at line 433.

### 5.3 Shadow mode — `OPENCLAW_STRATEGY_ORTHO_SHADOW`

When set, compute both tiers and **log the delta** vs. the live (un-orthogonalized) gate decisions — which tickers Tier-1/Tier-2 would gate out, per-block k_eff — but **return the live result unchanged** (routes nothing). Mirrors the `pyportfolioopt_shadow` soak pattern. Used to observe behavior for a soak period before flipping the real gates.

Shadow output must **also report the pairwise strategy-similarity histogram** (per regime), not just gate deltas — so the operator can see whether *any* pairs actually reach the 0.85 fold / 0.40 block cuts. This is the early-calibration signal (see §10 threshold calibration and the limitation in §2): if no pairs reach the cuts, the engine is correctly inert and the thresholds (or the data window) need revisiting before it does anything.

---

## 6. Schema (new operational tables)

- `strategy_daily_returns(strategy_id, ret_date, daily_return_pct, source, regime_state, ...)` — §4.1; `UNIQUE(strategy_id, ret_date, source)`.
- `strategy_fold_groups(regime_state, group_id, strategy_id, is_representative, effective_sharpe, computed_at, is_current)` — §4.3/4.4.
- `strategy_factor_blocks(regime_state, block_id, strategy_id, computed_at, is_current)` — §4.3.
- `strategy_fold_audit(run_at, regime_state, group_id, strategy_ids[], weeks_persisted, member_sharpes)` — §7 chronic-fold history; append-only.

Migration appends only; no master-data touch; `is_current` flips old rows false (like `strategy_weights_by_regime`).

---

## 7. Human-in-loop cleanup

A weekly `#strategy-memos` post lists strategy pairs/groups that have shared a fold-group **N weeks running** (default N=3), with each member's `effective_sharpe`, so the operator can *manually* decide to retire one. Structural retirement stays operator-only — never automated. Backed by `strategy_fold_audit`.

---

## 8. Rollout & safety

1. **Shadow first** (`OPENCLAW_STRATEGY_ORTHO_SHADOW=1`): compute + log deltas, route nothing, soak.
2. Flip `OPENCLAW_STRATEGY_FOLD=1`, then later `OPENCLAW_STRATEGY_CORR_WEIGHT=1`, each after its own soak.
3. **Byte-identical invariant:** all gates OFF ⇒ the sizer path produces identical orders to today (load-bearing regression test, per prior expansions).
4. Per-regime, reversible (regroup next week), no master-data writes, fail-safe to OFF on any missing/empty groups table.

---

## 9. Testing (TDD)

- Gates OFF ⇒ byte-identical sizing (golden test against current output).
- Tier-1: N same-group/same-dir/same-ticker contributions collapse to 1 representative in **both** sums; representative-didn't-fire falls back to highest-Sharpe firing member; opposite-direction same-group left intact.
- Tier-2: k_eff math (k=2/ρ̄=0.9 → k_eff≈1.05; k=5/ρ̄=0.9 → ≈1.09; ρ̄=0 → k_eff=k); **floor property** — a correlated confirmer never drops `block_conviction` below the strongest single member (heterogeneous 3.5+1 @ρ̄=0.5 → 3.83 ≥ 3.5); ρ̄=0 ⇒ gate byte-identical to today; k=1 guard (no deflation); cross-block full credit; sizing (`ticker_w`) unchanged by Tier-2.
- §4.1 return series: cumulative `unrealized_pnl_pct` is **differenced** to daily Δ before correlation (regression against the level-correlation bug); close-day uses `realized_pnl_pct − last unrealized`.
- Cold-start singletons (insufficient history) never fold/block.
- CRISIS uses stress-prior path; state-probability blend correct.
- Shadow mode routes nothing, logs the delta.
- Similarity substrate: Jaccard over week-bucketed co-firing; data-adaptive α(n) → 0 with no joint history, → ceiling with ample.

---

## 10. Open items / future

- **Tier-2 sizing sub-gate** (`OPENCLAW_STRATEGY_CORR_WEIGHT_SIZING`): deferred. If, after soak, gate-only proves insufficient against factor concentration, add k_eff deflation to `ticker_w` behind its own gate.
- **Options/sector corroboration** — separate spec once chain history accrues + sector-level aggregations are built.
- **Information ratio** — separate follow-on; the `strategy_daily_returns` table (§4.1) is the prerequisite this spec lands.
- **Continuous eigenvalue deflation** — someday-when-data-is-rich replacement for discrete cuts.
- **Threshold calibration** — 0.85 / 0.40 are seed defaults; tune from shadow-mode observation.

---

## 11. Grounding references (verified 2026-05-29)

- Aggregation + gate + sizing: `src/execution/regime_blended_sizer.py:309, 376–385, 409, 428–433`.
- Correlation scaffolding to mirror: `src/execution/correlation_matrix.py` (`SPARSE_DEFAULT`, `blended_correlation_by_state`, `current_state_probabilities`, coverage classification).
- Per-regime weights consumed: `strategy_weights.load_current` → `strategy_weights_by_regime` (`daily_weight`, `effective_sharpe`, `cadence_days`, `is_current`).
- Weekly cadence host: `strategy_weights.rebuild(trigger='weekly_cron')` via `src/agent/curators/weekly_live_sharpe.js` (Sun 06:00 ET).
- Return-series sources: `signal_pnl` (`pnl_date`, `unrealized_pnl_pct`, `closed_at`, `realized_pnl_pct`, `UNIQUE(signal_id, pnl_date)`; mig 012); `strategy_backtest_trades` (`entry_date`, `exit_date`, `pnl_pct`, `entry_regime`; mig 093); `unified_backtest._portfolio_daily_returns` (`unified_backtest.py:273`).
- Shadow pattern: `OPENCLAW_PYPORTFOLIOOPT_SHADOW` / `src/execution/pyportfolioopt_shadow.py`.
- Clustering dep: `scipy.cluster.hierarchy` (present on VPS).
- Append-only invariant: CLAUDE.md "NEVER DELETE FROM THE MASTER DATABASE" — new tables are operational, not master.
