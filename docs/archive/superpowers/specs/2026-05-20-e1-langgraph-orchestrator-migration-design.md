# E1 — LangGraph Migration of `pipeline_orchestrator.py` (Design Spec)

**Date:** 2026-05-20
**Status:** Approved for plan writing.
**Authors:** BotJohn (Claude Opus 4.7) + operator.
**Predecessor:** Deferred during 2026-05-20 TradingAgents-imports brainstorming (B3/D1/F3 shipped; E1 split out for separate cycle).

---

## 1. Goal

Replace the imperative Python daily-cycle orchestrator (`src/execution/pipeline_orchestrator.py`, 924 lines, 11 steps) with a single LangGraph.js `StateGraph` that owns daily-cycle control flow. The legacy agent-cycle graph (`src/agent/graph.js`, datajohn → tradejohn → botjohn) is dropped in the same migration — its nodes are redundant with current production steps and its event-driven Discord triggers are stale.

Outcomes targeted (operator-confirmed during brainstorming):

1. **Resumability across johnbot restarts.** PostgresSaver checkpoints persist `DailyCycleState` after every node, so an OOM kill or `systemctl restart` mid-cycle resumes at the last incomplete step rather than re-running the whole cycle or losing state context.
2. **Per-step dashboard observability.** Every step emits `traceBus` events (`start | ok | warn | err | skipped`) that the existing graph-runs SSE feed in the dashboard auto-surfaces. Operators get a live step-by-step view without grepping Discord or logs.
3. **Unified orchestration model.** One LangGraph framework runs the whole 11-step daily cycle and (via subset filtering) the intraday redeploy fragment. No more two parallel orchestrators (Python imperative + JS LangGraph) to reason about.

HITL gates, observability-only shims, and partial migrations were explicitly considered and rejected during brainstorming.

---

## 2. Scope

### 2.1 In scope

- New file: `src/agent/graphs/daily-cycle.js` — the unified LangGraph.
- New file: `src/execution/resolve_script.js` — JS twin of the Python `_resolve_script` helper.
- New file: `src/execution/pipeline_logging.js` — JS twin of the Python notification/DB-logging helpers.
- New file: `src/agent/graphs/recover_inflight.js` — startup probe to resume crashed cycles.
- Modify: `src/engine/cron-schedule.js` — gate-aware 10am dispatch (legacy vs LangGraph).
- Modify: `scripts/redeploy_pipeline.py` — gate-aware inner spawn (legacy vs LangGraph).
- Modify: `src/agent/graphs/index.js` — register `daily-cycle` graph.
- Modify: `bin/run-graph.js` — add `daily-cycle` CLI dispatch.
- Modify: `.env` — add `OPENCLAW_LANGGRAPH_ORCHESTRATOR` gate (default off, flipped during rollout).

### 2.2 Deletions (one week after flag-flip)

- `src/execution/pipeline_orchestrator.py` (924 lines)
- `src/agent/graph.js` (261 lines — legacy agent-cycle graph)

Both stay committed for ≥1 week of cohabitation so the flag is directly reversible.

### 2.3 Out of scope

- **HITL gates** on any node. The new graph has zero `interruptBefore` calls. Future spec if ever wanted.
- **Re-implementing step scripts in JS.** All 11 production scripts (`run_collector_once`, `engine`, `ic_gate_runner`, `trade_handoff_builder`, `regime_blended_sizer_live`, `alpaca_executor`, `alpaca_reconcile`, `send_report`, `pyportfolioopt_shadow`, `daily_health_digest`, `run_sentiment_step`) stay exactly as-is.
- **Dashboard UI work.** Existing graph-runs panel auto-surfaces daily-cycle traces via the shared `traceBus`. A bespoke "Daily Cycle" tab (per-step timeline + columns) is **deferred to a separate Tier-3 ticket**.
- **`scripts/redeploy_pipeline.py` rewrite.** Wrapper logic stays Python; only inner spawn changes.
- **`cron-schedule.js` consolidation.** Other cron jobs (intraday HMM every 5 min, weekly Mastermind, BotJohn maintenance) keep their existing spawn paths.
- **Replacing Redis run-locks** with LangGraph-native concurrency primitives. The Redis `engine:run_lock:<runDate>` lock stays as the concurrency gate.
- **Metrics-table backfill.** `pipeline_runs` already captures durations; no parallel metrics store.
- **Multi-environment deploys.** Single production environment assumed.

---

## 3. Architecture

### 3.1 High level

```
        cron 10am ET (cron-schedule.js)
                  │
                  ▼  (gate flag check)
        ┌─────────┴─────────┐
        ▼ legacy            ▼ new
  pipeline_orchestrator.py  runDailyCycleGraph(input)
                            │
                            ▼
    ┌──────────────────────────────────────────────────────┐
    │ LangGraph StateGraph (src/agent/graphs/daily-cycle.js)│
    │  START → collect → sentiment → signals → ic_gate →    │
    │  handoff → trade → alpaca → reconcile → report →      │
    │  pyportfolioopt_shadow → health → END                 │
    └──────────────────────────────────────────────────────┘
                            │
                            ▼
        PostgresSaver (langgraph schema)
        traceBus → dashboard SSE
        Discord (#pipeline-feed, #data-alerts, #trade-reports)
        Postgres pipeline_runs / pipeline_cycles
```

### 3.2 Entry points

1. **Scheduled cron** (`src/engine/cron-schedule.js`, 10am ET Mon–Fri): gate-aware. Legacy path spawns `pipeline_orchestrator.py`; new path invokes `runDailyCycleGraph(input)` in-process so PostgresSaver/traceBus share the johnbot runtime.
2. **Redeploy** (`scripts/redeploy_pipeline.py`, intraday regime-transition): gate-aware inner spawn. Legacy: `python3 pipeline_orchestrator.py --steps signals,handoff,trade,alpaca,reconcile --reason regime_transition`. New: `node bin/run-graph.js daily-cycle '{"runDate":...,"reason":"regime_transition","requestedSteps":["signals","handoff","trade","alpaca","reconcile"]}'`. Outer Redis cooldown + sentinel + RTH ship-safety gate stays unchanged.
3. **CLI / manual** (`bin/run-graph.js daily-cycle '<json>'`): new dispatch entry. Supports `runDate`, `reason`, optional `requestedSteps`.

### 3.3 State shape

```js
const DailyCycleState = Annotation.Root({
  runDate:         Annotation(),  // 'YYYY-MM-DD'
  runId:           Annotation(),  // 'run-<ts>-<rand>' for traceBus + log correlation
  reason:          Annotation(),  // 'scheduled' | 'manual' | 'regime_transition' | …
  requestedSteps:  Annotation(),  // null = all 11; Set([...]) = subset filter
  completedSteps:  Annotation(),  // [{step, rc, durationMs, startedAt, finishedAt, status}]
  abortedAt:       Annotation(),  // step name where cycle aborted, or null
  lastError:       Annotation(),  // {step, rc, stderrTail} or null
  env:             Annotation(),  // {PIPELINE_DRY_RUN, OPENCLAW_*} — passed to subprocs
});
```

Non-serializable values (Discord notifier fn, pg pool) live in `config.configurable`, same pattern as `graph.js`. State stays under ~5 KB per checkpoint.

### 3.4 Node template

All 11 nodes follow the same pattern; only the `STEP` constant differs:

```js
async function collectNode(state, config) {
  const STEP = 'collect';
  if (skipForSubset(STEP, state)) {
    traceBus.push({ runId: state.runId, node: STEP, status: 'skipped', ts: Date.now() });
    return {};
  }

  const startedAt = Date.now();
  traceBus.push({ runId: state.runId, node: STEP, status: 'start', ts: startedAt });
  await pipelineLog.feedStart(STEP, state.runDate, state.reason);

  const { argv, timeoutSec } = resolveScript(STEP, state.runDate, state.env);
  const { rc, stderrTail, durationMs } = await runSubprocess(argv, { timeoutSec, env: state.env });

  const completion = {
    step: STEP, rc, durationMs, startedAt, finishedAt: Date.now(),
    status: rc === 0 ? 'ok' : (rc === 1 && !strictMode() ? 'warn' : 'failed'),
  };

  if (rc === 0) {
    traceBus.push({ runId: state.runId, node: STEP, status: 'ok', ts: Date.now(), duration: durationMs });
    await pipelineLog.feedEnd(STEP, 'ok', state.runDate, durationMs);
    return { completedSteps: [...(state.completedSteps || []), completion] };
  }

  if (rc === 1 && !strictMode()) {
    traceBus.push({ runId: state.runId, node: STEP, status: 'warn', ts: Date.now(), duration: durationMs });
    await pipelineLog.feedEnd(STEP, 'warn', state.runDate, durationMs);
    return { completedSteps: [...(state.completedSteps || []), completion] };
  }

  // rc=2 always aborts; rc=1 aborts only in strict mode
  traceBus.push({ runId: state.runId, node: STEP, status: 'err', ts: Date.now(), rc, stderrTail });
  await pipelineLog.notifyFailure(STEP, state.runDate, rc, stderrTail);
  const err = new Error(`step ${STEP} exited rc=${rc}`);
  err.step = STEP; err.rc = rc; err.stderrTail = stderrTail;
  throw err;
}
```

### 3.5 Step → script resolution

`src/execution/resolve_script.js` mirrors the Python `_resolve_script` (pipeline_orchestrator.py:461). Search order:

1. `src/pipeline/<step>.py` → `['python3', path, '--date', runDate]`, 600s timeout
2. `src/pipeline/<step>.js` → `['node', path]`, 5400s timeout (collector is long-running; Node scripts handle date internally — no `--date` flag passed)
3. Fallback `src/execution/<step>.py` → `['python3', path, '--date', runDate]`, 300s timeout. Special case: `ic_gate_runner` gets `IC_TIMEOUT_SECONDS + 120s` (default 720s) so the runner's internal poll-timeout fires first.

`PIPELINE_DRY_RUN=1` appends `--dry-run` to every step argv. `PIPELINE_ALPACA_DRY_RUN=1` appends `--dry-run` only to the `alpaca` step. Unknown step names throw.

### 3.6 Graph wiring

```js
const g = new StateGraph(DailyCycleState)
  .addNode('collect',                collectNode)
  .addNode('sentiment',              sentimentNode)
  .addNode('signals',                signalsNode)
  .addNode('ic_gate',                icGateNode)
  .addNode('handoff',                handoffNode)
  .addNode('trade',                  tradeNode)
  .addNode('alpaca',                 alpacaNode)
  .addNode('reconcile',              reconcileNode)
  .addNode('report',                 reportNode)
  .addNode('pyportfolioopt_shadow',  pyportfolioOptShadowNode)
  .addNode('health',                 healthNode)
  .addEdge(START, 'collect')
  .addEdge('collect', 'sentiment')
  .addEdge('sentiment', 'signals')
  .addEdge('signals', 'ic_gate')
  .addEdge('ic_gate', 'handoff')
  .addEdge('handoff', 'trade')
  .addEdge('trade', 'alpaca')
  .addEdge('alpaca', 'reconcile')
  .addEdge('reconcile', 'report')
  .addEdge('report', 'pyportfolioopt_shadow')
  .addEdge('pyportfolioopt_shadow', 'health')
  .addEdge('health', END);
```

Linear DAG. Subset filtering happens *inside* each node (Section 3.4 `skipForSubset` check) rather than via conditional edges — keeps the graph topology simple and lets `traceBus` see `status:'skipped'` events for dashboard rendering.

The `sentiment` node, like in the legacy orchestrator, is gated by `OPENCLAW_SENTIMENT_INGEST=1` — when unset, it self-skips at the top of the node (emits `skipped` to traceBus, no Discord post). This preserves the D1 (2026-05-20) behavior byte-for-byte.

---

## 4. Discord notifications + Postgres logging

### 4.1 Behavioral parity

To an operator watching `#pipeline-feed` / `#data-alerts` / `#trade-reports` after the flag-flip, the message stream is **identical to today**. Same emojis, same channels, same content. Only addition: per-step events in the dashboard SSE feed (additive, non-disruptive). No new Postgres tables — structured per-cycle history lives in PostgresSaver checkpoints (Section 5) instead.

### 4.2 New `src/execution/pipeline_logging.js`

Re-uses the existing `src/channels/discord/notifications.js` for webhook dispatch. **No new Postgres tables.** The Python orchestrator today writes only to `agent_registry` (status updates) and not to a per-step log table — structured per-step history comes from PostgresSaver checkpoints (Section 5), which is already in scope and queryable via the standard `langgraph_checkpoints` table.

Exports:

| Function | Behavior |
|---|---|
| `feedStart(step, runDate, reason)` | Posts `▶️ <step> started for <runDate>` to `#pipeline-feed` |
| `feedEnd(step, status, runDate, durationMs)` | Posts `✅` (ok) or `⚠️` (warn) to `#pipeline-feed` |
| `notifyFailure(step, runDate, rc, stderrTail)` | Posts `❌` to step's failure channel (per `STEP_FAILURE_CHANNEL`) |
| `cycleStart(runDate, reason, runId)` | Posts `🚀 daily cycle started — <runDate> (<reason>)` to `#pipeline-feed` |
| `cycleEnd(runDate, runId, status, abortedAt)` | Posts `✅` (ok) or `❌ aborted at <step>` to `#pipeline-feed` |
| `updateAgentStatus(agentId, status, currentTask?)` | Updates `agent_registry` (matches Python `set_agent_status` at pipeline_orchestrator.py:112) |

`STEP_FAILURE_CHANNEL` and `STEP_AGENTS` maps are **ported verbatim** from `pipeline_orchestrator.py` to JS constants. Tests snapshot-compare both maps against the Python source so they don't drift while both files coexist.

### 4.3 CycleAbort equivalent

The Python orchestrator's `CycleAbort` exception (line 536) maps directly to a node throw in LangGraph: the throw propagates up to the `runDailyCycleGraph` wrapper which catches, records `abortedAt`, and calls `cycleEnd(status='aborted')`. The strict-vs-warn rc=1 distinction is enforced inside each node (Section 3.4) and gated by the existing `OPENCLAW_STRICT_EXIT_CODES=1` env flag — same semantic as today.

---

## 5. Resumability + checkpointing

### 5.1 Checkpoint persistence

`thread_id = "daily-cycle:${runDate}"` — one thread per cycle date. PostgresSaver writes a checkpoint after each node returns. Storage cost: ~few KB × 11 nodes × ~250 trading days/yr ≈ 30 MB/yr. Negligible. Retained indefinitely; no cleanup job specified.

### 5.2 Restart semantics

On johnbot startup, `src/agent/graphs/recover_inflight.js` runs once:

1. Query PostgresSaver for any thread matching `daily-cycle:<today>` (or `<yesterday>` if before 9am ET, to cover overnight crashes).
2. If a thread exists AND its last checkpoint has `next: ['<step>']` (LangGraph's "what's about to run"), resume:
   ```js
   await compiled.invoke(null, { configurable: { thread_id, notify } });
   ```
3. Post `🔄 daily cycle <runDate> resumed at step <step>` to `#pipeline-feed`.
4. If no in-flight thread, skip silently.

The recovery probe is gated by `OPENCLAW_LANGGRAPH_ORCHESTRATOR=1` so it doesn't fire when the legacy orchestrator is in charge.

### 5.3 Idempotency assumptions

Every step must be safe to re-run on restart. Documented:

| Step | Idempotent? | Why |
|---|---|---|
| `collect` | yes | Polygon/FMP/Alpaca collectors upsert on `(ticker, date)` |
| `sentiment` | yes | `ticker_sentiment_daily` has `ON CONFLICT DO UPDATE`; parquet append-only by invariant |
| `signals` | yes | `execution_signals` natural keys + skip-if-exists |
| `ic_gate` | yes | Redis verdict key overwrites |
| `handoff` | yes | Redis `handoff:<runDate>:structured` overwrites |
| `trade` | **mostly** | Sizer deterministic; one edge: subprocess dies AFTER Alpaca submit but BEFORE DB write → restart could re-submit. **Mitigation**: existing Alpaca CLI uses client-side idempotency keys derived from `(strategy_id, ticker, date)`. Already covered. |
| `alpaca` | yes | Reads pending submissions, skips already-acknowledged |
| `reconcile` | yes | Pure read-side |
| `report` | yes | Re-posting same `#trade-reports` summary is operator-visible but harmless |
| `pyportfolioopt_shadow` | yes | Non-fatal shadow sidecar |
| `health` | yes | Digest computation, no writes |

No code changes required for idempotency.

### 5.4 Lock semantics

`runDailyCycleGraph(input)` acquires the existing Redis `engine:run_lock:<runDate>` with `NX EX 7200s` before graph invocation. Releases on terminal state (both success and abort paths). Restart-resume path does **not** re-acquire (lock already held by the crashed process's leaked TTL; will expire naturally if recovery doesn't complete). Concurrent same-date invocations fail fast with `⚠️ cycle already in progress for <runDate>`.

### 5.5 Resume can/can't

- ✅ Resume after johnbot crash / OOM / clean restart mid-step
- ❌ Cannot rewind a *partially completed* step (e.g., `collect` died after 80 of 100 batches → restart re-runs the step from scratch; idempotency handles partial state). LangGraph checkpoints are at node boundaries, not inside subprocesses.
- ❌ Cannot resume across machine moves or DB resets

Matches existing Python orchestrator behavior. No regression.

---

## 6. Rollout

### 6.1 Pre-flight

- All Section 7 unit + smoke tests green
- One successful **dry-run cycle** completes against today's data with `PIPELINE_DRY_RUN=1` while legacy continues running live
- Compare: `pipeline_runs` rows, Discord message text (modulo timestamps), traceBus events for all 11 nodes
- New graph committed; `OPENCLAW_LANGGRAPH_ORCHESTRATOR` stays unset in `.env`

### 6.2 Flag-flip checklist (operator-driven)

1. Pick a non-trading day (weekend / US market holiday)
2. Confirm Mastermind Saturday cohort jobs aren't running
3. Run one full live cycle manually: `node bin/run-graph.js daily-cycle '{"runDate":"<date>","reason":"manual_flip_validation"}'`
4. Inspect `pipeline_runs` rows and Alpaca submissions against an equivalent prior legacy run. Same green steps, same direction/ticker counts, sizes within sizer-determinism tolerance
5. Set `OPENCLAW_LANGGRAPH_ORCHESTRATOR=1` in `.env`
6. `systemctl restart johnbot.service`
7. Operator on standby for the first Monday cycle

### 6.3 Validation period

- **Week 1 after flip**: legacy Python orchestrator stays committed. If anything looks off, flip flag back to 0, restart johnbot, legacy resumes. Zero data loss.
- **End of week 1**: if all 5 daily cycles + any intraday redeploys completed cleanly, delete `src/execution/pipeline_orchestrator.py` and `src/agent/graph.js` in a single cleanup commit. Update CLAUDE.md references.
- **End of week 2**: spec is closed; retention is git history only.

### 6.4 Rollback

If a daily cycle fires under the new graph and fails in a way the legacy orchestrator wouldn't have:

1. Unset (or set to 0) `OPENCLAW_LANGGRAPH_ORCHESTRATOR` in `.env`
2. `systemctl restart johnbot.service`
3. cron resumes spawning `pipeline_orchestrator.py` on next 10am ET tick
4. File an issue against the new graph code; iterate before re-flip

The flag is **directly reversible** at any point in week 1.

---

## 7. Testing strategy

### 7.1 Test pyramid

| Tier | File | Cases | LOC est. |
|---|---|---|---|
| Per-node wrapper | `tests/agent/graphs/daily-cycle.test.js` | 66 (11 steps × 6 cases) | ~1300 |
| Script resolver | `tests/agent/graphs/resolve_script.test.js` | 5 | ~120 |
| Graph wiring | `tests/agent/graphs/daily-cycle-graph.test.js` | 5 | ~250 |
| Pipeline logging | `tests/execution/pipeline_logging.test.js` | 4 | ~180 |
| Recovery probe | `tests/agent/graphs/recover_inflight.test.js` | 3 | ~120 |
| **Total** | | **83 new** | **~1970** |

### 7.2 Per-node wrapper test cases

For each step, six cases run against a mocked `runSubprocess`:

1. **subset-skip**: state has `requestedSteps: new Set(['signals'])`, node is `collect` → asserts no subprocess, `status:'skipped'` traceBus event
2. **success (rc=0)**: `feedStart` + `feedEnd` called with right args; `completedSteps` extended; `status:'ok'` event
3. **warn-and-continue (rc=1, OPENCLAW_STRICT_EXIT_CODES unset)**: no throw; `feedEnd` posts ⚠️; `completedSteps` entry has `status:'warn'`
4. **strict-mode rc=1 → abort**: with `OPENCLAW_STRICT_EXIT_CODES=1` → throw with `err.step` and `err.rc` set
5. **rc=2 → abort always**: throw regardless of strict gate; `notifyFailure` to right channel
6. **subprocess timeout → throw**: `notifyFailure` with rc=124-equivalent

### 7.3 Graph wiring tests

- **Full happy path**: all 11 nodes visited in order, `completedSteps.length === 11`, `status: 'ok'`
- **Subset request**: `requestedSteps: ['signals','handoff','trade']` → exactly 3 nodes ran (others emit `skipped`, not invoked)
- **Mid-cycle abort**: stub `trade` to throw → `abortedAt: 'trade'`, `completedSteps.length === 5`, downstream nodes not invoked
- **Resume from checkpoint**: invoke + abort at `trade`; second invoke with same `thread_id` resumes at `trade`, not `collect`
- **PostgresSaver thread isolation**: two concurrent cycles on different `runDate` → different `thread_id`, no cross-state contamination

### 7.4 Integration tests

- **`pipeline_logging.js`** (4 cases): `feedStart` posts to `#pipeline-feed`; `notifyFailure` routes per `STEP_FAILURE_CHANNEL`; webhook failure is non-fatal (logs warning, doesn't throw); `STEP_FAILURE_CHANNEL` + `STEP_AGENTS` maps snapshot-match the Python source verbatim
- **Recovery probe** (3 cases): no in-flight thread → silent exit; in-flight thread with `next: ['trade']` → resume + 🔄 Discord post; in-flight thread with flag off → skip (legacy owns recovery)

### 7.5 End-to-end smoke (manual, pre-flip)

Not automated. Per Section 6.1:
```bash
OPENCLAW_LANGGRAPH_ORCHESTRATOR=1 PIPELINE_DRY_RUN=1 \
  node bin/run-graph.js daily-cycle '{"runDate":"<today>","reason":"smoke"}'
```
Run twice on same date (idempotency check). Compare Discord output + `pipeline_runs` rows + traceBus events against same-day legacy `pipeline_orchestrator.py --dry-run` baseline.

### 7.6 Regression coverage

Legacy Python orchestrator tests stay green during the 1-week cohabitation:
- `tests/test_pipeline_orchestrator_steps.py`
- `tests/test_pipeline_orchestrator_sentiment_step.py`
- `tests/test_dry_run_dataflow.py`
- Per-step unit tests (test_trade_handoff_builder.py, test_sentiment_storage.py, etc.) untouched

After week-2 cleanup, the three Python orchestrator tests get a port-or-delete pass.

---

## 8. Observability

The new graph emits one observability artifact the legacy path lacks: **per-cycle structured state in Postgres** (`langgraph_checkpoints` rows). An operator can answer "what state was the cycle in at 10:34 today?" with one SQL query against checkpoint state instead of grepping logs.

The existing dashboard graph-runs panel auto-surfaces daily-cycle traces via the shared `traceBus`/registry. No dashboard code changes in this scope.

**Deferred** (separate Tier-3 ticket): bespoke "Daily Cycle" dashboard tab with per-step timeline, durations, and rc columns.

---

## 9. Open questions for plan-writing

Settle these during writing-plans without re-opening this spec:

- Exact JSON schema for `requestedSteps` input — Set on the wire (JSON array) vs Set object inside graph state. Recommend: array on the wire, materialize to `new Set(arr)` at graph entry.
- Whether `runDailyCycleGraph` should fire-and-forget at the cron tick or await completion. Recommend: fire-and-forget (matches existing cron behavior; the cycle's own Discord notifications handle progress reporting).
- ~~Postgres table layout for per-cycle/per-step history.~~ **Resolved during plan-writing**: no new tables. PostgresSaver checkpoints already give us structured cycle/step history queryable via SQL. The Python orchestrator never wrote to `pipeline_runs` either — that table is for data-collection runs, unrelated.
- Whether to back-port the recovery probe to the legacy code path during cohabitation. Recommend: no — the Python orchestrator's existing Redis-checkpoint resume already covers it; back-porting would invert the flag's "default to legacy" semantic.

---

## 10. Resolved questions

Captured during brainstorming for the record:

| Question | Resolution |
|---|---|
| Unification scope | **One graph for everything** (single LangGraph drives daily cycle; legacy `graph.js` deleted) |
| Agent-cycle LLM nodes | **Drop entirely** — datajohn/tradejohn/botjohn nodes redundant with production steps |
| Node implementation language | **JS shells out to existing Python/Node scripts** — no script rewrites |
| Migration strategy | **Flag-gated parallel** (`OPENCLAW_LANGGRAPH_ORCHESTRATOR`), 1-week cohabitation, then delete |
| HITL gates | **Out of scope** |
| Dashboard UI work | **Deferred to separate Tier-3 ticket** |
| Subset filtering mechanism | **In-node check (`skipForSubset`)** rather than conditional edges — simpler topology, `skipped` events visible in traceBus |

---

*End of spec.*
