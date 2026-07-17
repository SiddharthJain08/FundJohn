# Regime-Blended Sizer — Spec Revision (2026-05-12)

Surgical corrections to `2026-05-11-regime-blended-position-sizing-design.md` after Phase 2 Task 12 surfaced plan-vs-reality gaps.

**The 11 locked design decisions remain unchanged.** Only architecture/components/data-flow plumbing is revised.

## Verified facts (from live system inspection)

1. **Production trade step is `trade_agent_llm`** (LLM call), with `deterministic_sizer.size_orders()` as a Python fallback inside it (`_run_deterministic_sizer`). Both paths feed `_finalize_sized_payload(payload, source=...)`.
2. **`p_t1`, `entry`, `stop`, `t1`, `ev_gbm` are top-level keys** on each signal *after* `trade_handoff_builder.py` enriches the handoff. Raw `execution_signals` rows have these in `signal_params` JSONB; the handoff lifts them.
3. **`execution_signals` schema**: `id UUID`, columns `entry_price`, `stop_loss`, `target_1`, `target_2`, `target_3` (NOT `take_profit_1`), `regime_state`, `signal_params JSONB`, `confluence_count`, etc.
4. **`market_regime` schema**: timestamp column is `updated_at` (NOT `ts`).
5. **`strategy_sizing_recommendations` table exists** with 14 rows (last write 2026-05-09). Sizing column is `recommended_size_pct` (NOT `target_pct_nav`).
6. **`signal_pnl` schema**: trade close timestamp is `closed_at` (NOT `exit_ts`); pnl column is `realized_pnl_pct` (Task 11 fix already applied).
7. **`parity_orders.contributing_signal_ids` is `BIGINT[]`** but actual signal IDs are `UUID`. Migration 070 needed.

## Surgical corrections

### Correction 1 — Production sizer is `trade_agent_llm`, not `deterministic_sizer`

**Spec change:** In Architecture and Migration/Rollout sections, replace every reference to "deterministic_sizer is the production submitter" with "trade_agent_llm is the production submitter; deterministic_sizer is a Python fallback inside trade_agent_llm."

**Implication for parity:** Task 17 ("Mirror deterministic_sizer to parity_orders") is re-scoped to "Mirror trade_agent_llm's submitted orders to parity_orders with `source='production'` by reading `alpaca_submissions` after the trade step completes." `deterministic_sizer` keeps running as the LLM fallback, untouched.

**Implication for Phase 3/4 cutover:** When `OPENCLAW_REGIME_BLENDED_LIVE=1`, `regime_blended_sizer` REPLACES `trade_agent_llm` entirely (TradeJohn moves from upstream picker to inside-the-sizer confirmer). `deterministic_sizer` continues to be available as a Python fallback inside `regime_blended_sizer` if the confirmer LLM is unavailable AND the formula path also degrades. Phase 4 retirement removes only the trade_parity step, not `deterministic_sizer.py`.

### Correction 2 — `kelly_p` enrichment via shared helper

**Spec change:** Add `src/execution/_kelly.py` to the components list. Extract `_reward_to_risk()` and `_kelly_fraction()` from `deterministic_sizer.py:120-145` into the shared helper. Both `deterministic_sizer.py` and `regime_blended_sizer.py` import from `_kelly`.

**Implementation pattern:** In `regime_blended_sizer.size_positions()`, immediately after the cadence gate, run a kelly-enrichment pass:

```python
from execution._kelly import enrich_with_kelly
enriched_signals = enrich_with_kelly(passed_signals)  # adds kelly_p to each signal dict
```

`ticker_consolidator.consolidate()` stays a pure function — it consumes signals that already have `kelly_p` populated.

**Test impact:** `tests/test_ticker_consolidator.py` already passes signals with `kelly_p` set explicitly — no change needed. New tests for `_kelly.py` validate the extracted helpers.

### Correction 3 — Schema column corrections

**Mechanical find/replace across new code:**

| Old (in plan) | New (real) | Files |
|---|---|---|
| `take_profit_1` | `target_1` | `regime_blended_sizer_parity.py`, any test fixtures |
| `market_regime.ts` | `market_regime.updated_at` | `regime_blended_sizer_parity.py`, parity_diff.py |
| `target_pct_nav` (column) | `recommended_size_pct` | `regime_blended_sizer.py:_independent_path`, parity wrapper, any SQL |
| `exit_ts` | `closed_at` | already fixed in `regime_performance_analyzer.py` (Task 11) |
| `signal_pnl.pnl` | `signal_pnl.realized_pnl_pct` (alias as `pnl`) | already fixed (Task 11) |

**Note on `target_pct_nav`:** The internal Python variable name in `regime_blended_sizer._independent_path()` can stay as `target_pct = sig.get('target_pct_nav')` IF the upstream wrapper extracts `recommended_size_pct` from the DB row and stores it in the signal dict under the key `target_pct_nav`. Keep the in-memory naming aligned with the spec; only the DB column name differs.

### Correction 4 — Migration 070 (UUID type fix)

```sql
-- src/database/migrations/070_parity_orders_uuid_fix.sql
-- Fix: contributing_signal_ids was BIGINT[] but execution_signals.id is UUID.
-- parity_orders is empty in dev; safe to DROP and re-add.

ALTER TABLE parity_orders DROP COLUMN contributing_signal_ids;
ALTER TABLE parity_orders ADD COLUMN contributing_signal_ids UUID[];
```

After applying, back out Task 12's workaround (which stashed UUIDs in `bracket_json['contributing_signal_ids']`) — `regime_blended_sizer_parity.py` should write UUIDs directly to the column.

### Correction 5 — Parity contract paragraph

(New paragraph to add to spec under "Data flow" section.)

**Parity contract (Phase 2):**

`parity_orders` table accumulates rows from two sources per signal_date:

- `source='production'`: one row per `alpaca_submissions` insert (mirrored by a new step `trade_parity_capture` after `alpaca`). Captures whatever the production path actually submitted to broker — `trade_agent_llm` (LLM) or its `deterministic_sizer` fallback.
- `source='regime_blended'`: one row per order produced by `regime_blended_sizer` running in DRY-RUN (the existing `trade_parity` step after `trade`). Uses the stub confirmer (no LLM cost) so parity is about the formula's output, not LLM judgment.

**`parity_diff.py` compares the two sources at ticker level**, tolerance 1% per ticker. Large diffs (>1%) get listed in the daily summary to `#botjohn-log`. After 30 trading days of clean parity in HIGH_VOL/CRISIS regimes (where the LLM has less room to deviate from mechanical sizing), operator flips `OPENCLAW_REGIME_BLENDED_LIVE=1` and `regime_blended_sizer` becomes the production path. `trade_agent_llm` is then run in DRY-RUN as the rollback canary for an additional 30 days.

## Affected tasks (revisions to apply)

| Task | Revision needed |
|---|---|
| Task 1 (migration 069) | No change — UUID fix lives in new migration 070 |
| Task 5 (ticker_consolidator) | No code change — assume signals come pre-enriched with kelly_p |
| Task 7 (regime_blended_sizer) | Add kelly-enrichment call before consolidate; rename DB column references |
| Task 12 (orchestrator + parity wrapper) | DONE with workarounds; back out the bracket_json hack once migration 070 applies; switch to real Kelly via `_kelly.py` |
| Task 16 (parity_diff) | Compare source='regime_blended' vs source='production' (not 'deterministic') |
| Task 17 (mirror) | Re-scope: read `alpaca_submissions` after trade step, mirror to `parity_orders` with source='production'. NEW STEP `trade_parity_capture` after `alpaca` step in pipeline_orchestrator |
| Task 28 (LIVE flag) | Phase 3 doc: clarify that flipping the flag REPLACES `trade_agent_llm` with `regime_blended_sizer`, not just changes which sizer's orders submit |
| Task 29 (retirement) | Phase 4 doc: remove `trade_parity` and `trade_parity_capture` steps; `deterministic_sizer.py` and `trade_agent_llm.py` both stay on disk as fallbacks/canaries |

## New tasks (to add to plan)

| ID | Task |
|---|---|
| Task 11.5 | Migration 070: fix parity_orders.contributing_signal_ids UUID[] |
| Task 11.6 | Create `src/execution/_kelly.py` (extract from deterministic_sizer); add unit tests |
| Task 11.7 | Patch `regime_blended_sizer.py` to enrich signals via `_kelly.enrich_with_kelly()`; back out workarounds in `regime_blended_sizer_parity.py` |

These three tasks land before resuming Phase 2 from Task 13.

## Resume-from checkpoint

After applying corrections 1-5 and Tasks 11.5-11.7:
- Phase 2 resumes at Task 13 (cron schedule additions) with the corrected base.
- Existing Phase 0 unit tests still pass (consolidator's `kelly_p` assumption now matches enrichment pass).
- Task 12's smoke test should produce > 0 orders for 2026-05-08 once Kelly enrichment is wired (was 0 because `kelly_p=None` everywhere).
- Tasks 14-21 dispatch as planned, with Task 16 and Task 17 using the revised contract.

## What does NOT change

- All 11 locked design decisions (regime modes, ER weight, λ, attribution, etc.)
- TradeJohn confirmer prompt + role
- Cadence gate logic
- Position circuit breaker design
- Strategy creation pipeline (regime_performance_analyzer is good)
- Saturday review refresh design
- Walk-forward backtest harness

---

## 2026-05-12 — Phase 3 gate dropped; operator-driven flip

Operator decision: the "30 trading days of clean parity in HIGH_VOL/CRISIS" gate referenced in this revision is no longer the gating condition for the LIVE flip. The operator monitors live behavior themselves and flips `OPENCLAW_REGIME_BLENDED_LIVE=1` when ready, without an automated readiness signal. Rationale + full procedure documented in the addendum on `2026-05-11-regime-blended-position-sizing-design.md`. `parity_diff` continues running as audit-trail/bug-tripwire, decoupled from any countdown.
