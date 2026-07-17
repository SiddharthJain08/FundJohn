# Phase 3 — oversized-file splits (execution runbook)

Turnkey plan for the restart-coupled / fleet-blocked splits deferred from the
2026-07-17 simplification campaign. Grounded in a read-only structural analysis
(workflow wf_8673b83b-a29). **Execute only after the fleet re-backtest AND the
Oxford re-hearing queue have both exited** (`refresh_backtests_resumable.js` +
`backtest_ids.js --ids` gone; deadline was 2026-07-18T06:00 UTC).

## Universal rules (every split)
- **Behavior-preserving only** — pure code movement + import/require rewiring.
  Every existing export, CLI, and require-path from callers must survive.
- **One atomic commit per module** — never leave the tree with `require()`/
  `import` pointing at a not-yet-created file across a timer boundary (a
  timer-spawned run would crash at load).
- **Gate each step** before committing; **don't run the heavy suite while the
  fleet runs** — use the light smokes below, cap Python runs with
  `systemd-run --scope -p MemoryMax=2500M` (not `ulimit -v`, which breaks Arrow).
- Deploy windows: avoid the 10:00 ET daily cycle, the 09:00–19:00 ET intraday
  redeploy window, EOD (20:15/20:30 UTC), and the weekend maintenance timers.

## Order of execution (lowest risk first)

### 1. run_maintenance.js — prompt extraction (LOW, no restart)
1,110 → ~415 LOC. It's ~700 LOC of pure prompt-string data, not per-mode logic.
- New: `src/agent/maintenance/prompts/daily.js` (DAILY_PROMPT verbatim),
  `src/agent/maintenance/prompts/weekend.js` (the 4 weekend templates verbatim).
  **The `\{\{...\}\}` / `$\{\{COST_CAP_USD\}\}` escapes are load-bearing — copy
  verbatim, no reformat.**
- `run_maintenance.js` keeps the dispatcher + shared machinery + entire
  `module.exports` (still re-exporting the 5 prompt names). COST_CAP_USD stays
  in the dispatcher (module-level mutable, reassigned in main()).
- Gate: `node --test tests/agent/test_botjohn_maintenance_runner.test.js`; plus
  byte-diff `buildPrompt()` output for all 5 modes before/after (the oracle).
- Deploy: subprocess/timer-spawned → NO johnbot restart; land outside timer
  windows (safest: Tue–Thu 14:00–17:00 ET). NOT the lane/config Phase-2.

### 2. alpaca_executor.py — seam-free helper extraction ONLY (Phase-1, no restart)
2,983 → ~2,590 LOC. **Do NOT extract the option/crypto lanes** —
`execution.alpaca_executor.<name>` is the monkeypatch surface for ~15 test
seams; moving a seam (or a caller of one) silently breaks test fidelity on the
LIVE order path. Extract only pure, seam-free, global-free helpers:
- New: `src/execution/executor_helpers.py` (~205 LOC: `_static_session`,
  `_normalize_alpaca_symbol`, `_dtbp_*`, `_recompute_bracket_from_quote`,
  `MIN_BRACKET_GAP_PCT`, `_placed_or_order_*`, …) and
  `src/execution/executor_options_helpers.py` (~185 LOC: `_build_occ_symbol`,
  `_cash_collateral_*`, `_resolve_option_qty`, `_build_mleg_legs_json`, …).
- `alpaca_executor.py` stays the facade: keep `execute_single`, `main`, all
  mutable globals (`_asset_cache`, `_broker_positions_cache`, …), all patch
  seams, and add explicit `from execution.executor_helpers import (…)  # noqa: F401`.
- **Fidelity guard (the criterion that qualified each move):** for every moved
  name, `grep -rn "patch('execution.alpaca_executor.<name>'"` and
  `patch.object(a[ex], '<name>')` must return ZERO hits. Confirm before commit.
- Gate: `pytest tests/execution -k "executor or alpaca or option or crypto or
  dtbp or bracket or reconcile or vertical or credit or close_inflight or
  handoff" tests/system_checks/test_option_routing_check.py -q` — byte-match
  baseline. Import smoke asserts every re-exported symbol present on the facade.
- Deploy: subprocess-spawned → NO restart; land outside daily+intraday windows.

### 3. doctor.py — per-domain check modules (MEDIUM, no restart)
1,907 → ~180 LOC runner. 40 `@_check` functions → 8 domain modules registering
into a shared registry.
- **Import model is the trap:** src/ has no `__init__.py` (namespace pkgs) and
  the suite imports via BOTH `maintenance.doctor` and `src.maintenance.doctor`.
  Use ABSOLUTE imports rooted at `maintenance.` (never relative, never
  `src.maintenance.`) or you get a split-brain registry / ImportError under the
  bare-script systemd invocation.
- New: `doctor_common.py` (PASS/WARN/FAIL, `_ok/_warn/_fail`, the `_check`
  decorator changed to append to a module-level `_REGISTRY`, seams
  `_run_alpaca_cli`/`_parquet_last_date`, constants) + `doctor_checks_{system,
  broker,data,universe,intraday,strategies,infra,regime}.py`. `doctor.py`
  becomes the runner/CLI (`run`/`_all_checks`→`_REGISTRY`/`_format_table`/`main`)
  re-exporting the symbols tests import-and-call.
- **Dominant effort = monkeypatch retargeting:** ~14 test files patch
  `maintenance.doctor.<helper>`; each must retarget to the domain module the
  check moved into (full map in the design digest / journal). External patches
  (`psycopg2.connect`, `backtest.regime_blended_backtest.run_walkforward`,
  `builtins.open`) are move-safe.
- **Golden gate (safe, sub-second, no DB/parquet):** before AND after every step
  assert the sorted registered check-name set is identical:
  `python3 -c "import sys; sys.path.insert(0,'src'); import maintenance.doctor as d; print(sorted(n for n,_,_ in d._all_checks()))"`.
- Sequence: STEP 1 scaffolding (registry swap, all checks still in doctor.py) →
  STEP 2 pilot `doctor_checks_system` (lowest coupling) → STEPS 3–9 one domain
  per step (broker→data→universe→intraday→strategies→infra→regime) with that
  domain's test retargets → STEP 10 slim the runner. Suite + golden gate each step.
- Smokes after final: `doctor.py --quick` (no ImportError),
  `doctor.py --only alpaca_cli_binary,alpaca_auth --quick --fail-only` (the
  options-archive ExecStartPre names still resolve), `python3 -m
  src.maintenance.doctor --required-only --json` (module entry works).
- Deploy: spawned fresh each ExecStartPre/orchestrator run → NO restart.

### 4. collector.js — two leaf modules (MEDIUM, JOHNBOT RESTART)
2,321 → ~1,760 LOC orchestrator. Most module state is shared/mutable; extract
only the two grep-verified zero-coupling clusters:
- New: `src/pipeline/collector_fills.js` (~300 LOC, provider fill layer —
  stateless leaf: `fillPricesAlpaca/Crypto/FmpHistorical`, symbol normalizers,
  `_httpsGetJson`, `_snapshotToPriceRow`, …) and
  `src/pipeline/collector_freshness.js` (~300 LOC, the **2026-07-15 starvation-
  fix surface** — `_signalsConsumedScope`, `applyResolverEnvelope`,
  `_verifyEquityFreshness`, the dash→dot bridge regex, universe_config-not-
  envelope degraded fallback — **move as literal text, do not edit the logic**).
- Both files MUST live in `src/pipeline/` (relative requires resolve). collector.js
  re-exports every moved symbol (import named at top, leave `module.exports`
  byte-identical). `runIntradaySnapshotPrices` STAYS (touches `_stats`).
- The one non-pure seam: `_verifyEquityFreshness`'s two `notify()` calls →
  add a `setNotify()` wire-once seam (mirrors `setBroadcast`), called at
  collector load. `runDailyCollection`/`runEodRefresh` stay put.
- **Required companion edit:** add
  `delete require.cache[require.resolve('../../src/pipeline/collector_fills')]`
  to `loadCollectorWithStub` in `tests/pipeline/test_fill_prices_alpaca.test.js`.
- Gate: whole `tests/pipeline/` dir + all four `scripts/smoke/collector-*.js` +
  a 37-key public-surface export smoke (in the design digest).
- Deploy: **johnbot restart** (`XDG_RUNTIME_DIR=/run/user/0 systemctl --user
  restart johnbot`) — collector runs inside it. `run_collector_once.js` paths
  pick up new files on next fresh spawn.

### 5. server.js — template + SSE + 7 route modules (MEDIUM, JOHNBOT RESTART)
10,762 → ~180 LOC composition root. **Biggest, safest win first.**
- STEP 1: `dashboard_template.js` — `getDashboardHtml()` verbatim (lines
  2795-10716). **Verified pure constant: 0 unescaped `${}`** → ~7,922 LOC out at
  near-zero risk. Byte-compare `GET /` output.
- STEP 2: `sse.js` — `broadcast` + `sseClients` + events router. server.js must
  `module.exports.broadcast = require('./sse').broadcast` (5 external files
  consume `.broadcast`: bot.js, collector.js×2, budget/enforcer, agent/graph,
  skills-loader) and keep `collector.setBroadcast` / `approvals.init` wiring.
- STEPS 3-9: route groups mirroring the existing `routes_research.js` pattern —
  watchlist (canary) → config → data → regime_status → market → strategies →
  portfolio (last + biggest, owns `warmPortfolioCache`). **Mount every router at
  the same linear position its routes occupy today** (Express matches in order;
  two bare `app.use('/api',…)` mounts exist). Each module imports
  `const { query: dbQuery }` (50 call sites use the alias — silent ReferenceError
  if missed). Every new module must be **side-effect-free at require time** (the
  `collect` step transitively requires the tree via `NO_HTTP_LISTEN=1`).
- server.js keeps: express app, the `NO_HTTP_LISTEN`-gated `app.listen`, all
  require-time wiring, and all 4 exports (`broadcast`, `app`, `httpServer`,
  `shutdown`).
- Gate (no route-level tests exist — helpers only): `npm run test:js` (the glob
  never loads server.js, safe) + the require-only export smoke + a route-count
  smoke (`app._router.stack` length) before/after each step.
- Deploy: **johnbot restart off-market-hours** per step; each step independently
  deployable/rollback-able.

## NO-GO: backtest 4-engine consolidation
`unified/quick/regime_blended/intraday_regime_backtest` are NOT duplicative —
different call sites, `unified_backtest` is the exact code the detached fleet is
mid-run on, backtest numbers feed conviction floors → live trading, and it's
imported by doctor.py + ~13 strategy impls. Fleet-blocked; revisit only with a
golden-output regression harness (which needs compute the box lacks during the
fleet). **Do not touch `src/backtest`.**

## Suggested overall sequence
run_maintenance (LOW, no restart) → alpaca_executor Phase-1 (no restart) →
doctor.py (no restart) → collector.js (1 restart) → server.js STEP 1 template
(1 restart, huge win) → server.js STEPS 2-9 (batched restarts off-hours).
Full detail + line ranges: workflow wf_8673b83b-a29 journal / the design digest.
