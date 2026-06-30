# W4 Tier-3 — Promotion-Gate Unification + Pipeline Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** One shared, instrument-class-aware promotion service that BOTH the Discord `/approve-strategy` and the dashboard `/transition` call (closing the W4-F2 ungated path + the manifest/registry drift), plus pipeline-robustness (dedup wiring, tier-a-cap watchdog, processing recovery).

**Architecture:** Extract the C7-hardened gate + transition core from `server.js` into `src/lib/promotion_service.js`; refactor the dashboard route to call it (behavior-preserving + now class-aware); wire `/approve-strategy` through it as a full `→live` transition. Robustness items are independent finisher/cron changes.

**Tech Stack:** Node (CommonJS, `require`), Python (psycopg2), pg, manifest_lock, systemd.

## Global Constraints
- PATH-SCOPED commits ONLY. Never `git add -A`/`.`. Live tree has UNRECOVERABLE WIP (`src/strategies/manifest.json`, `src/strategies/registry.py`, untracked `src/strategies/implementations/S_*`, `scripts/first_wide_fill_watcher.py`). Stage only each task's files explicitly + verify the staged set; never `git reset --hard`/`clean`/blind `checkout`.
- Do NOT push / restart johnbot / apply to live DB / install systemd units — those are operator-gated deploy steps. Tests use injected mocks or rolled-back temp tables; never the live DB.
- `strategy_registry`/`research_candidates` are research/registry state (UPDATE permitted), NOT master data (the NEVER-DELETE invariant covers prices/options/financials/macro/insider/earnings/prices_30m/historical_regimes/crypto_bars_1h + execution_signals/signal_pnl/alpaca_submissions/data_coverage/data_columns).
- Commit footer EVERY commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work from /root/openclaw.
- **Canonical thresholds** (mirror lifecycle.py PROMOTION_THRESHOLDS; existing JS mirror at comprehensive_review.js:267-272): equity {0.5, 0.20}, etp {0.5, 0.20}, option {0.80, 0.30}, crypto {0.50, 0.70}. `max_drawdown` is a FRACTION there; the gate compares MaxDD as PERCENT (DB stores 15.0 = 15%), so expose `max_drawdown_pct = fraction*100`.
- The C7 registry-first invariant is LOAD-BEARING: on gate-fail OR registry-sync-fail, NOTHING is written (no manifest, no registry). Preserve exactly.

---

### Task C1: promotion_service gate primitives (pure-ish, no caller change)

**Files:**
- Create: `src/lib/promotion_service.js`
- Test: `tests/test_promotion_service_gate.js`

**Interfaces — Produces:**
- `getPromotionThreshold(instrumentClass) -> { min_sharpe: number, max_drawdown_pct: number }` (equity fallback).
- `evaluatePromotionGate({ dbQuery, sid, instrumentClass, force }) -> Promise<{ pass, failedGates: string[], sharpe, maxDd, thresholds }>`.

- [ ] **Step 1: Write the failing test** — `tests/test_promotion_service_gate.js`:
```js
'use strict';
const assert = require('assert');
const { getPromotionThreshold, evaluatePromotionGate } = require('../src/lib/promotion_service');

// getPromotionThreshold — per class + fallback
assert.deepStrictEqual(getPromotionThreshold('equity'), { min_sharpe: 0.5, max_drawdown_pct: 20 });
assert.deepStrictEqual(getPromotionThreshold('option'), { min_sharpe: 0.80, max_drawdown_pct: 30 });
assert.deepStrictEqual(getPromotionThreshold('crypto'), { min_sharpe: 0.50, max_drawdown_pct: 70 });
assert.deepStrictEqual(getPromotionThreshold('weird'), { min_sharpe: 0.5, max_drawdown_pct: 20 }); // fallback
assert.deepStrictEqual(getPromotionThreshold(undefined), { min_sharpe: 0.5, max_drawdown_pct: 20 });

// mock dbQuery: first call = strategy_backtest_runs, second = strategy_registry fallback
function mkQuery(runRow, regRow) {
  return async (sql) => {
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_registry/.test(sql))      return { rows: regRow ? [regRow] : [] };
    return { rows: [] };
  };
}
(async () => {
  // canonical present, equity pass
  let g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.9, total_max_dd_pct: 10 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, true); assert.deepStrictEqual(g.failedGates, []);
  // equity sub-floor sharpe
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.4, total_max_dd_pct: 10 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('sharpe'));
  // equity dd fail
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.9, total_max_dd_pct: 25 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('max_dd'));
  // option stricter: sharpe 0.7 passes equity but FAILS option (0.80 floor)
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.7, total_max_dd_pct: 10 }), sid: 'x', instrumentClass: 'option', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('sharpe'));
  // crypto looser DD: 50% dd FAILS equity but PASSES crypto (0.70)
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.6, total_max_dd_pct: 50 }), sid: 'x', instrumentClass: 'crypto', force: false });
  assert.strictEqual(g.pass, true);
  // canonical NaN -> registry fallback used
  g = await evaluatePromotionGate({ dbQuery: mkQuery(null, { backtest_sharpe: 0.4, backtest_max_dd_pct: 5 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('sharpe'));
  // force bypasses everything
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: -5, total_max_dd_pct: 90 }), sid: 'x', instrumentClass: 'equity', force: true });
  assert.strictEqual(g.pass, true);
  console.log('ok test_promotion_service_gate');
})();
```

- [ ] **Step 2: Run → FAIL** (`node tests/test_promotion_service_gate.js` → Cannot find module).

- [ ] **Step 3: Implement** — `src/lib/promotion_service.js` (gate primitives). Port the EXACT read logic from server.js:1560-1588 (canonical `strategy_backtest_runs` primary_window=TRUE latest, then `strategy_registry` fallback on NaN), but class-aware:
```js
'use strict';
// Shared promotion gate + transition core. Single source both the dashboard
// /transition route AND the Discord /approve-strategy call, so the engine's
// trade-gate (strategy_registry.status='approved') can only be reached through
// the same class-aware quality gate (W4-F2 / W4-Tier3). Mirrors lifecycle.py
// PROMOTION_THRESHOLDS — keep in sync.
const PROMOTION_THRESHOLDS = {
  equity: { min_sharpe: 0.5,  max_drawdown_pct: 20 },
  etp:    { min_sharpe: 0.5,  max_drawdown_pct: 20 },
  option: { min_sharpe: 0.80, max_drawdown_pct: 30 },
  crypto: { min_sharpe: 0.50, max_drawdown_pct: 70 },
};
function getPromotionThreshold(instrumentClass) {
  return PROMOTION_THRESHOLDS[instrumentClass] || PROMOTION_THRESHOLDS.equity;
}
async function evaluatePromotionGate({ dbQuery, sid, instrumentClass, force }) {
  const thresholds = getPromotionThreshold(instrumentClass);
  if (force) return { pass: true, failedGates: [], sharpe: NaN, maxDd: NaN, thresholds };
  let sharpe = NaN, maxDd = NaN;
  try {
    const ubt = await dbQuery(
      `SELECT total_sharpe, total_max_dd_pct FROM strategy_backtest_runs
        WHERE strategy_id = $1 AND primary_window = TRUE
        ORDER BY run_at DESC LIMIT 1`, [sid]);
    if (ubt.rows[0]) { sharpe = parseFloat(ubt.rows[0].total_sharpe); maxDd = parseFloat(ubt.rows[0].total_max_dd_pct); }
  } catch (_) {}
  if (isNaN(sharpe) || isNaN(maxDd)) {
    try {
      const sr = (await dbQuery(`SELECT backtest_sharpe, backtest_max_dd_pct FROM strategy_registry WHERE id = $1`, [sid])).rows[0] || {};
      if (isNaN(sharpe)) sharpe = parseFloat(sr.backtest_sharpe);
      if (isNaN(maxDd))  maxDd  = parseFloat(sr.backtest_max_dd_pct);
    } catch (_) {}
  }
  const failedGates = [];
  if (!isNaN(sharpe) && sharpe < thresholds.min_sharpe)    failedGates.push('sharpe');
  if (!isNaN(maxDd)  && maxDd  > thresholds.max_drawdown_pct) failedGates.push('max_dd');
  return { pass: failedGates.length === 0, failedGates, sharpe, maxDd, thresholds };
}
module.exports = { getPromotionThreshold, evaluatePromotionGate, PROMOTION_THRESHOLDS };
```
NOTE: preserve the current gate's permissive behavior on missing data — if BOTH sources are NaN, `failedGates` is empty ⇒ pass (the existing route does NOT block on missing metrics; only blocks on a *present* sub-floor value). Keep that exactly.

- [ ] **Step 4: Run → PASS**. **Step 5: Commit** (path-scoped: the 2 files).

---

### Task C2: transitionStrategy core (registry-first + manifest, C7-hardened)

**Files:**
- Modify: `src/lib/promotion_service.js`
- Test: `tests/test_promotion_service_transition.js`

**Interfaces — Consumes:** evaluatePromotionGate (C1), `./registry_sync.js:syncRegistryStatus`, `../lib/manifest_lock.js:withManifestLock`.
**Produces:** `transitionStrategy({ dbQuery, manifestPath, sid, toState, fromState, force, actor, reason, instrumentClass, eligibleRegimes, gateApplies }) -> Promise<{ ok, fromState, toState, failedGates?, error?, weights_rebuild_triggered, event }>` — does NOT spawn the weights rebuild itself (returns `weights_rebuild_triggered` flag for the caller to act on, so the pure transition is testable). Does NOT write strategy_regime_params / SSE (caller-specific).

- [ ] **Step 1: Write the failing test** — `tests/test_promotion_service_transition.js`. Use a temp manifest file + injected dbQuery; assert: (a) gate-fail (candidate:live, sub-floor, !force) returns `{ok:false, failedGates}` and the manifest file is UNCHANGED + NO registry write happened; (b) registry-sync throw returns `{ok:false, error}` + manifest UNCHANGED (C7 invariant); (c) happy path writes manifest state+history + called registry sync + returns `{ok:true, weights_rebuild_triggered:true}` for candidate→live; (d) force bypasses the gate. (Mirror tests/test_migration style for temp files; inject a fake `syncRegistryStatus` via an opts hook OR a fake dbQuery that records the UPSERT.) Provide the full test in the brief.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `transitionStrategy` — port server.js:1605-1677 logic:
```js
const REGISTRY_STATUS_FOR = { live:'approved', monitoring:'approved', paper:'pending_approval',
  candidate:'pending_approval', staging:'pending_approval', deprecated:'deprecated', archived:'deprecated' };
async function transitionStrategy({ dbQuery, manifestPath, sid, toState, fromState, force, actor, reason, instrumentClass, eligibleRegimes, gateApplies }) {
  const { syncRegistryStatus } = require('../channels/api/registry_sync');
  const { withManifestLock } = require('./manifest_lock');
  // Gate (only when the caller says this transition is gated — i.e. ->live)
  let failedGates = [];
  if (gateApplies && !force) {
    const g = await evaluatePromotionGate({ dbQuery, sid, instrumentClass, force: false });
    if (!g.pass) return { ok:false, failedGates: g.failedGates };
  }
  const now = new Date().toISOString();
  const event = { from_state: fromState, to_state: toState, timestamp: now, actor,
                  reason: reason || `${fromState}->${toState}`, metadata: force ? { override:true } : {} };
  const targetStatus = REGISTRY_STATUS_FOR[toState];
  if (targetStatus) {
    try { await syncRegistryStatus({ dbQuery, sid, targetStatus, actor }); }
    catch (e) { return { ok:false, error:`registry sync refused (nothing written): ${e.message}` }; }
  }
  try {
    await withManifestLock(manifestPath, (m) => {
      const r = (m.strategies || {})[sid];
      if (!r) throw new Error(`strategy ${sid} not in manifest`);
      r.state = toState; r.state_since = now; r.history = r.history || []; r.history.push(event);
      m.updated_at = now; return m;
    }, { actor: `${actor || 'unknown'}` });
  } catch (e) { return { ok:false, error:`manifest write failed (registry already ${targetStatus}; drift badge): ${e.message}` }; }
  // audit (non-fatal)
  try { await dbQuery(`INSERT INTO lifecycle_events (strategy_id, from_state, to_state, actor, reason, metadata) VALUES ($1,$2,$3,$4,$5,$6)`,
    [sid, fromState, toState, actor, event.reason, JSON.stringify(event.metadata)]); } catch (_) {}
  const ACTIVE = new Set(['live','monitoring']);
  const weights_rebuild_triggered = ACTIVE.has(fromState) !== ACTIVE.has(toState);
  return { ok:true, fromState, toState, weights_rebuild_triggered, event };
}
module.exports = { getPromotionThreshold, evaluatePromotionGate, transitionStrategy, PROMOTION_THRESHOLDS, REGISTRY_STATUS_FOR };
```
The eligibleRegimes manifest-delete + regime_params upsert + SSE stay in the dashboard route (C3), NOT here.

- [ ] **Step 4: Run → PASS. Step 5: Commit.**

---

### Task C3: refactor dashboard /transition to call the service (behavior-preserving + class-aware)

**Files:**
- Modify: `src/channels/api/server.js` (the POST /api/strategies/:id/transition handler, ~1556-1781)
- Test: `tests/test_transition_route_refactor.js` (or extend an existing server test if present)

**Interfaces — Consumes:** promotion_service (C1/C2).

- [ ] **Step 1: characterize current behavior** (write a test asserting the gate decision + the registry-first/manifest order for an equity fixture) BEFORE refactoring, so the refactor is provably behavior-preserving. Then:
- [ ] **Step 2: Replace** the inline gate (1560-1596) with `evaluatePromotionGate({ dbQuery, sid, instrumentClass: rec.instrument_class || 'equity', force })` — preserve the 422 `{error, failed_gates, allow_override}` response shape from its `failedGates`. Replace the REGISTRY_STATUS_FOR + syncRegistryStatus + withManifestLock + lifecycle_events + weights-rebuild blocks (1615-1677, 1748-1777) with one `transitionStrategy({ ..., gateApplies: tKey==='candidate:live' })` call; on `{ok:false, failedGates}` → 422; on `{ok:false, error}` → 500; then KEEP the route's eligibleRegimes manifest handling? — NO: the manifest eligibleRegimes delete is inside the lock. Move that concern: pass `eligibleRegimes` is NOT handled by the service; instead, after a successful `transitionStrategy`, run the EXISTING strategy_regime_params upsert block (1686-1746) + SSE broadcast (1760) + spawn the weights rebuild iff `result.weights_rebuild_triggered`. KEEP STRATEGY_VALID_TRANSITIONS validation (1546) + the staging-409 guard (1536) + the eligible_regimes manifest cleanup. **The implementer must preserve the eligible_regimes manifest-delete semantics** — simplest: keep the dashboard's own withManifestLock for the eligible_regimes delete OR thread eligibleRegimes into transitionStrategy. Resolve in the brief; default = keep eligible_regimes delete in the route's regime_params block path and have the service write only state/state_since/history. **Equity behavior must be byte-identical; the only intended delta is class-aware thresholds.**
- [ ] **Step 3: Regression test** — equity fixture: same gate decision + write order as before; crypto/option fixture: class-aware threshold now applies. `node --check server.js`.
- [ ] **Step 4: Commit** (path-scoped: server.js + test). HIGHEST CARE — live route.

---

### Task C4: wire Discord /approve-strategy through the service

**Files:**
- Modify: `src/channels/discord/relay.js` (the `/approve-strategy` case, 114-129)
- Test: `tests/test_approve_strategy_relay.js`

- [ ] **Step 1: test** (mock transitionStrategy + a temp manifest): `/approve-strategy <id>` on a sub-floor candidate → gate-block reply (no writes); `<id> force` → promotes; unknown id → not-found; already-live → "already live"; `deprecated` from-state → refuse. 
- [ ] **Step 2-3: Implement** — replace the bare UPDATE with: read manifest rec for `stratId` (reply 404 if absent); compute `fromState=rec.state`; if `fromState==='live'` reply already-live; if `fromState` not in {candidate, staging, monitoring} reply "cannot approve from <state>"; else `const r = await transitionStrategy({ dbQuery: pgQuery, manifestPath, sid: stratId, toState: 'live', fromState, force: args[2]==='force', actor: 'discord:operator', reason: '/approve-strategy', instrumentClass: rec.instrument_class || 'equity', gateApplies: true })`; reply per `r.ok`/`r.failedGates`/`r.error`. If `r.weights_rebuild_triggered`, spawn the same detached weights rebuild as the route. Confirm relay.js's manifest path + `pgQuery` handle in the brief.
- [ ] **Step 4: Commit** (path-scoped: relay.js + test).

---

### Task C5: wire fingerprint_dedup into the finisher + recovery (T3-2a)

**Files:** Modify `src/agent/curators/saturday_brain_finisher.js` (Phase-6 Tier-A loop ~232) + `src/agent/curators/saturday_brain_recovery.js` (Tier-A loop ~189). Test: `tests/test_finisher_dedup.js` (mock the shell-out).
- [ ] FIRST verify `src/research/fingerprint_dedup.py` exact CLI args (`--slug/--tokens/--regimes`?) + its JSON output keys + that `hunterResult` carries the tokens/regimes (or how to derive). Then add, before `_codeFromQueue`: shell out (mirror paper_expansion_ingestor.js:96-115), parse `{duplicate, reason}`; if duplicate → log + `deduped++` + `continue`; on ANY error/timeout → fail-OPEN (proceed). Add `--no-dedup` finisher flag. Report `deduped` in the run summary. Test: a stubbed dedup=true skips coding; dedup error proceeds.
- [ ] Commit (path-scoped).

---

### Task C6: tier-a-cap per-candidate watchdog (T3-2b)

**Files:** Modify `src/agent/curators/saturday_brain_finisher.js` (the `_codeFromQueue` call site ~244) + `docs/sunday-research-code.service` (cap bump, tracked template). Test: `tests/test_finisher_watchdog.js`.
- [ ] Add `--candidate-timeout-min` (default 20). Wrap `_codeFromQueue` in `Promise.race([call, timeoutThatRejects])`; on timeout catch → `failed++` + log + `continue` (NOT throw — one stuck candidate must not kill the run). Verify `_codeFromQueue` abandonment is safe (idempotent; skips already-manifested) — document. Bump the tracked `docs/sunday-research-code.service` `--tier-a-cap 3`→`6`. Test: a hanging coder promise times out + the loop proceeds to the next candidate.
- [ ] Commit (path-scoped).

---

### Task C7: processing-status recovery script + system_check (T3-2c)

**Files:** Create `scripts/recover_stuck_processing.py` + a `system_check` (`src/system_checks/`). Tests: `tests/test_recover_stuck_processing.py` (temp-table rollback).
- [ ] Idempotent APPLY-only script: `UPDATE research_candidates SET status='pending' WHERE status='processing' AND submitted_at < NOW() - INTERVAL '30 minutes' AND hunter_result_json IS NULL` (env `RESEARCH_CANDIDATE_PROCESSING_TIMEOUT_MIN` default 30); print before/after counts; load .env like `backfill_research_candidate_status.py`. system_check (pipeline tag): WARN if processing count > 5 OR oldest > 24h. Temp-table test: a stuck row (>30m, null json) resets; a fresh processing row + a hunted processing row stay. (Cron install is deploy-gated, not in this task.)
- [ ] Commit (path-scoped).

---

## Self-Review notes
- C1-C4 are the keystone (sequential — C2 needs C1, C3/C4 need C2). C5/C6 both touch saturday_brain_finisher.js → SEQUENTIAL (C6 after C5) to avoid conflicts. C7 is independent.
- The single biggest risk is C3 (live dashboard route). Its regression test (equity byte-identical) is the gate. Final whole-branch review (opus) over the whole range before deploy.
- Threshold drift: the JS PROMOTION_THRESHOLDS mirrors lifecycle.py — add a comment "keep in sync" + (optional) a test that asserts the two match if a shared JSON is introduced later (not now).
