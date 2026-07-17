# Option B — Retire the registry backtest mirror (design)

**Status:** DESIGN — awaiting operator approval before implementation (deploy-gated).
**Context:** §7 metric reconciliation. Now that the canonical `strategy_backtest_runs`
(true-MTM + adverse slippage) is trustworthy, the stale/unit-corrupt registry mirror
(`strategy_registry.backtest_sharpe / backtest_return_pct / backtest_max_dd_pct /
backtest_trade_count / backtest_regime_breakdown`) should stop being read as a
fallback/authority. **Append-only invariant: columns are deprecated-in-code only, NEVER dropped.**

## Goal
Point every *read* consumer of the registry backtest mirror at canonical
`strategy_backtest_runs` (+ `strategy_backtest_regimes` for per-regime), and remove
the mirror as a silent fallback. Keep the mirror's *write* path intact for now
(backward-compat) but flag it deprecated.

## Read-consumers to repoint (grep-grounded 2026-07-05)
1. **`src/lib/promotion_service.js:27-32` — gate-fallback (canonical NaN → registry).**
   The load-bearing one. Retire the registry fallback block.
   **KEY DECISION (promotion-safety):** today `if (!isNaN(sharpe) && sharpe < min)` — if
   BOTH canonical and registry are NaN, `sharpe` stays NaN and the gate does **not** flag
   it → the strategy **auto-passes** the sharpe/DD gates. Removing the registry fallback
   *widens* the NaN surface. Fix: treat a missing/NaN **canonical** metric as a **HARD
   fail** (`failedGates.push('no_backtest')`) — you cannot promote a strategy with no
   valid backtest. `force=true` still bypasses (unchanged). This is stricter than today
   and is the correct direction, but it is a behavior change — operator must confirm.
2. **`src/research/strategy_forensics.py:54,175`** — reads `registry.backtest_sharpe`.
   Repoint to canonical latest primary_window run (join by the strategy_id mapping).
3. **`src/services/mastermind_chat/snapshot.py:147`** — mastermind snapshot reads registry
   backtest fields. Repoint to canonical so MasterMind reasons on honest numbers.
4. **`src/channels/api/routes_research.js:279`** — `reg.backtest_sharpe AS registry_sharpe`
   (research-tab display). Repoint to canonical, or relabel as an explicit "legacy mirror"
   diagnostic if the UI intends to show the divergence.
5. **`src/channels/discord/relay.js:143`** — Discord strategy-list Sharpe. Repoint to canonical.
6. **`src/channels/api/server.js` (dashboard :7870)** — ALREADY canonical-first
   (`unifiedBacktest[sid]?.X ?? sr.backtest_X ?? null`). Low priority: drop the `?? sr.…`
   tail for cleanliness (behavior unchanged on the happy path). Optional in this pass.

## NOT in scope (leave intact)
- **Write sites** `research-orchestrator.js:919-923` (UPDATE) + `:1194` (INSERT) — keep
  writing the mirror for now (backward-compat); add a `-- DEPRECATED: mirror` code comment.
  A later pass can stop the writes once no reader remains.
- **No migration, no column drop** (append-only). Columns stay; only reads move.
- The `strategy_id` ⟷ registry `id`/`name` namespace mapping is real (registry `id` is a
  slug; canonical keys on `strategy_id`). Each repoint must resolve via the registry `id`
  slug → `strategy_backtest_runs.strategy_id`, same mapping used in the F2 sheet.

## Test plan
- `promotion_service`: unit tests for (a) canonical present → uses canonical; (b) canonical
  missing → HARD fail `no_backtest` (NOT silent pass); (c) `force=true` bypass; (d) class-aware
  floors unchanged. Pin the NaN-hard-fail behavior with a regression test.
- forensics/snapshot: assert the repointed query returns canonical values for a known strategy.
- No behavior change on the dashboard happy path (canonical-first already).

## Rollout
SDD cycle (spec→plan→implement→task-review→final review) on a branch; **deploy operator-gated**
(johnbot restart for the JS gate/relay; mastermind-chat restart for snapshot.py). Sequence
AFTER the Phase 1e reweight lands (mirror retirement only pays off once canonical is the
trusted authority, which the reweight makes it in the live sizer too).
