# SP-4 Follow-up — Instrument-Class-Aware Backtest Dispatch (Design)

**Date:** 2026-05-27
**Status:** Design approved (operator: "looks good"). Plan + execution to follow in worktree `worktree-sp4-backtest-dispatch` (branched from `main` `ad41f79`, the SP-4 A–D merge).
**Builds on:** SP-4 Phase 0 (synthetic greeks options engine + `run_backtest(instrument_class=...)` dispatch) and SP-4 Phases A–D (origination writes `instrument_class` to the manifest + a module-level `INSTRUMENT_CLASS` const).

---

## 1. Goal

Make `python -m backtest.unified_backtest` resolve each strategy's `instrument_class` and pass it to `run_backtest`, so an originated/registered **option** strategy backtests on the Phase-0 synthetic greeks engine instead of the equity delta-1 path.

This closes the integration gap surfaced by the SP-4 A–D proof: origination correctly tags a strategy `instrument_class=option` (manifest + module const), but the backtest CLI never reads that tag, so `run_backtest` defaults to `instrument_class='equity'` → wrong (delta-1) metrics for options.

## 2. Context — the precise gap

In `src/backtest/unified_backtest.py`:
- `run_backtest(strategy_id, *, ..., instrument_class='equity')` (line 589) and `_simulate_for(instrument_class)` (line 579) **already dispatch correctly** — `_simulate_for('option')` returns `options_backtest.simulate`; **everything else returns `_per_bar_simulate`** (so `equity`/`etp`/`crypto` are identical).
- `main()` (lines 783–833) calls `run_backtest(...)` in all three branches — `--strategy-id`, `--strategy-file`, `--all-live` — **without** passing `instrument_class`, so it always defaults to `'equity'`.

The orchestrator's convergence-gate backtest is `python -m backtest.unified_backtest --strategy-file <impl>` (`research-orchestrator.js`), so an originated option strategy hits the CLI's equity default.

## 3. Decision (locked)

**Always-on** (not gated). The dispatch is a correctness fix: backtesting an option strategy on the equity engine is simply wrong. Because only `'option'` diverges in `_simulate_for`, `equity`/`etp`/`crypto` remain **byte-identical** regardless. Behavior change is therefore confined to option strategies (today only the reference `S_short_straddle_vrp`).

## 4. Architecture

One helper + three call-site edits, all in `src/backtest/unified_backtest.py`. **No orchestrator change** (the CLI self-resolves). **No migration. No gate.**

### `_resolve_instrument_class(strategy_id, filepath=None) -> str`

Resolution precedence:
1. **Manifest** (authoritative — same source the lifecycle promotion gate reads): `strategies[strategy_id].instrument_class`, accepted only if in `VALID_INSTRUMENT_CLASSES` (imported from `strategies.lifecycle`).
2. **Module const** (covers a not-yet-registered `--strategy-file`): if a `filepath` is given and step 1 missed, read the top-level `INSTRUMENT_CLASS = "..."` via the existing `strategies.lifecycle._detect_module_instrument_class(filepath)`.
3. **Default** `'equity'`.

Manifest-first matches what promotion enforces; the module-const fallback handles a freshly-coded file backtested before it lands in the manifest. Any read failure or unrecognized value falls through to `'equity'`.

### Wiring in `main()`

Each of the three branches resolves the class and threads it:
- `--strategy-id`: `ic = _resolve_instrument_class(args.strategy_id)` → `run_backtest(args.strategy_id, ..., instrument_class=ic)`.
- `--strategy-file`: `sid = Path(args.strategy_file).stem; ic = _resolve_instrument_class(sid, filepath=args.strategy_file)` → `run_backtest(sid, filepath=..., ..., instrument_class=ic)`.
- `--all-live`: per-sid `ic = _resolve_instrument_class(sid)` inside the loop → `run_backtest(sid, ..., instrument_class=ic)`.

## 5. Data flow

```
CLI (--strategy-id | --strategy-file | --all-live)
  └─ _resolve_instrument_class(sid[, filepath])
        ├─ manifest[strategies][sid].instrument_class ∈ VALID_INSTRUMENT_CLASSES ? → use it
        ├─ else filepath ? _detect_module_instrument_class(filepath) → use it
        └─ else 'equity'
  └─ run_backtest(sid, ..., instrument_class=<resolved>)
        └─ _simulate_for(<resolved>)  → options_backtest.simulate (option) | _per_bar_simulate (else)
```

## 6. Error handling

- Manifest missing/unreadable/malformed → caught, fall through to step 2/3.
- `strategy_id` absent from manifest → step 2/3.
- Unknown/invalid class value (manifest or const) → not in `VALID_INSTRUMENT_CLASSES` → ignored → `'equity'`.
- Net: the resolver never raises; worst case it returns `'equity'` (today's behavior).

## 7. Testing

Deterministic, fast — no full backtest run required:
- **Unit** (`_resolve_instrument_class`, temp manifest/file fixtures):
  - manifest entry `option` → `'option'`;
  - sid absent from manifest but `filepath` has `INSTRUMENT_CLASS = 'option'` → `'option'` (module-const fallback);
  - neither present → `'equity'`;
  - manifest value `'banana'` (invalid) → `'equity'`;
  - manifest `crypto`/`etp` → returned verbatim.
- **Dispatch-wiring assertion** (ties resolution → engine without running a backtest): against the live manifest, `_simulate_for(_resolve_instrument_class('S_short_straddle_vrp')) is options_backtest.simulate`, and `_simulate_for(_resolve_instrument_class('<an equity strategy>')) is _per_bar_simulate`.

## 8. Out of scope

- Orchestrator (`research-orchestrator.js`) changes — none needed.
- `auto_backtest.py` — its Phase-0 option-refusal guard stays (unrelated path).
- Promoting any option strategy (still operator-gated; this only makes the backtest *metrics* correct).
- The greeks engine itself / IV model (Phase 0, already shipped).

## 9. Grounding note (for plan-writing)

Verify against live source before dispatching subagents: `unified_backtest.py` `main()` arg parsing + the three `run_backtest` call sites (lines 783–833), `run_backtest`'s `instrument_class` kwarg (line 597) + `_simulate_for` (579), `VALID_INSTRUMENT_CLASSES` + `_detect_module_instrument_class` import path in `strategies.lifecycle`, the `ROOT`/manifest-path constant in `unified_backtest.py`, and the pytest sys.path header convention.
