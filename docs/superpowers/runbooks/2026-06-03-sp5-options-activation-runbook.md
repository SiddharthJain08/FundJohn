# SP-5 Options Activation Runbook (5.1b-ii + 5.1c stack)

**Date:** 2026-06-03
**Status:** ACTIVE — Phases 0/1a/1b COMPLETE (tip 5dbf3a7); Phase 2 review→merge→deploy next (operator-gated)

**Phase results:**
- Phase 0 DONE: main merged in (04ca488; one conflict, executor re-expression verified
  2-hunks-vs-main), reviewer APPROVED, 170+ tests + real-DB round-trip PASS. Merge-widening
  catch: hedge='delta' restricted to all-buy structures (81a4a2d) — a delta-hedged vertical
  would have produced a WRONG hedge (ledger legs carry no side; compute_structure_delta sums
  unsigned).
- Phase 1a DONE (57d8a83..c85ed88): partition consolidation + broker OCC filter + reconcile
  option-row skip + G2 ledger warning; partition moved BEFORE the orthogonalization fold
  (implementer-flagged silent-drop hazard). 244-test surface green.
- Phase 1b DONE (ebee95c..5dbf3a7): G3 fail-closed (malformed spec / unnormalizable direction
  drops the order, never falls to equity), G4 close path (held_legs dispatch generalization +
  option orphan-close emission gated on OPTION_EXEC + hedge-ledger deactivation on close,
  status='closed' flag pattern), PLUS the implementer-surfaced pre-promotion gap: option OPENS
  through the sizer emitted as opens, not close_only (5dbf3a7). 144-test regression green.
**Operator decision (2026-06-03):** **Arm + smoke, no promotion.** Close G1–G4, integrate the
stack, merge + deploy inert, run the T9 supervised hedge smoke via a temporary gates-on window
(candidate stays UNREGISTERED), gates back OFF. A future organic candidate (Saturday-originated,
passing 0.80/0.30) then activates in one step: promote → smoke → flip.
**Spec:** `docs/superpowers/specs/2026-06-03-sp5.1c-options-exec-activation-design.md`
(§Pre-Activation Gates G1–G4).

---

## Architecture clarification on record (operator exchange, 2026-06-03)

The hedge points **from options to equity**: an option strategy holds a long-vol structure
(`hedge='delta'`); the EOD step reads live greeks → net delta → injects an offsetting APPROVED
**equity** row. P&L thesis = delta-neutral long gamma monetizing realized vol — but at **EOD
cadence only** (rides SP-6 compute[T]→fill[T+1]). It does NOT hedge the equity book, and it does
NOT rebalance intraday. **Intraday rehedge is a deferred upgrade** (would ride the B1 scheduler
substrate) — scope separately after this runbook completes. The hedge subsystem is
**strategy-less infrastructure**: `__hedge__<sid>` registry rows are FK plumbing
(`pending_approval`, never traded); the reference candidate `S_long_straddle_delta_hedged` is
chain-prover scaffolding and is never registered in this runbook.

## Load-bearing invariants

1. **G1 must be live BEFORE any option-strategy promotion — not merely before gate flip.**
   The `option_spec` attribution block in `_sharpe_cadence_path` is UNGATED; today's safety
   holds because no option strategy is registered. Promoting an option strategy whose underlying
   overlaps equity strategies (SPY does: 10 equity strategies signalled SPY in the last 30 days)
   contaminates the consolidated net and mis-routes it as a structure — regardless of
   `OPENCLAW_OPTION_EXEC`. Live sequence is always: **deploy(G1-fixed) → promote → smoke → flip**.
2. **`OPENCLAW_INSTRUMENT_CLASS_ROUTING=1` is ALREADY set in the live `.env`** (option sizing).
   Inert only while no option signal exists. Do not treat "all option gates are off" as true.
3. **Live checkout discipline:** `/root/openclaw` is on `feat/sp6-phase-a-eod-open-execution`
   (07441ad) with 3 uncommitted live-critical files (`manifest.json`, `strategy_signatures.json`,
   `run_sentiment_step.py`). NEVER `git reset --hard` there. Deploy is `git merge --ff-only main`
   — verified safe: 07441ad is an ancestor of main AND main has not touched those 3 files.
4. **SP-6 cron timing:** johnbot restart must avoid the 16:15 ET EOD compute, 9:28 ET reconcile,
   and 3:55 ET sizer windows.
5. All smokes/probes: operator-OK at fire time, RTH only, paper account, process-scoped gates
   (`.env` never modified), independent post-verify (positions/orders/residuals), never trust ack.

## Phases

### Phase 0 — Integration (task #66) — IN PROGRESS
Merge `main` (f8f628a: SP-6 + 5.1a + 5.1b-i + 5.2 + 5.2b) into
`feat/sp5.1c-options-exec-activation` (0ebe272 = 5.1b-ii 8e7719b + 5.1c).
- One conflicted file (probed via `git merge-tree`): `src/execution/alpaca_executor.py` —
  5.1c's C3 (hedge='delta' envelope-lift for long structures + `option_hedge_ledger` write on
  fill) was written against the pre-5.2 executor; re-express it onto 5.2b's final
  structure-aware envelope (side-aware helpers, credit structures, intent-aware net guard,
  signed limits).
- Exit: full options+hedge test surface green (5.1c ~300 + 5.2/5.2b ~95 + hedge 38) +
  `scripts/sp5_1c_roundtrip.py` real-DB round-trip PASS (rolled back) + reviewer pass on the
  conflict resolution.

### Phase 1a — G1 + G2 (task #67, blocker)
Per-`(ticker, instrument_class)` consolidation in `regime_blended_sizer._sharpe_cadence_path`:
option-class contributors get their own net/meta/emission, never blended with equity weight on
the same underlying. G2 rides along (option-only contributor set ⇒ stable ledger key).
TDD; **equity-byte-identical regression is the gate** (the sizer is live-critical for all
strategies — SP-6 EOD trades through it daily).

**Phase-1a design (locked 2026-06-03 after grounding; three prongs):**
1. **Partition-at-source consolidation.** Split `active` by class at the top of the
   aggregation (`option` iff `signal_params` carries `option_spec`); the ENTIRE existing
   equity pipeline runs UNCHANGED on the equity partition (no option signals ⇒ the partition
   is a no-op ⇒ byte-identical trivially). Option partition consolidates in a new
   `_consolidate_option_orders` helper: per-underlying Σ weight×dir + Σ sharpe×dir, the SAME
   `min_cum_sharpe` gate, first-spec-wins on conflicting specs (loud log), option-only
   composite `strategy_id` (G2). **Sizing = on-top with the hedge-style headroom guard**
   (scale OPTIONS down, never equity; mirrors the operator-approved 5.1b-ii hedge model) at
   per-unit-weight USD parity with the equity scale. Option emissions are pass-through
   `(underlying, usd, 'delta')` — NO broker netting, NO flip/orphan participation (option
   position lifecycle belongs to the executor + EOD carried set, not the equity book logic).
2. **Broker OCC-filter (grounding catch #2).** `_load_broker_positions_usd` returns EVERY
   broker position keyed by raw symbol — held option legs (OCC symbols, e.g.
   `SPY260626C00755000`) would be `orphan_close`d as equity by BOTH the daily sizer AND the
   9:28 `run_reconcile` (same loader + `_classify_position_deltas`). Fix: OCC-pattern filter
   (`[A-Z.]{1,6}\d{6}[CP]\d{8}$`) inside the shared loader (loud log of excluded legs) —
   both consumers protected in one place.
3. **Reconcile target-side class filter (grounding catch #3).** `_load_approved_set` builds
   sign-only per-ticker targets from ALL APPROVED rows; an APPROVED option row for SPY would
   wrongly shield a dropped equity SPY position from orphan-close. Fix: skip option-class
   rows (signal_params carries option_spec) when building the reconcile's equity target map.
   G4 (the option close path itself) remains Phase 1b.

G2 residual: if a SECOND option strategy on the same underlying ever promotes, the composite
key changes → `upsert_hedge_ledger_on_fill` gains a loud operator-visible warning when an
ACTIVE ledger row exists for the same underlying under a different strategy_id (surfaced,
never silently superseded).

### Phase 1b — G3 + G4 (task #68)
G3: carried `option_spec` failing `OptionSpec.from_dict` currently falls through to the EQUITY
executor → make it fail-closed (skip + loud log). G4: close-path `option_spec` carry — ground
against the POST-merge executor (5.2's `_route_mleg_close` reads held legs by underlying, so the
executor side may need only `instrument_class` on the close order). Both designed against the
Phase-0-integrated executor.

### Phase 2 — Review, merge, deploy (task #69) — OPERATOR-GATED
- Opus whole-branch review of the integrated stack.
- Merge to main: throwaway-worktree `--no-ff`, tree==branch proof, backup tag, push (the
  established pattern; live checkout untouched).
- Deploy: live checkout `git merge --ff-only main` + johnbot restart, cron-timed per invariant 4.
  Everything is gate-OFF inert on deploy (invariant 2 noted).

### Phase 3 — T9 supervised hedge smoke + arm (task #70) — OPERATOR-GATED
Temporary gates-on window (`OPENCLAW_OPTION_EXEC=1` + `OPENCLAW_OPTION_DELTA_HEDGE=1`,
process-scoped), script-driven, candidate UNREGISTERED:
1. Fill a small long delta-hedged structure (ATM SPY straddle, 1 contract).
2. Verify `option_hedge_ledger` row written on fill (the C3 seam).
3. Run `compute_option_hedge_targets` → verify FK-satisfied APPROVED `is_hedge` row +
   `hedge_shares`; verify sizer injection path (dry/inspect — no hedge fill needed).
4. Close the structure per-leg → flat-verify (long-leg re-flatten retry is load-bearing,
   per 5.2/5.2b smokes).
5. Independent post-verify: equity book untouched, zero residuals, zero open option orders.
6. Gates back OFF. Clean the smoke's ledger/signal rows per append-only policy
   (test rows via rollback where possible; anything persisted gets `active=false`-style
   deprecation, never DELETE from canonical tables).

### Armed state (exit criteria of this runbook)
- main + live disk carry the full options exec lane + hedge stack + G1–G4 closed, gates OFF.
- Hedge loop live-proven by T9 (fill → ledger → EOD hedge row).
- **Future organic-candidate activation procedure** (one step, operator-owned):
  1. Candidate passes 0.80/0.30 → register/approve in `strategy_registry` (G1 already live).
  2. Supervised candidate smoke (first EOD cycle watched: structure fill + hedge row + T+1 fill).
  3. Flip `OPENCLAW_OPTION_EXEC=1` (+ `OPENCLAW_OPTION_DELTA_HEDGE=1` if the candidate hedges)
     in `.env` via `printf >>`, restart johnbot (cron-timed), arm a fill-verify watchdog for the
     first live cycle (SP-6 pattern: `sp6_fill_verify.py` analog).

## Abort paths
- Phase 0/1: branch work only — abandon/fix freely; never touches main or live.
- Phase 2 merge: `git merge --abort` in the throwaway worktree; backup tag restores main.
- Phase 2 deploy: live checkout rollback = `git reset --keep <pre-deploy-tag>` (NEVER --hard);
  restart johnbot.
- Phase 3 smoke: built-in flatten + re-flatten; on orphan → manual `position close` per leg +
  gates OFF + surface to operator. No destructive path: paper account, 1-contract sizes,
  equity book independently verified untouched.
