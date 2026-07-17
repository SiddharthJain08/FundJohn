# W4 Tier-3 — Promotion-Gate Unification + Pipeline Robustness Design Spec

- **Date:** 2026-06-30
- **Branch:** feat/intraday-regime-15min-prefetch
- **Status:** Approved (design + scope: operator chose "build keystone + robustness", 2026-06-30) — pending spec→plan→SDD.
- **Workstream:** W4 Tier-3 (deferred backlog from `2026-06-29-w4-research-tier12-design.md` §7). Recon: workflow `wf3pnmteh` (6 Explore agents) + controller live-checks. W4 Tier-1+2 + F2 done & live.

## §0 Context & Verdict
W4-F2 happened because a strategy can reach `strategy_registry.status='approved'` (the engine's trade-gate) WITHOUT the quality gate — there are **two promotion paths that diverged**: the Discord `/approve-strategy {id}` (relay.js:114-129) is a bare ungated `UPDATE strategy_registry SET status='approved' WHERE id=$1` (no gate, no manifest write → also the *drift* cause, e.g. S_vp_macd_index_sensitivity manifest=candidate/registry=approved), while the dashboard `/transition` route (server.js:1488-1781) enforces a Sharpe/DD gate but **hardcodes equity 0.5/20 for ALL instrument classes** (ignores `rec.instrument_class`; lifecycle.py has per-class `PROMOTION_THRESHOLDS`). Recon confirmed **no other ungated paths** to `status='approved'` (the approvals worker is backtest-guarded).

**Live-check finding (logged, NOT in this build):** `strategy_registry.backtest_sharpe` (the column the F2 sheet used) is **stale/divergent** from the canonical `strategy_backtest_runs.total_sharpe` (the gate's source; rebuilt 06-25/26 on the conservative t+1 fill model). They disagree materially (e.g. `low_volatility_us` registry 3.89 vs canonical −7.70; `S_visibility_graph_rsi` registry 3.00 vs canonical 0.47). Under the canonical source 31/62 approved are sub-floor. This is a **metric-source reconciliation + F2 re-validation** workstream (§7) — out of this code build per operator scope. The gate already reads the canonical source first, so the build is unblocked.

## §1 Scope (operator-locked 2026-06-30)
- **BUILD:** T3-1 keystone (shared class-aware promotion service, both paths) + T3-2 robustness (dedup wiring, tier-a-cap watchdog, processing-recovery).
- **DEFER (separate operator/data workstream, §7):** metric-source reconciliation; F2 re-validation against canonical; the "31 sub-floor under canonical" decision.

## §2 T3-1 — Keystone: one shared promotion service (A1 + A2 + A6)
The root cause is two divergent paths; the fix is **one** promotion core both call (don't build a second Discord-only gate — it would re-diverge). New module `src/lib/promotion_service.js`:

### Exports
- `getPromotionThreshold(instrumentClass)` → `{min_sharpe, max_drawdown_pct}`. Mirrors lifecycle.py `PROMOTION_THRESHOLDS` (the existing JS mirror lives in `comprehensive_review.js:267-272`; **units note:** that mirror stores `max_drawdown` as a FRACTION 0.20 — the gate compares MaxDD as PERCENT, so the service exposes `max_drawdown_pct` = fraction×100). Fallback `equity` for unknown class. Single source of truth = lifecycle.py (Python authoritative); the JS constant is a documented mirror "keep in sync."
- `evaluatePromotionGate({dbQuery, sid, instrumentClass, force})` → `{pass, failedGates, sharpe, maxDd, thresholds}`. Reads CANONICAL `strategy_backtest_runs` (primary_window=TRUE, latest run_at) → `strategy_registry` fallback when NaN (the EXACT current logic at server.js:1560-1588), then compares vs the CLASS-AWARE thresholds (replacing the hardcoded `CANDIDATE_TO_LIVE_MIN_SHARPE`/`_MAX_DD_PCT`). `force=true` ⇒ `{pass:true}` regardless (the override path — required so a backtest-weak/live-strong strategy like `S9_dual_momentum` 0.32bt/+22.10live can be approved on purpose).
- `transitionStrategy({dbQuery, manifestPath, sid, toState, fromState, force, actor, reason, eligibleRegimes})` → the C7-hardened CORE shared by both callers: (1) if `${fromState}:${toState}` is `candidate:live` (or `staging:live`/`monitoring`?) run `evaluatePromotionGate` (skip if force) → return `{ok:false, failedGates}` on fail; (2) `REGISTRY_STATUS_FOR[toState]` → `syncRegistryStatus` (registry-first, retry 3x — throw ⇒ return `{ok:false, error}`, manifest unchanged); (3) `withManifestLock` manifest write (state/state_since/history event; handle eligibleRegimes delete+metadata exactly as today); (4) `lifecycle_events` audit (non-fatal); (5) weights rebuild iff `stackChanged` (live/monitoring add/remove). Returns `{ok, fromState, toState, weights_rebuild_triggered}`.

### Caller refactors
- **Dashboard `/transition` (server.js):** replace its inline gate + REGISTRY_STATUS_FOR + syncRegistryStatus + manifest write + lifecycle_events + weights-rebuild blocks with a single `transitionStrategy(...)` call, passing `instrumentClass = rec.instrument_class || 'equity'`. KEEP the dashboard-specific extras IN the route (STRATEGY_VALID_TRANSITIONS validation, the staging→approve 409 guard, the `strategy_regime_params`/`strategy_regime_param_changes` picker upsert, the SSE `broadcast`). The route's externally-observable behavior MUST be unchanged except the now-class-aware thresholds. The error response shape (422 `{error, failed_gates, allow_override}`) is preserved.
- **Discord `/approve-strategy` (relay.js:114-129):** replace the bare UPDATE with: read the manifest rec for `sid` (404 reply if absent); if already `state==='live'` + registry `approved`, reply "already live"; else call `transitionStrategy({... toState:'live', fromState: rec.state, force: args[2]==='force', actor:'discord:operator', reason:'/approve-strategy'})`. On `{ok:false, failedGates}` reply with the gate failure + "re-run with `force` to override"; on `{ok:false, error}` reply the error (registry-sync refused, nothing changed); on ok reply success. This makes Discord do the FULL manifest+registry transition (kills the drift class) through the SAME gate. **Validate the transition is legal** (only `candidate:live`/`staging`-via-approve/`monitoring`/already-live) — refuse `deprecated→live` etc. with a clear reply.

### Tests (TDD)
- `getPromotionThreshold`: per-class returns + equity fallback (pure).
- `evaluatePromotionGate`: injected `dbQuery` mock — canonical-first, registry-fallback-on-NaN, class-aware pass/fail (equity 0.5/20, option 0.80/30, crypto 0.50/70), force bypass. (mirror the existing server gate cases.)
- `transitionStrategy`: injected dbQuery + a temp manifest file — gate-fail returns failedGates w/ NO writes; registry-sync-throw returns error w/ manifest unchanged (the C7 invariant); happy path writes registry+manifest+audit; force bypasses gate.
- relay.js `/approve-strategy`: gate-block path, force path, not-found, already-live, illegal-from-state (mock transitionStrategy).
- **Regression:** the dashboard `/transition` route still behaves identically (existing behavior) — assert the refactor is behavior-preserving for equity (byte-equivalent gate decision) + the class-aware delta for a crypto/option fixture.

## §3 T3-2 — Pipeline robustness (A3, A5, A4)
### T3-2a — wire fingerprint_dedup into the finisher (A3)
`src/research/fingerprint_dedup.py` is production-ready but UNWIRED. In `saturday_brain_finisher.js` Phase-6 Tier-A loop (~line 232, before `_codeFromQueue` at 244): shell out `python3 src/research/fingerprint_dedup.py --slug <sid> --tokens <formula_tokens> --regimes <regimes>` (mirror the shell-out pattern in `paper_expansion_ingestor.js:96-115`), parse JSON `{duplicate, reason, matches}`; if `duplicate` → log + increment a `deduped` counter + `continue` (skip coding); on error/timeout → fail-OPEN (proceed to code, never block the finisher). Add the same to `saturday_brain_recovery.js`'s Tier-A loop for parity. Optional `--no-dedup` finisher flag for force-code. NB: confirm the exact `fingerprint_dedup.py` CLI arg names + that `hunterResult` carries `formula_tokens`/regimes (or derive) before wiring.

### T3-2b — tier-a-cap per-candidate watchdog (A5)
The finisher serial-codes Tier-A under a 4h unit budget; `--tier-a-cap 3` (from default 10) caps throughput vs ~15-20/wk ingest. Wrap each `_codeFromQueue` call in a per-candidate timeout (`Promise.race([call, timeout(candidateTimeoutMs)])`, new `--candidate-timeout-min` default ~20): on timeout → mark that candidate failed (emit gate-decision/log), `continue` to the next (one stuck job no longer dWODKs the whole run). With the watchdog in place, raise the deployed `--tier-a-cap` modestly (e.g. 3→6) in the systemd unit. Confirm `_codeFromQueue` can be safely abandoned mid-flight (no half-written manifest/registry — it's idempotent + skips already-manifested) before relying on the race.

### T3-2c — processing-status recovery (A4, cheap-preventive)
`status='processing'` is set only in `research-orchestrator.js:437` (processQueue, FOR UPDATE SKIP LOCKED). No recovery exists. **0 rows stuck now** (live-checked) so this is purely preventive. Add `scripts/recover_stuck_processing.py` (idempotent): `UPDATE research_candidates SET status='pending' WHERE status='processing' AND submitted_at < NOW() - INTERVAL '30 min' AND hunter_result_json IS NULL` (only genuinely-stuck rows; hunted rows are in-pipeline). + a `system_check` that alerts if `processing` count > 5 or oldest > 24h. Wire as a small cron (hourly). Idempotent; research-state; deploy-gated.

## §4 Testing
- JS: `node --check` + targeted `node tests/*.js`. Python: `python3 -m pytest` (temp-table/rollback for any SQL). Respect VPS 2-core. Live-touching tests (gate reading real DB) use injected mocks, not the live DB.

## §5 Sequencing (path-scoped commits, footer on every commit)
1. C1 — `promotion_service.js` (getPromotionThreshold + evaluatePromotionGate) + unit tests. [pure-ish, no caller change]
2. C2 — add `transitionStrategy` to the service + tests (injected dbQuery + temp manifest).
3. C3 — refactor dashboard `/transition` to call the service (behavior-preserving + class-aware) + regression test.
4. C4 — wire `/approve-strategy` (relay.js) through the service + tests.
5. C5 — T3-2a fingerprint_dedup wiring (finisher + recovery).
6. C6 — T3-2b tier-a-cap watchdog (+ unit-template cap bump as tracked doc).
7. C7 — T3-2c processing-recovery script + system_check.
Each commit path-scoped (live tree has UNRECOVERABLE WIP — never `git add -A`). C3 touches the LIVE dashboard route (I used it for F2 today) → highest care + the regression gate.

## §6 Gated deploy (operator-approved, AFTER final review)
Push; the JS service/route applies on johnbot restart (C3/C4 are in the resident server/bot → restart needed, unlike pure subprocess changes); the dedup/watchdog/finisher changes apply next Sunday; processing-recovery cron + system_check installed (root). Each step explicit-approved. The class-aware gate is a *stricter*-for-option / looser-DD-for-crypto change that only affects FUTURE promotions (no retroactive book change).

## §7 Deferred (NOT this build — logged)
- **Metric-source reconciliation:** refresh `strategy_registry.backtest_sharpe`/`backtest_max_dd_pct` from canonical `strategy_backtest_runs` (or standardize all consumers + sheets on the canonical) so registry/gate/sheets agree. Root fix for "31 sub-floor".
- **F2 re-validation** against canonical: `S_btc_gold_dual_momentum_rotation` (deprecated on registry −0.01 but canonical +0.63 — possible un-deprecate) + `S_visibility_graph_rsi` (KEPT on registry 3.00 but canonical 0.47 — possible deprecate). Reversible; per-row sign-off.
- Ranked/triaged pending_approval operator view; metric backfill for the 2 KEEP + 38 Tier-B.

## §8 Risk controls
- No fix touches the sizer/execution math. T3-1 refactors a LIVE route — strict behavior-preservation + regression test + final review; registry-first C7 invariant preserved (gate-fail/sync-fail ⇒ nothing written). Dedup + watchdog fail-OPEN (never block the finisher). Processing-recovery touches only genuinely-stuck research rows (research-state, not master data). Path-scoped commits; class-aware gate affects only future promotions.
