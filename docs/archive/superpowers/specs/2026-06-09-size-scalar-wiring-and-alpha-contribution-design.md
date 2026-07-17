# Design — `size_scalar` wiring + per-strategy alpha-contribution display

**Date:** 2026-06-09
**Branch:** `feat/trade-output-accuracy` (extends the trade-output work; migration 130 `cycle_contributing_strategies` lives here)
**Author:** BotJohn (operator: Sid)

---

## 1. Goal (operator's Arc-6 request)

On both **open and closed** positions, show a per-strategy **alpha contribution** that is
*the actual allocation term that drives the position's sizing* — `daily_weight × direction`
with the approved size adjustments folded in — restricted to the **corr-gate representatives**
(the surviving strategies after the correlation fold), signed by direction (longs green/positive,
shorts red/negative; the signed terms sum to the ticker's net conviction). On ticker click-through,
show **entry + current price (open) / close price (closed)** plus these contributions, and **remove**
the old historical IR / mean_pct / Sharpe decomposition ("by-strategy stats are essentially the
same for each strategy and no longer relevant").

The contribution must reflect **the final adjustments of the whole sizing process** — which requires
the approved per-strategy size adjustment to actually be *part of* that process. It currently is not.

## 2. Key finding — the approved size knob is severed from the live sizer

The system has a real, operator-driven size-adjustment path that is **dead in the live sizer**:

```
comprehensive_review.js  →  proposed_size_scalar (Opus multiplier, 0..2, null = no change)
        →  strategy_regime_param_proposals (pending)   [surfaced on the strategy page]
        →  operator approves on the strategy page       [the approval gate]
        →  proposal_manager.approve → eligibility_manager.set_params
        →  strategy_regime_params.size_scalar           [per (strategy, regime)]
        →  regime_param_resolver.size_scalar(sid, regime)   ← reader exists …
        ✗  … but NOTHING in the live sizing path calls it per-strategy.
```

Evidence (verified 2026-06-09):
- `strategy_weights.rebuild()` reads `strategy_regime_params` for the **`eligible` boolean only**
  (`strategy_weights.py:188-192`); it computes `weight = effective_sharpe`,
  `daily_weight = effective_sharpe / sqrt(cadence_days)` and never reads `size_scalar`.
- `regime_blended_sizer.py` and `strategy_weights.py` contain **no reference** to `size_scalar`
  or `regime_param_resolver`.
- The only trade-path caller of the `size_scalar()` getter is `trade_handoff_builder.py:165-166`,
  which uses the sentinel `'__regime_default__'` → the **regime-level** Phase-1 scalar, never the
  per-strategy value; and the EOD sharpe-cadence sizer re-sizes from `strategy_weights_by_regime`,
  ignoring the handoff's `scale`.
- **25 approved non-default `size_scalar` values are sitting unused** in `strategy_regime_params`
  (e.g. `momentum_12_1` LOW_VOL ×1.5, `S22_quality_momentum` ×1.5, `S25/S9/S_barbell` ×1.25,
  `S12_insider` TRANSITIONING ×0.55, `S_epistemic_rank_gate` CRISIS ×0.5). 79 approved proposals,
  43 carrying a size_scalar.

This was almost certainly collateral damage from the **2026-05-21 legacy-sizer purge** (`7bf5392`),
which removed the mode-dispatch sizer that consumed both `size_scalar` and the separate
`target_pct_nav` knob; the new sharpe-cadence sizer never re-incorporated `size_scalar`.

### 2a. The other ("recommended_size_pct") knob is NOT the one to wire
`strategy_sizing_recommendations.recommended_size_pct` (Saturday position-recs → injected as
`sig['target_pct_nav']` in `regime_blended_sizer_live.py:405-414`) is **also dead** (consumed
nowhere since `7bf5392`) **and** corrupted by a 100× producer unit bug
(`position_recommender.js:80-82` adds a percentage-point delta to a fraction baseline →
`momentum_12_1` "recommends" 91% of NAV). It is a redundant orphan; it is **out of scope** here
(see §7). The clean, human-approved, per-(strategy,regime) multiplier is `size_scalar`.

## 3. Architectural constraints (must respect)

- **Fixed-gross renormalization** (`regime_blended_sizer.py:1019`):
  `scale = (λ × NAV) / Σ|ticker_w|`, so `target_usd = ticker_w × scale`. A *uniform* weight scaling
  is normalized away — `size_scalar` can only **redistribute** the fixed λ×NAV gross between
  strategies/tickers, never inflate total exposure.
- **FOLD is ON in prod** (`OPENCLAW_STRATEGY_FOLD=1`): `fold_active_contributions` collapses each
  correlated clump to one representative **before** the `ticker_w` sum (`regime_blended_sizer.py:904-928`).
  So `ticker_w` already sums **only corr-gate representatives** — "show representatives only" equals
  the actual sizing term, with no extra reconciliation.
- **VPS is 2-core / 7.8 GB, no swap** — tests run sequentially, `nice -n 19`.
- **NEVER DELETE FROM MASTER DATA** — schema changes are additive only.

## 4. Locked design decisions (operator-approved 2026-06-09)

1. **Wire `size_scalar`** (not `recommended_size_pct`).
2. **Allocation-only.** The scalar multiplies `weight_by_strat` (daily_weight → `ticker_w` → dollars).
   The cum-sharpe **gate** (`sharpe_by_strat` = effective_sharpe), the **fold representative
   selection**, AND the **bracket-leader pick** (which strategy's entry/stop/targets anchor the order)
   all stay on **raw** effective_sharpe/`weight_by_strat` — so an approved scalar resizes a position
   but never changes which tickers trade, which strategy represents a clump, or whose technical levels
   are used.
3. **No clamp.** `size_scalar` is human-approved, Opus-bounded `0..2`, with an existing magnitude
   rail in `proposal_manager` ("Rail 2"). NULL / missing / non-finite / negative → **1.0** (no
   change, fail-safe); an explicit finite `0.0` is honored as a deliberate mute.
4. **Apply at sizing time** (not baked into `strategy_weights_by_regime`): batch-load the regime's
   scalars in one query inside `_sharpe_cadence_path` and multiply. This keeps `strategy_weights`
   semantically pure (weights stay in Sharpe units) and makes an approval take effect on the **next
   cycle** rather than waiting for the weekly weights rebuild.
5. **Shadow-first.** New gate `OPENCLAW_STRATEGY_SIZE_SCALAR` (default **OFF**). When OFF, the live
   sizing is **byte-identical** to today (raw weights), and the sizer **logs the per-strategy and
   per-ticker dollar diff** the scalars *would* produce (mirrors the existing
   `OPENCLAW_STRATEGY_ORTHO_SHADOW` pattern at `regime_blended_sizer.py:988-1013`). The operator
   reviews a real diff, then flips the gate ON. Activating it switches on all 25 approvals at once.
6. **Display reflects reality.** The persisted per-strategy contribution = the **actual** allocation
   term used that cycle: `eff_weight_by_strat[sid] × direction` where
   `eff_weight = raw` while the gate is OFF (shadow) and `raw × size_scalar` once ON. Always honest
   about what drove the book.

## 5. Components

### A. Sizer wiring — `regime_blended_sizer.py` (`_sharpe_cadence_path`)
- **New gate** `OPENCLAW_STRATEGY_SIZE_SCALAR` (default OFF).
- **Batch loader** (new): one query for the active regime →
  `{strategy_id: size_scalar}` from `strategy_regime_params WHERE regime_state = %s AND size_scalar
  IS NOT NULL`. Add as `regime_param_resolver.size_scalars_for_regime(regime)` (keeps the resolver
  the single source). Missing / NULL / non-finite / **negative** → coerce to **1.0** at read
  (fail-safe). An explicit finite **0.0 is honored** (deliberate mute — contributes 0 to the
  allocation sum but still to the gate, consistent with allocation-only).
- Define `eff_weight_by_strat[sid] = weight_by_strat[sid] × scalar(sid)`.
  - Gate **ON**  → use `eff_weight_by_strat` for the **allocation** only: the `ticker_w[tkr] += … × d`
    sum. The **bracket-leader pick stays on RAW `weight_by_strat`** — the direction-leader (whose
    entry/stop/targets anchor the order) is chosen by raw conviction, NOT by the operator's dollar
    adjustment (a size_scalar must not silently change which strategy's technical levels are used).
    *(Revised post-review 2026-06-09: an earlier draft scaled the bracket weight too; reverted — see §4.2.)*
  - Gate **OFF** → use raw `weight_by_strat` for routing (unchanged), but compute the shadow
    `ticker_w'`/`target_usd'` from `eff_weight_by_strat` and `log.info` the per-strategy scalar set
    and the per-ticker `Δusd = target_usd' − target_usd` (no money moves).
- **Untouched:** `sharpe_by_strat` / `ticker_net_sharpe` / `gate_net_sharpe` (the cum-sharpe gate),
  and `fold_active_contributions` (representative selection) — both stay on raw effective_sharpe
  (decision §4.2).
- The function returns, per surviving (gated) ticker, the **representative contributions**
  `[{strategy_id, contribution = eff_weight × d, direction = d}]` in `ticker_meta`, for persistence
  by the live wrapper.

### B. Per-cycle contribution persistence — migration **134** + `regime_blended_sizer_live.py`
- **Migration 134** (additive; extends migration 130's `cycle_contributing_strategies`):
  add `contributions JSONB` — an array `[{"strategy_id","contribution","direction"}]` per
  (run_date, ticker). Keep the existing `strategies TEXT[]` column as-is (no drop). New rows write
  both; the endpoint prefers `contributions` and falls back to `strategies`.
- `regime_blended_sizer_live.py` already persists the contributing set each cycle; extend that write
  to include the `contributions` array from the sizer's `ticker_meta` (representatives only, signed).

### C. Ticker-alpha endpoint + modal — `server.js` (`/api/portfolio/ticker-alpha`)
- **Replace** the historical IR / mean_pct / Sharpe computation with a read of
  `cycle_contributing_strategies.contributions`:
  - **Open** position → latest `run_date` row for the ticker.
  - **Closed** position → the last `run_date` that sized it (most-recent row for the ticker).
- Payload: `{ ticker, status: open|closed, entry, current|close, net,
  contributions: [{strategy_id, contribution, direction}] }`.
- **Modal:** entry + current(open)/close(closed) price; the signed contributions colored by sign
  (long green/positive, short red/negative); a net total. Drop the old per-strategy stats table.
- Graceful fallback unchanged: if no `contributions` row, show the strategies list (or "—").

### D. Docstring correctness — `strategy_weights.py`
- Fix the stale comments (`:73-74`, `:98-100`) that claim "position-recs govern sizing" — they
  assert behavior the code never implemented. Reword to reflect that `size_scalar` (when the gate is
  ON) is the per-(strategy,regime) sizing adjustment, applied downstream in the sizer.

## 6. Data flow (end to end)

```
comprehensive_review (Opus 0..2)
  → proposal → OPERATOR APPROVES (strategy page) → strategy_regime_params.size_scalar
    → [NEW] sizer batch-load → × weight_by_strat (gated) → ticker_w → target_usd → orders
    → ticker_meta.contributions → [NEW] cycle_contributing_strategies.contributions
      → server.js /api/portfolio/ticker-alpha → modal (entry/current·close + signed contributions)
```

## 7. Out of scope (do NOT entangle)
- Removing the dead `recommended_size_pct → target_pct_nav` orphan (`regime_blended_sizer_live.py:405-414`).
- Fixing the 100× unit bug in `position_recommender.js` (cosmetic — only affects the
  `#position-recommendations` Discord table / dashboard rec display, not sizing). Worth a separate
  fix; confirm whether this is what "represent percentages properly" referred to.

## 8. Testing (TDD; sequential, `nice -n 19`)
- **Sizer:** scalar applied when gate ON scales a strategy's `ticker_w` share; gate OFF is
  byte-identical to today AND emits the shadow diff log; NULL/missing/≤0 → 1.0; Σ|target_usd| stays
  = λ×NAV (redistribution invariant); gate (`gate_net_sharpe`) and fold representative are unchanged
  by the scalar (allocation-only invariant).
- **Persistence:** `contributions` sums to `ticker_w` over the gated set; representatives only; signed.
- **Endpoint:** open reads latest cycle; closed reads last sizing cycle; entry/current·close present;
  fallback when no `contributions` row.
- **End-to-end spot check (manual, during build):**
  `regime_param_resolver.size_scalar('momentum_12_1','LOW_VOL') == 1.5`.

## 9. Rollout
- Build on `feat/trade-output-accuracy`. New migration 134 (additive).
- Ship with `OPENCLAW_STRATEGY_SIZE_SCALAR=OFF` (shadow). Operator reviews the per-cycle dollar diff
  for a cycle or two, then flips the gate ON to activate the 25 approved scalars.
- Operator owes (already pending on this branch): merge `feat/trade-output-accuracy` + johnbot restart.
- **Note:** migration-number divergence — `130` is `cycle_contributing_strategies` on this branch but
  `eod_health_panel_max_date` on `feat/sp6-phase-a`; the operator reconciles numbering at merge.
