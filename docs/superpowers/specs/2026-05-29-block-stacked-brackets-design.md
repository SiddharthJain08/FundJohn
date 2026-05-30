# Block-Stacked Brackets — Design

**Date:** 2026-05-29
**Author:** BotJohn (Opus 4.8)
**Status:** Approved (design) — implementation pending
**Gate:** `OPENCLAW_STRATEGY_BRACKET_STACK` (default-OFF)
**Builds on:** Strategy orthogonalization substrate (`docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md`)

---

## 1. Problem

When several strategies fire on the same ticker on the same cycle, the regime-blended
sizer emits **one** Alpaca bracket per ticker. Today that bracket is chosen by
`regime_blended_sizer._select_bracket`: from the direction-aligned contributing
signals it keeps the **single largest-`daily_weight`** signal's `(entry, stop, t1)`
and discards every other strategy's exit levels.

Two problems follow:

1. **The bracket ignores conviction breadth.** Whether 1 strategy or 6 *uncorrelated*
   strategies agree on a ticker, the take-profit is whatever the single highest-weight
   strategy happened to set. The breadth of independent agreement — which is real
   information about how far the move may run — is thrown away at the exit layer.
2. **The representative is chosen by the wrong key.** The fold representative is the
   **max-effective-sharpe** member of a correlated group (`strategy_similarity.representatives`),
   but the bracket is picked by **max-`daily_weight`**. Within a correlated set the
   exit levels should come from the strategy we already trust most (top-sharpe), not
   the largest-weight one.

The operator's intent: **within a correlated set, use the top-sharpe strategy's
stop/take-profit; across *uncorrelated* strategies, stack the take-profits rather
than picking one** — so a ticker confirmed by more independent strategies is given a
proportionally further target, on the thesis that broader independent agreement
predicts a larger move.

## 2. Scope

* Changes **only the bracket** (`entry`/`stop`/`t1`) attached to `delta` and
  `flip_open` emissions in `_sharpe_cadence_path`. **Position sizing
  (`ticker_w`, `target_usd`), the conviction gate, the confirmer, and all emission
  kinds are untouched** — this is "bracket only," the exit-layer analog of Tier-2's
  "gate only."
* Equity is the focus (matches the orthogonalization work). The path is
  instrument-agnostic in code, but only equity strategies populate the multi-strategy
  co-firing the feature acts on.
* **Out of scope:** `t2` (Alpaca bracket/OCO routes only `t1` today — `t2` is carried
  through for audit but not used at execution); partial/scaled exits; any change to
  the OCO reattach process (`stop_reattach.py`), which consumes whatever `t1`/`stop`
  the order carries.

## 3. Key design decisions (operator-approved)

| Decision | Choice | Rationale |
|---|---|---|
| **Stop treatment** | **Asymmetric** — stack the take-profit across blocks; the stop is the **tightest** per-block representative value (`min` across blocks), never widened. | A take-profit is a *prediction* (more independent agreement → larger expected move → further target). A stop is a *risk control*, not a prediction. Keeping the stop tight holds dollar-risk ~linear while reward scales with conviction → a deliberate **convex** payoff. Avoids the ~`n²` dollar-risk blow-up that stacking *both* legs would cause (size already scales with conviction). |
| **Take-profit growth** | **Capped linear** — `tp_total = min( Σ_b tp_pct_b , TP_CAP_MULT × max_b tp_pct_b )`, `TP_CAP_MULT = 3.0`. | Honest "add on top of each other" for the first few confirmations, with an explicit, *relative* ceiling (3× the largest single-block target) well before the executor's 50% clamp. Bounds the round-trip-without-filling trap that unbounded targets create. Relative cap is robust to a non-5% strategy. |
| **Uncorrelated unit** | The **factor block** (the 0.40-similarity cut), identical to Tier-2's `deflated_net_sharpe`. Ungrouped strategies are their own singleton block. | One coherent definition of "uncorrelated cluster" across the whole orthogonalization stack. |
| **Within-block representative** | The **top-effective-sharpe member that fired** in the block (mirrors `representatives` / fold). | Implements "within a correlated set, use the top-sharpe strategy's SL/TP," and closes the max-weight-vs-max-sharpe inconsistency. |
| **Validation before flip** | **Shadow + counterfactual backtest**, then operator flips. | "Most profitable" is empirical; settle it on data before routing live. |
| **Rollout** | Default-OFF env gate, byte-identical when OFF. | Established orthogonalization pattern. |

## 4. Architecture

```
_sharpe_cadence_path (regime_blended_sizer.py)
  … fold (Tier-1) → ticker_w / ticker_net_sharpe → gate (Tier-2) → emissions …
  per delta / flip_open emission:
      if OPENCLAW_STRATEGY_BRACKET_STACK and ortho substrate present:
          bracket = bracket_stacking.stacked_bracket(
                        brackets=ticker_meta[tkr]['brackets'],   # now carry 'sid'
                        dir_sign=dir_sign,
                        block_map=_ortho_groups['block_map'],
                        eff_sharpe=sharpe_by_strat,
                        matrix=_ortho_groups['matrix'])          # reserved; not needed for min-stop/sum-tp
      else:
          bracket = _select_bracket(ticker_meta[tkr]['brackets'], dir_sign)   # UNCHANGED
```

* **New pure module** `src/execution/bracket_stacking.py` — no I/O, no DB; mirrors
  `orthogonalization.py`. Returns a dict in the **exact shape `_select_bracket`
  returns** (`{entry, stop, t1, t2, weight, direction}` keys), so the order-construction
  block (`regime_blended_sizer.py` ~line 632) needs no change.
* **One additive change to existing collection:** the per-signal bracket dict appended
  in `_sharpe_cadence_path` (~line 414) gains `'sid': sid` so each bracket maps to its
  block. (`_select_bracket` ignores the extra key → OFF path byte-identical.)
* **No new DB tables, no migration.** Reuses `strategy_similarity.load_groups()`
  (`block_map`, `rep_map`, `matrix`) already loaded into `_ortho_groups`, and
  `sharpe_by_strat` already in scope.

## 5. Algorithm (`stacked_bracket`)

Inputs: `brackets` (list of `{sid, direction, weight, entry, stop, t1, t2}`),
`dir_sign ∈ {+1,-1}`, `block_map: sid→block_id`, `eff_sharpe: sid→float`.

1. **Filter & convert.** Keep `b.direction == dir_sign` with finite `entry>0`, `stop`,
   `t1`. For each, fractional distances on its own entry:
   * long:  `stop_pct = (entry−stop)/entry`, `tp_pct = (t1−entry)/entry`
   * short: `stop_pct = (stop−entry)/entry`, `tp_pct = (entry−t1)/entry`
   * Drop any with `stop_pct ≤ 0` or `tp_pct ≤ 0` (inverted/degenerate — same spirit as
     the executor's `_recompute_bracket_from_quote` guards).
   * If none survive → return `{}` (caller falls back / close_only). Fail-open.
2. **Group by block.** `block_map.get(sid)`; ungrouped sid → unique singleton block id
   (negative sequence, identical convention to `deflated_net_sharpe`).
3. **Per-block representative.** Within each block, choose the member with the highest
   `eff_sharpe` (ties broken by `sid` for determinism). Yields per block:
   `(stop_pct_b, tp_pct_b, entry_b, t2_b, sharpe_b)`.
4. **Combine across blocks** (`B` = number of blocks):
   * **Take-profit:** `tp_total = min( Σ_b tp_pct_b , TP_CAP_MULT × max_b tp_pct_b )`.
     (`B == 1` → `tp_total = tp_pct_1`, the rep's target.)
   * **Stop:** `stop_total = min_b stop_pct_b` (tightest).
5. **Anchor & rebuild.** Anchor to the **highest-sharpe block's rep** (`entry*`, `t2*`):
   * long:  `stop = entry*·(1−stop_total)`, `t1 = entry*·(1+tp_total)`
   * short: `stop = entry*·(1+stop_total)`, `t1 = entry*·(1−tp_total)`
   * Return `{entry: entry*, stop, t1, t2: t2*, weight: <max block weight>, direction: dir_sign,
     n_blocks: B, why: '<summary>'}`. The executor's 1% min-gap floor and 50% clamp
     remain the final backstops.

### Edge cases
* **n=1 block** → the top-sharpe rep's bracket verbatim (max-sharpe, *not* max-weight —
  the intended within-set fix; this is the byte-difference vs. current behavior even
  on single-block tickers when the gate is ON).
* **No finite/aligned brackets** → `{}` → caller's `_select_bracket` fallback →
  `close_only` downstream. Never raises.
* **orphan_close / flip_close** never call this (bracket forced empty upstream).

## 6. Validation

### 6.1 Shadow (no routing)
Extend the existing `OPENCLAW_STRATEGY_ORTHO_SHADOW` block in `_sharpe_cadence_path`
to log, per co-firing ticker: current `_select_bracket` pick vs. `stacked_bracket`
result — `stop_pct`, `tp_pct`, `n_blocks`, cap-hit flag. Read-only; no order change.

### 6.2 Counterfactual backtest — `scripts/backtest_bracket_stacking.py`
Isolates the **bracket-policy effect** (same entries, same tickers, different exits):

1. Window-scan `execution_signals` for historical co-firing events
   (`(signal_date, ticker)` with ≥1 finite-bracket signal).
2. For each event, build **both** brackets: current max-weight pick **and**
   `stacked_bracket` (using the period's `load_groups`/`eff_sharpe`; for a first pass,
   current substrate is acceptable and documented as a limitation).
3. Run `unified_backtest.simulate_trade` on the ticker's forward bars for **each**
   bracket → exit price / reason / pnl_pct / holding_days.
4. Aggregate and compare: mean pnl_pct, hit-rate, exit-reason mix
   (target/stop/max-hold), avg holding days, and the subset where `n_blocks ≥ 2`.

**Documented scope/limitation:** holds size fixed (per-unit) → measures *per-trade
exit quality on co-firing names*, not full portfolio interaction. That is exactly the
question that settles the stop/TP policy. Read-only; touches no master data.

## 7. Testing

* **`tests/test_bracket_stacking.py`** (pure): sum-then-cap TP; min stop; `n=1` = rep
  bracket (and proves max-sharpe rep, not max-weight); short side; ungrouped singleton
  blocks; non-finite/inverted rejection; empty → `{}`; determinism of rep tie-break.
* **`tests/test_bracket_stacking_sizer.py`** (integration): gate OFF →
  `_select_bracket` byte-identical; gate ON → stacked bracket on a constructed
  co-firing ticker; orphan/flip still empty bracket.
* **Backtest smoke:** `backtest_bracket_stacking.py` runs on a small window and emits a
  comparison table without error.

## 8. Gating & safety invariants

* `OPENCLAW_STRATEGY_BRACKET_STACK` default-OFF. When OFF, `_sharpe_cadence_path`
  calls `_select_bracket` exactly as today → **byte-identical**.
* `TP_CAP_MULT` overridable via `OPENCLAW_BRACKET_STACK_TP_CAP_MULT` (default 3.0).
* **Risk invariant:** the stop is a `min` of per-block stop fractions, so it can only
  be ≤ any single contributing stop → stacking never widens risk; combined with
  conviction-scaled notional, per-trade dollar-risk stays ~linear in block count.
* No master-data writes; no migration; no new live order *types* — only changed
  price levels on brackets the sizer already emits.

## 9. Files

**New:** `src/execution/bracket_stacking.py`, `scripts/backtest_bracket_stacking.py`,
`tests/test_bracket_stacking.py`, `tests/test_bracket_stacking_sizer.py`.
**Modified:** `src/execution/regime_blended_sizer.py` (carry `sid` in bracket dict;
gated `stacked_bracket` call; extend shadow log).
**Reused unchanged:** `strategy_similarity.load_groups`, `orthogonalization` conventions,
`unified_backtest.simulate_trade`, `alpaca_executor._recompute_bracket_from_quote`
(min-gap/clamp backstops).
