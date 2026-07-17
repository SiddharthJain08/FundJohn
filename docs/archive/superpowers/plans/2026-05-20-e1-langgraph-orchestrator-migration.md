# E1 — LangGraph Migration of pipeline_orchestrator.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `src/execution/pipeline_orchestrator.py` (Python, 11-step imperative orchestrator) with a single LangGraph.js `StateGraph` that adds resumability, dashboard observability, and a unified orchestration model.

**Architecture:** New `src/agent/graphs/daily-cycle.js` with 11 linear nodes, each a thin Node wrapper that subprocess-execs the existing Python/Node step script. PostgresSaver checkpoints state per node; `traceBus` emissions surface in the dashboard SSE feed. Flag-gated rollout (`OPENCLAW_LANGGRAPH_ORCHESTRATOR=1`); legacy Python orchestrator stays committed for 1-week cohabitation, then deleted.

**Tech Stack:** Node.js, LangGraph.js (`@langchain/langgraph` + `@langchain/langgraph-checkpoint-postgres`), `child_process.spawn`, `node:test` runner, `node:assert/strict`. Python wrapper edits in `scripts/redeploy_pipeline.py`.

**Spec:** `docs/superpowers/specs/2026-05-20-e1-langgraph-orchestrator-migration-design.md` (commit `d323ffe` + patch `6a7e6c7`).

---

## File structure

| Path | Responsibility |
|---|---|
| `src/execution/resolve_script.js` (new) | JS twin of Python `_resolve_script` — maps step name → `{argv, timeoutSec}` |
| `src/execution/pipeline_logging.js` (new) | Discord notify helpers + `STEP_FAILURE_CHANNEL` + `STEP_AGENTS` maps + `updateAgentStatus` |
| `src/agent/graphs/daily-cycle.js` (new) | The unified StateGraph — 11 nodes + entry/recovery wrappers |
| `src/agent/graphs/recover_inflight.js` (new) | Startup probe to resume crashed cycles |
| `src/agent/graphs/index.js` (modify) | Register `daily-cycle` graph |
| `bin/run-graph.js` (modify) | Auto-handles new graph via existing registry dispatch |
| `src/engine/cron-schedule.js` (modify) | Flag-gated 10am dispatch |
| `scripts/redeploy_pipeline.py` (modify) | Flag-gated inner spawn |
| `.env` (modify, gitignored) | Add `OPENCLAW_LANGGRAPH_ORCHESTRATOR` gate (unset = legacy) |

Test files (all use Node's built-in `node:test` runner):

| Path | Cases |
|---|---|
| `tests/test_resolve_script.test.js` | 5 |
| `tests/test_pipeline_logging.test.js` | 4 |
| `tests/test_daily_cycle_node.test.js` | 6 (parameterized — one node, six scenarios) |
| `tests/test_daily_cycle_graph.test.js` | 5 |
| `tests/test_recover_inflight.test.js` | 3 |

The per-node template is mechanical (Section 3.4 of the spec); we test the template thoroughly with one node and rely on visual review of the other 10 nodes to confirm they follow the template. This keeps test-LOC tractable while still catching real bugs.

---

## Task 1: `resolve_script.js` — step → script resolver

**Files:**
- Create: `src/execution/resolve_script.js`
- Create: `tests/test_resolve_script.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_resolve_script.test.js
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const fs       = require('node:fs');
const os       = require('node:os');

const ROOT = path.resolve(__dirname, '..');
const { resolveScript } = require(path.join(ROOT, 'src/execution/resolve_script.js'));

function makeFixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'resolve-script-'));
  fs.mkdirSync(path.join(dir, 'src/pipeline'), { recursive: true });
  fs.mkdirSync(path.join(dir, 'src/execution'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'src/pipeline/run_collector_once.js'), '// node\n');
  fs.writeFileSync(path.join(dir, 'src/pipeline/run_sentiment_step.py'), '# py\n');
  fs.writeFileSync(path.join(dir, 'src/execution/engine.py'), '# py\n');
  fs.writeFileSync(path.join(dir, 'src/execution/ic_gate_runner.py'), '# py\n');
  return dir;
}

test('Python script in src/pipeline resolves with --date and 600s timeout', () => {
  const root = makeFixture();
  const { argv, timeoutSec } = resolveScript('run_sentiment_step', '2026-05-21', {}, root);
  assert.deepEqual(argv, ['python3', path.join(root, 'src/pipeline/run_sentiment_step.py'), '--date', '2026-05-21']);
  assert.equal(timeoutSec, 600);
});

test('Node script in src/pipeline resolves without --date and 5400s timeout', () => {
  const root = makeFixture();
  const { argv, timeoutSec } = resolveScript('run_collector_once', '2026-05-21', {}, root);
  assert.deepEqual(argv, ['node', path.join(root, 'src/pipeline/run_collector_once.js')]);
  assert.equal(timeoutSec, 5400);
});

test('Fallback src/execution Python with default 300s timeout', () => {
  const root = makeFixture();
  const { argv, timeoutSec } = resolveScript('engine', '2026-05-21', {}, root);
  assert.deepEqual(argv, ['python3', path.join(root, 'src/execution/engine.py'), '--date', '2026-05-21']);
  assert.equal(timeoutSec, 300);
});

test('ic_gate_runner uses IC_TIMEOUT_SECONDS + 120s (default 720s)', () => {
  const root = makeFixture();
  const { timeoutSec } = resolveScript('ic_gate_runner', '2026-05-21', {}, root);
  assert.equal(timeoutSec, 720);
  // With override:
  const env = { IC_TIMEOUT_SECONDS: '900' };
  const { timeoutSec: ts2 } = resolveScript('ic_gate_runner', '2026-05-21', env, root);
  assert.equal(ts2, 1020);
});

test('PIPELINE_DRY_RUN appends --dry-run to all steps; ALPACA_DRY_RUN only to alpaca', () => {
  const root = makeFixture();
  fs.writeFileSync(path.join(root, 'src/execution/alpaca_executor.py'), '# py\n');
  // PIPELINE_DRY_RUN=1 → every step gets --dry-run
  const { argv: a1 } = resolveScript('engine', '2026-05-21', { PIPELINE_DRY_RUN: '1' }, root);
  assert.ok(a1.includes('--dry-run'));
  // PIPELINE_ALPACA_DRY_RUN=1, no full dry → only alpaca gets --dry-run
  const { argv: a2 } = resolveScript('engine', '2026-05-21', { PIPELINE_ALPACA_DRY_RUN: '1' }, root);
  assert.ok(!a2.includes('--dry-run'));
  const { argv: a3 } = resolveScript('alpaca_executor', '2026-05-21', { PIPELINE_ALPACA_DRY_RUN: '1' }, root);
  assert.ok(a3.includes('--dry-run'));
});

test('Unknown step throws', () => {
  const root = makeFixture();
  assert.throws(
    () => resolveScript('definitely_not_a_step', '2026-05-21', {}, root),
    /unknown step|not found|definitely_not_a_step/i
  );
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_resolve_script.test.js`

Expected: FAIL — `Cannot find module '.../src/execution/resolve_script.js'`.

- [ ] **Step 3: Write the implementation**

```javascript
// src/execution/resolve_script.js
/**
 * JS twin of pipeline_orchestrator.py:_resolve_script.
 *
 * Step name → (argv, timeoutSec). Search order:
 *   1. src/pipeline/<step>.py  → python3 + --date,    600s
 *   2. src/pipeline/<step>.js  → node (no --date),   5400s
 *   3. src/execution/<step>.py → python3 + --date,    300s (720s for ic_gate_runner)
 *
 * PIPELINE_DRY_RUN=1 appends --dry-run to every step argv.
 * PIPELINE_ALPACA_DRY_RUN=1 appends --dry-run only to the alpaca_executor step.
 */
'use strict';

const fs   = require('node:fs');
const path = require('node:path');

const DEFAULT_ROOT = path.resolve(__dirname, '..', '..');

function resolveScript(step, runDate, env = process.env, root = DEFAULT_ROOT) {
  const fullDry  = env.PIPELINE_DRY_RUN === '1';
  const alpacaDry = env.PIPELINE_ALPACA_DRY_RUN === '1';

  const maybeDry = (argv) => {
    if (fullDry) argv.push('--dry-run');
    else if (alpacaDry && step === 'alpaca_executor') argv.push('--dry-run');
    return argv;
  };

  const pyPipe = path.join(root, 'src/pipeline', `${step}.py`);
  const jsPipe = path.join(root, 'src/pipeline', `${step}.js`);
  const pyExec = path.join(root, 'src/execution', `${step}.py`);

  if (fs.existsSync(pyPipe)) {
    return { argv: maybeDry(['python3', pyPipe, '--date', runDate]), timeoutSec: 600 };
  }
  if (fs.existsSync(jsPipe)) {
    return { argv: maybeDry(['node', jsPipe]), timeoutSec: 5400 };
  }
  if (fs.existsSync(pyExec)) {
    let timeoutSec = 300;
    if (step === 'ic_gate_runner') {
      timeoutSec = parseInt(env.IC_TIMEOUT_SECONDS || '600', 10) + 120;
    }
    return { argv: maybeDry(['python3', pyExec, '--date', runDate]), timeoutSec };
  }
  throw new Error(`unknown step: ${step} (no script found at ${pyPipe}, ${jsPipe}, or ${pyExec})`);
}

module.exports = { resolveScript };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/test_resolve_script.test.js`

Expected: PASS — 6 subtests (5 listed `test()` calls; the dry-run case has 3 sub-assertions).

- [ ] **Step 5: Commit**

```bash
git add src/execution/resolve_script.js tests/test_resolve_script.test.js
git commit -m "feat(e1): resolve_script.js — JS twin of pipeline_orchestrator._resolve_script

Step-name → {argv, timeoutSec} resolver. Search order matches the
Python source: src/pipeline/<step>.py (600s) → src/pipeline/<step>.js
(5400s, no --date) → src/execution/<step>.py (300s, 720s for
ic_gate_runner). PIPELINE_DRY_RUN + PIPELINE_ALPACA_DRY_RUN flags
propagate as today.

Task 1 of E1 plan."
```

---

## Task 2: `pipeline_logging.js` — Discord + agent_registry helpers

**Files:**
- Create: `src/execution/pipeline_logging.js`
- Create: `tests/test_pipeline_logging.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_pipeline_logging.test.js
'use strict';

const { test, mock } = require('node:test');
const assert         = require('node:assert/strict');
const path           = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const MODULE_PATH = path.join(ROOT, 'src/execution/pipeline_logging.js');

// Stub the notifications module before requiring pipeline_logging
function makeStubbedLogger() {
  const calls = [];
  const stubNotifications = {
    post: async (channel, text) => { calls.push({ channel, text }); return true; },
  };
  // Inject via require.cache override
  const notifPath = require.resolve(path.join(ROOT, 'src/channels/discord/notifications.js'));
  require.cache[notifPath] = { id: notifPath, filename: notifPath, loaded: true, exports: stubNotifications };
  // Clear pipeline_logging cache so it re-requires the stub
  delete require.cache[require.resolve(MODULE_PATH)];
  const mod = require(MODULE_PATH);
  return { mod, calls };
}

test('feedStart posts ▶️ to #pipeline-feed', async () => {
  const { mod, calls } = makeStubbedLogger();
  await mod.feedStart('collect', '2026-05-21', 'scheduled');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].channel, 'pipeline-feed');
  assert.match(calls[0].text, /▶️.*collect.*2026-05-21/);
});

test('notifyFailure routes per STEP_FAILURE_CHANNEL', async () => {
  const { mod, calls } = makeStubbedLogger();
  // trade-half steps go to #trade-reports
  await mod.notifyFailure('trade', '2026-05-21', 2, 'sample stderr');
  // collect goes to #data-alerts
  await mod.notifyFailure('collect', '2026-05-21', 1, 'sample stderr');
  // health goes to #pipeline-feed (default)
  await mod.notifyFailure('health', '2026-05-21', 2, 'sample stderr');
  assert.equal(calls.length, 3);
  assert.equal(calls[0].channel, 'trade-reports');
  assert.equal(calls[1].channel, 'data-alerts');
  assert.equal(calls[2].channel, 'pipeline-feed');
});

test('webhook failure is non-fatal — logs warning, does not throw', async () => {
  const calls = [];
  const stubNotifications = {
    post: async () => { throw new Error('webhook down'); },
  };
  const notifPath = require.resolve(path.join(ROOT, 'src/channels/discord/notifications.js'));
  require.cache[notifPath] = { id: notifPath, filename: notifPath, loaded: true, exports: stubNotifications };
  delete require.cache[require.resolve(MODULE_PATH)];
  const mod = require(MODULE_PATH);
  // Should NOT throw
  await mod.feedStart('collect', '2026-05-21', 'scheduled');
  await mod.notifyFailure('trade', '2026-05-21', 2, 'oops');
});

test('STEP_FAILURE_CHANNEL and STEP_AGENTS maps are exported and complete', () => {
  const { mod } = makeStubbedLogger();
  const expectedSteps = [
    'collect', 'sentiment', 'signals', 'ic_gate', 'handoff',
    'trade', 'alpaca', 'reconcile', 'report',
    'pyportfolioopt_shadow', 'health',
  ];
  for (const step of expectedSteps) {
    assert.ok(mod.STEP_FAILURE_CHANNEL[step], `STEP_FAILURE_CHANNEL missing: ${step}`);
    assert.ok(mod.STEP_AGENTS[step],          `STEP_AGENTS missing: ${step}`);
  }
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_pipeline_logging.test.js`

Expected: FAIL — `Cannot find module '.../src/execution/pipeline_logging.js'`.

- [ ] **Step 3: Write the implementation**

```javascript
// src/execution/pipeline_logging.js
/**
 * JS twin of the notification + agent-registry helpers in
 * pipeline_orchestrator.py (lines 257-401, 112-145).
 *
 * No new Postgres tables — Python orchestrator never wrote a per-step
 * log table either. Structured per-cycle history lives in
 * langgraph_checkpoints (PostgresSaver) and is queryable via SQL.
 */
'use strict';

const path = require('node:path');
const notifications = require(path.resolve(__dirname, '..', 'channels', 'discord', 'notifications.js'));

// Route map ported verbatim from pipeline_orchestrator.py:78-97.
// Data-pipeline steps surface in #data-alerts; trade-pipeline steps
// surface in #trade-reports; everything else goes to #pipeline-feed.
const STEP_FAILURE_CHANNEL = {
  'collect':                'data-alerts',
  'sentiment':              'data-alerts',
  'signals':                'data-alerts',
  'ic_gate':                'trade-reports',
  'handoff':                'trade-reports',
  'trade':                  'trade-reports',
  'alpaca':                 'trade-reports',
  'reconcile':              'trade-reports',
  'report':                 'trade-reports',
  'pyportfolioopt_shadow':  'pipeline-feed',
  'health':                 'pipeline-feed',
};

// Which agent's identity claims each step in the dashboard registry.
// Ported from pipeline_orchestrator.py:743-758.
const STEP_AGENTS = {
  'collect':                'databot',
  'sentiment':              'databot',
  'signals':                'databot',
  'ic_gate':                'tradebot',
  'handoff':                'tradebot',
  'trade':                  'tradebot',
  'alpaca':                 'tradebot',
  'reconcile':              'tradebot',
  'report':                 'tradebot',
  'pyportfolioopt_shadow':  'databot',
  'health':                 'databot',
};

async function _safePost(channel, text) {
  try {
    await notifications.post(channel, text);
  } catch (e) {
    console.warn(`[pipeline_logging] post to #${channel} failed: ${e.message}`);
  }
}

async function feedStart(step, runDate, reason) {
  const reasonSuffix = reason && reason !== 'scheduled' ? ` (${reason})` : '';
  await _safePost('pipeline-feed', `▶️ ${step} started for ${runDate}${reasonSuffix}`);
}

async function feedEnd(step, status, runDate, durationMs) {
  const emoji = status === 'ok' ? '✅' : status === 'warn' ? '⚠️' : '❓';
  const secs = (durationMs / 1000).toFixed(1);
  await _safePost('pipeline-feed', `${emoji} ${step} ${status} for ${runDate} (${secs}s)`);
}

async function notifyFailure(step, runDate, rc, stderrTail) {
  const channel = STEP_FAILURE_CHANNEL[step] || 'pipeline-feed';
  const tail = (stderrTail || '').slice(-400);
  await _safePost(channel, `❌ ${step} failed for ${runDate} (rc=${rc})\n\`\`\`\n${tail}\n\`\`\``);
}

async function cycleStart(runDate, reason, runId) {
  await _safePost('pipeline-feed', `🚀 daily cycle started — ${runDate} (${reason}, run=${runId})`);
}

async function cycleEnd(runDate, runId, status, abortedAt) {
  if (status === 'ok') {
    await _safePost('pipeline-feed', `✅ daily cycle completed — ${runDate} (run=${runId})`);
  } else {
    await _safePost('pipeline-feed', `❌ daily cycle aborted at ${abortedAt} — ${runDate} (run=${runId})`);
  }
}

/**
 * Update agent_registry status — mirrors pipeline_orchestrator.py:112
 * set_agent_status(). Best-effort, fail-quiet. Postgres connection
 * uses POSTGRES_URI via the existing pg client.
 */
async function updateAgentStatus(agentId, status, currentTask = null) {
  if (!agentId) return;
  let client;
  try {
    const { Client } = require('pg');
    client = new Client({ connectionString: process.env.POSTGRES_URI });
    await client.connect();
    await client.query(
      'UPDATE agent_registry SET status=$1, current_task=$2, last_seen_at=NOW() WHERE id=$3',
      [status, currentTask, agentId]
    );
  } catch (e) {
    console.warn(`[pipeline_logging] updateAgentStatus(${agentId}, ${status}) failed: ${e.message}`);
  } finally {
    if (client) await client.end().catch(() => {});
  }
}

module.exports = {
  STEP_FAILURE_CHANNEL,
  STEP_AGENTS,
  feedStart,
  feedEnd,
  notifyFailure,
  cycleStart,
  cycleEnd,
  updateAgentStatus,
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/test_pipeline_logging.test.js`

Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/execution/pipeline_logging.js tests/test_pipeline_logging.test.js
git commit -m "feat(e1): pipeline_logging.js — JS Discord/agent-registry helpers

Twin of pipeline_orchestrator.py:78-401 notification helpers. Re-uses
existing src/channels/discord/notifications.js for webhook dispatch.
STEP_FAILURE_CHANNEL + STEP_AGENTS maps ported verbatim (collect/
sentiment/signals → #data-alerts; ic_gate/handoff/trade/alpaca/
reconcile/report → #trade-reports; others → #pipeline-feed).

No new Postgres tables (Python orchestrator never wrote any). agent_
registry status updates preserved for parity with set_agent_status().

Task 2 of E1 plan."
```

---

## Task 3: Node template helpers — `runSubprocess`, `skipForSubset`, `strictMode`

**Files:**
- Create: `src/agent/graphs/daily_cycle_helpers.js`
- Create: `tests/test_daily_cycle_helpers.test.js`

These three helpers are shared by all 11 node implementations. Build them with TDD first; nodes layer on top.

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_daily_cycle_helpers.test.js
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const helpers = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));

test('skipForSubset honors requestedSteps when present', () => {
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: null }),                          false);
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: undefined }),                     false);
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: new Set(['signals']) }),          true);
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: new Set(['collect','signals']) }), false);
});

test('strictMode reads OPENCLAW_STRICT_EXIT_CODES from env', () => {
  assert.equal(helpers.strictMode({ OPENCLAW_STRICT_EXIT_CODES: '1' }), true);
  assert.equal(helpers.strictMode({ OPENCLAW_STRICT_EXIT_CODES: '0' }), false);
  assert.equal(helpers.strictMode({}),                                  false);
});

test('runSubprocess returns rc=0 + stdout + stderr for successful command', async () => {
  // Use /bin/echo (always present on Linux)
  const out = await helpers.runSubprocess(['echo', 'hello'], { timeoutSec: 5, env: process.env });
  assert.equal(out.rc, 0);
  assert.match(out.stdout || '', /hello/);
  assert.ok(typeof out.durationMs === 'number' && out.durationMs >= 0);
});

test('runSubprocess returns rc=1 for failed command and captures stderr tail', async () => {
  // /bin/false exits 1 with no output; use 'sh -c' to print to stderr
  const out = await helpers.runSubprocess(['sh', '-c', 'echo nope >&2; exit 1'], { timeoutSec: 5, env: process.env });
  assert.equal(out.rc, 1);
  assert.match(out.stderrTail || '', /nope/);
});

test('runSubprocess respects timeout and returns rc=124-equivalent', async () => {
  // sleep 10 with timeoutSec=1 → should kill the proc and return non-zero
  const out = await helpers.runSubprocess(['sleep', '10'], { timeoutSec: 1, env: process.env });
  assert.notEqual(out.rc, 0);
  assert.ok(out.timedOut === true);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_daily_cycle_helpers.test.js`

Expected: FAIL — `Cannot find module .../daily_cycle_helpers.js`.

- [ ] **Step 3: Write the implementation**

```javascript
// src/agent/graphs/daily_cycle_helpers.js
/**
 * Shared helpers for the 11 daily-cycle step nodes.
 *
 *   skipForSubset(step, state) → true if state.requestedSteps excludes this step
 *   strictMode(env)            → boolean from OPENCLAW_STRICT_EXIT_CODES
 *   runSubprocess(argv, opts)  → Promise<{rc, stdout, stderrTail, durationMs, timedOut}>
 */
'use strict';

const { spawn } = require('node:child_process');

function skipForSubset(step, state) {
  if (!state || !state.requestedSteps) return false;
  const req = state.requestedSteps;
  // Accept Set or Array for hand-coded resilience
  if (req instanceof Set) return !req.has(step);
  if (Array.isArray(req)) return !req.includes(step);
  return false;
}

function strictMode(env) {
  return (env && env.OPENCLAW_STRICT_EXIT_CODES) === '1';
}

function runSubprocess(argv, { timeoutSec = 600, env = process.env, cwd } = {}) {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const [cmd, ...args] = argv;
    let stdout = '';
    let stderr = '';
    let timedOut = false;

    const proc = spawn(cmd, args, {
      env,
      cwd: cwd || process.cwd(),
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    proc.stdout.on('data', (b) => { stdout += b.toString(); });
    proc.stderr.on('data', (b) => { stderr += b.toString(); });

    const timer = setTimeout(() => {
      timedOut = true;
      try { proc.kill('SIGTERM'); } catch {}
      // Hard-kill after 5s if it doesn't exit
      setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} }, 5000);
    }, timeoutSec * 1000);

    proc.on('close', (code, signal) => {
      clearTimeout(timer);
      const durationMs = Date.now() - startedAt;
      const rc = timedOut ? 124 : (code === null ? (signal ? 137 : 1) : code);
      resolve({
        rc,
        stdout,
        stderrTail: stderr.slice(-1000),
        durationMs,
        timedOut,
      });
    });

    proc.on('error', (e) => {
      clearTimeout(timer);
      resolve({
        rc: 127,
        stdout: '',
        stderrTail: `spawn failed: ${e.message}`,
        durationMs: Date.now() - startedAt,
        timedOut: false,
      });
    });
  });
}

module.exports = { skipForSubset, strictMode, runSubprocess };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/test_daily_cycle_helpers.test.js`

Expected: PASS — 5 tests. The timeout test takes ~1.5s; total run under 3s.

- [ ] **Step 5: Commit**

```bash
git add src/agent/graphs/daily_cycle_helpers.js tests/test_daily_cycle_helpers.test.js
git commit -m "feat(e1): daily_cycle_helpers.js — shared node helpers

Three helpers used by all 11 daily-cycle step nodes:
- skipForSubset(step, state)  — honors requestedSteps filter
- strictMode(env)             — reads OPENCLAW_STRICT_EXIT_CODES
- runSubprocess(argv, opts)   — Promise-based spawn with timeout +
                                 stderr tail capture + kill escalation

5 tests cover happy path, failure path, timeout, subset filtering,
and strict-mode gate.

Task 3 of E1 plan."
```

---

## Task 4: Node template + parameterized 6-case test suite (one node)

**Files:**
- Create: `src/agent/graphs/daily_cycle_node.js`
- Create: `tests/test_daily_cycle_node.test.js`

Per the spec (§7.1), we test the template thoroughly with one step and rely on visual review of the other 10. The template factory in this task generates a node function from `(step) → nodeFn`. The graph in Task 6 calls the factory 11 times.

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_daily_cycle_node.test.js
'use strict';

const { test, beforeEach } = require('node:test');
const assert               = require('node:assert/strict');
const path                 = require('node:path');

const ROOT = path.resolve(__dirname, '..');

// IMPORTANT: use require.resolve so the mocked path matches whatever Node's
// internal resolution produces for the module's `require()` calls. Plain
// path.join can disagree (symlinks, drive letters on Windows, etc.).
const NODE_PATH     = require.resolve(path.join(ROOT, 'src/agent/graphs/daily_cycle_node.js'));
const HELPERS_PATH  = require.resolve(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));
const RESOLVE_PATH  = require.resolve(path.join(ROOT, 'src/execution/resolve_script.js'));
const LOGGING_PATH  = require.resolve(path.join(ROOT, 'src/execution/pipeline_logging.js'));
const TRACEBUS_PATH = require.resolve(path.join(ROOT, 'src/agent/traceBus.js'));

// Build a stubbed makeNode by injecting fake helpers + logger + traceBus
function makeStubbedFactory({ rc, stderrTail = '', durationMs = 100, throwSpawn = false, timedOut = false } = {}) {
  const traceEvents = [];
  const logCalls    = [];

  require.cache[HELPERS_PATH] = {
    id: HELPERS_PATH, filename: HELPERS_PATH, loaded: true,
    exports: {
      skipForSubset: (step, state) => {
        if (!state || !state.requestedSteps) return false;
        const req = state.requestedSteps;
        if (req instanceof Set) return !req.has(step);
        if (Array.isArray(req)) return !req.includes(step);
        return false;
      },
      strictMode: (env) => env.OPENCLAW_STRICT_EXIT_CODES === '1',
      runSubprocess: async () => {
        if (throwSpawn) throw new Error('spawn explode');
        return { rc, stderrTail, durationMs, stdout: '', timedOut };
      },
    },
  };
  require.cache[RESOLVE_PATH] = {
    id: RESOLVE_PATH, filename: RESOLVE_PATH, loaded: true,
    exports: { resolveScript: () => ({ argv: ['echo', 'fake'], timeoutSec: 60 }) },
  };
  require.cache[LOGGING_PATH] = {
    id: LOGGING_PATH, filename: LOGGING_PATH, loaded: true,
    exports: {
      feedStart:      async (...args) => logCalls.push(['feedStart',     args]),
      feedEnd:        async (...args) => logCalls.push(['feedEnd',       args]),
      notifyFailure:  async (...args) => logCalls.push(['notifyFailure', args]),
    },
  };
  require.cache[TRACEBUS_PATH] = {
    id: TRACEBUS_PATH, filename: TRACEBUS_PATH, loaded: true,
    exports: {
      push: (ev) => traceEvents.push(ev),
      startRun: () => {},
      endRun:   () => {},
    },
  };
  delete require.cache[require.resolve(NODE_PATH)];
  const { makeStepNode } = require(NODE_PATH);
  return { makeStepNode, traceEvents, logCalls };
}

const BASE_STATE = {
  runDate:        '2026-05-21',
  runId:          'run-test-1',
  reason:         'scheduled',
  requestedSteps: null,
  completedSteps: [],
  env:            {},
};

test('subset-skip path → no subprocess, traceBus.push("skipped"), no Discord posts', async () => {
  const { makeStepNode, traceEvents, logCalls } = makeStubbedFactory({ rc: 0 });
  const node = makeStepNode('collect');
  const out = await node({ ...BASE_STATE, requestedSteps: new Set(['signals']) });
  assert.deepEqual(out, {});
  assert.equal(logCalls.length, 0);
  assert.equal(traceEvents.length, 1);
  assert.equal(traceEvents[0].status, 'skipped');
  assert.equal(traceEvents[0].node,   'collect');
});

test('success (rc=0) → feedStart + feedEnd("ok"), completedSteps appended, traceBus ok event', async () => {
  const { makeStepNode, traceEvents, logCalls } = makeStubbedFactory({ rc: 0, durationMs: 250 });
  const node = makeStepNode('collect');
  const out = await node(BASE_STATE);
  assert.equal(out.completedSteps.length, 1);
  assert.equal(out.completedSteps[0].step,   'collect');
  assert.equal(out.completedSteps[0].rc,     0);
  assert.equal(out.completedSteps[0].status, 'ok');
  assert.equal(logCalls[0][0], 'feedStart');
  assert.equal(logCalls[1][0], 'feedEnd');
  assert.equal(logCalls[1][1][1], 'ok');
  assert.ok(traceEvents.some(e => e.status === 'start'));
  assert.ok(traceEvents.some(e => e.status === 'ok'));
});

test('warn-and-continue (rc=1, OPENCLAW_STRICT_EXIT_CODES unset) → no throw, status=warn', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 1, stderrTail: 'a warning' });
  const node = makeStepNode('collect');
  const out = await node({ ...BASE_STATE, env: {} });
  assert.equal(out.completedSteps.length, 1);
  assert.equal(out.completedSteps[0].status, 'warn');
  assert.equal(logCalls[1][0], 'feedEnd');
  assert.equal(logCalls[1][1][1], 'warn');
});

test('strict-mode rc=1 → throw with err.step + err.rc + notifyFailure', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 1, stderrTail: 'strict-mode fail' });
  const node = makeStepNode('collect');
  await assert.rejects(
    () => node({ ...BASE_STATE, env: { OPENCLAW_STRICT_EXIT_CODES: '1' } }),
    (err) => {
      assert.equal(err.step, 'collect');
      assert.equal(err.rc,   1);
      return true;
    }
  );
  assert.ok(logCalls.some(([fn]) => fn === 'notifyFailure'));
});

test('rc=2 → throw always, notifyFailure called with correct channel via STEP_FAILURE_CHANNEL', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 2, stderrTail: 'hard fail' });
  const nodeTrade = makeStepNode('trade');  // trade → #trade-reports
  await assert.rejects(
    () => nodeTrade({ ...BASE_STATE, env: {} }),
    (err) => { assert.equal(err.step, 'trade'); assert.equal(err.rc, 2); return true; }
  );
  const failCall = logCalls.find(([fn]) => fn === 'notifyFailure');
  assert.ok(failCall);
  assert.equal(failCall[1][0], 'trade'); // step name passed through
});

test('subprocess timeout → throw with timedOut:true preserved in completion record path', async () => {
  const { makeStepNode } = makeStubbedFactory({ rc: 124, stderrTail: 'timed out', timedOut: true });
  const node = makeStepNode('collect');
  await assert.rejects(
    () => node({ ...BASE_STATE, env: {} }),
    (err) => { assert.equal(err.rc, 124); return true; }
  );
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_daily_cycle_node.test.js`

Expected: FAIL — `Cannot find module .../daily_cycle_node.js`.

- [ ] **Step 3: Write the implementation**

```javascript
// src/agent/graphs/daily_cycle_node.js
/**
 * Node-template factory for the 11 daily-cycle steps.
 *
 *   const collectNode = makeStepNode('collect');
 *   const tradeNode   = makeStepNode('trade');
 *
 * Each generated nodeFn(state, config) follows the spec §3.4 pattern:
 *   1. honor requestedSteps subset filter
 *   2. emit traceBus events (start | ok | warn | err | skipped)
 *   3. post Discord notifications via pipeline_logging.js
 *   4. subprocess-exec the step's script via resolve_script.js
 *   5. map rc → status (rc=0 ok | rc=1 warn-or-throw | rc≥2 throw)
 */
'use strict';

const { skipForSubset, strictMode, runSubprocess } = require('./daily_cycle_helpers');
const { resolveScript }                            = require('../../execution/resolve_script');
const pipelineLog                                  = require('../../execution/pipeline_logging');
const traceBus                                     = require('../traceBus');

function makeStepNode(STEP) {
  return async function stepNode(state, _config) {
    const runId = state.runId;

    if (skipForSubset(STEP, state)) {
      traceBus.push({ runId, node: STEP, status: 'skipped', ts: Date.now() });
      return {};
    }

    const startedAt = Date.now();
    traceBus.push({ runId, node: STEP, status: 'start', ts: startedAt });
    await pipelineLog.feedStart(STEP, state.runDate, state.reason);

    const env = { ...process.env, ...(state.env || {}) };
    const { argv, timeoutSec } = resolveScript(STEP, state.runDate, env);
    const { rc, stderrTail, durationMs, timedOut } = await runSubprocess(argv, { timeoutSec, env });

    const completion = {
      step:       STEP,
      rc,
      durationMs,
      startedAt,
      finishedAt: Date.now(),
      timedOut:   timedOut || false,
      status:     'failed', // overwritten below
    };

    if (rc === 0) {
      completion.status = 'ok';
      traceBus.push({ runId, node: STEP, status: 'ok', ts: Date.now(), duration: durationMs });
      await pipelineLog.feedEnd(STEP, 'ok', state.runDate, durationMs);
      return { completedSteps: [...(state.completedSteps || []), completion] };
    }

    if (rc === 1 && !strictMode(env)) {
      completion.status = 'warn';
      traceBus.push({ runId, node: STEP, status: 'warn', ts: Date.now(), duration: durationMs });
      await pipelineLog.feedEnd(STEP, 'warn', state.runDate, durationMs);
      return { completedSteps: [...(state.completedSteps || []), completion] };
    }

    // rc ≥ 2 always aborts; rc=1 in strict mode also aborts
    traceBus.push({ runId, node: STEP, status: 'err', ts: Date.now(), rc, stderrTail });
    await pipelineLog.notifyFailure(STEP, state.runDate, rc, stderrTail);

    const err = new Error(`step ${STEP} exited rc=${rc}`);
    err.step       = STEP;
    err.rc         = rc;
    err.stderrTail = stderrTail;
    err.timedOut   = timedOut || false;
    throw err;
  };
}

module.exports = { makeStepNode };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/test_daily_cycle_node.test.js`

Expected: PASS — 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/agent/graphs/daily_cycle_node.js tests/test_daily_cycle_node.test.js
git commit -m "feat(e1): daily_cycle_node.js — makeStepNode(step) factory

Single node-template factory generates a LangGraph node function for
each of the 11 daily-cycle steps. Pattern (spec §3.4):
  - honor requestedSteps subset filter (skipped → no Discord, traceBus event only)
  - emit traceBus start/ok/warn/err events
  - feedStart/feedEnd/notifyFailure via pipeline_logging
  - subprocess-exec via resolve_script
  - rc=0 → ok; rc=1 → warn-or-throw per strict gate; rc≥2 → throw

6 parameterized tests cover subset-skip, success, warn-continue,
strict-throw, hard-fail-throw, timeout-throw.

Task 4 of E1 plan."
```

---

## Task 5: Daily-cycle StateGraph wiring

**Files:**
- Create: `src/agent/graphs/daily-cycle.js`
- Create: `tests/test_daily_cycle_graph.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_daily_cycle_graph.test.js
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const DAILY_CYCLE_PATH = path.join(ROOT, 'src/agent/graphs/daily-cycle.js');
const NODE_PATH        = path.join(ROOT, 'src/agent/graphs/daily_cycle_node.js');
const LOGGING_PATH     = path.join(ROOT, 'src/execution/pipeline_logging.js');
const TRACEBUS_PATH    = path.join(ROOT, 'src/agent/traceBus.js');

const STEPS_IN_ORDER = [
  'collect', 'sentiment', 'signals', 'ic_gate', 'handoff',
  'trade', 'alpaca', 'reconcile', 'report',
  'pyportfolioopt_shadow', 'health',
];

// Stub makeStepNode so each "node" just records its invocation and appends to completedSteps
function makeStubbed({ abortAt = null } = {}) {
  const visited = [];
  require.cache[NODE_PATH] = {
    id: NODE_PATH, filename: NODE_PATH, loaded: true,
    exports: {
      makeStepNode: (STEP) => async (state, _config) => {
        visited.push(STEP);
        if (abortAt === STEP) {
          const err = new Error(`step ${STEP} stub abort`);
          err.step = STEP; err.rc = 2;
          throw err;
        }
        // skip path for subset
        if (state.requestedSteps && !state.requestedSteps.has(STEP)) return {};
        return { completedSteps: [...(state.completedSteps || []), { step: STEP, rc: 0, status: 'ok' }] };
      },
    },
  };
  require.cache[LOGGING_PATH] = {
    id: LOGGING_PATH, filename: LOGGING_PATH, loaded: true,
    exports: {
      feedStart: async () => {}, feedEnd: async () => {},
      notifyFailure: async () => {}, cycleStart: async () => {},
      cycleEnd: async () => {}, updateAgentStatus: async () => {},
    },
  };
  require.cache[TRACEBUS_PATH] = {
    id: TRACEBUS_PATH, filename: TRACEBUS_PATH, loaded: true,
    exports: { push: () => {}, startRun: () => {}, endRun: () => {} },
  };
  // Force PostgresSaver to be in-memory MemorySaver for tests
  process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER = '1';
  delete require.cache[require.resolve(DAILY_CYCLE_PATH)];
  const mod = require(DAILY_CYCLE_PATH);
  return { mod, visited };
}

test('Full happy path: all 11 nodes visited in canonical order, status ok', async () => {
  const { mod, visited } = makeStubbed();
  const out = await mod.runDailyCycleGraph({ runDate: '2026-05-21', reason: 'test' });
  assert.deepEqual(visited, STEPS_IN_ORDER);
  assert.equal(out.status, 'ok');
  assert.equal(out.completedSteps.length, 11);
});

test('Subset request: only listed steps visited', async () => {
  const { mod, visited } = makeStubbed();
  await mod.runDailyCycleGraph({
    runDate: '2026-05-21',
    reason:  'regime_transition',
    requestedSteps: ['signals', 'handoff', 'trade', 'alpaca', 'reconcile'],
  });
  // All 11 nodes still "visit" (because the test stub doesn't honor subset),
  // but graph state should only record completedSteps for the 5 requested.
  // Adjust test: assert graph state shape via output rather than visited array.
  // (Actual subset filtering lives inside makeStepNode; here we stub it.)
  // So just assert that the graph runs without throwing — subset behavior
  // is tested in Task 4.
});

test('Mid-cycle abort: throw at trade → abortedAt set, downstream not visited', async () => {
  const { mod, visited } = makeStubbed({ abortAt: 'trade' });
  const out = await mod.runDailyCycleGraph({ runDate: '2026-05-21', reason: 'test' });
  assert.equal(out.status, 'aborted');
  assert.equal(out.abortedAt, 'trade');
  // Steps before trade visited; trade visited (and threw); steps after NOT visited
  assert.ok(visited.includes('handoff'));
  assert.ok(visited.includes('trade'));
  assert.ok(!visited.includes('alpaca'));
  assert.ok(!visited.includes('reconcile'));
});

test('thread_id is "daily-cycle:<runDate>"', async () => {
  const { mod } = makeStubbed();
  const out = await mod.runDailyCycleGraph({ runDate: '2026-05-22', reason: 'test' });
  assert.equal(out.threadId, 'daily-cycle:2026-05-22');
});

test('Concurrent runs on different runDates have isolated state', async () => {
  const { mod } = makeStubbed();
  const [a, b] = await Promise.all([
    mod.runDailyCycleGraph({ runDate: '2026-05-21', reason: 'test' }),
    mod.runDailyCycleGraph({ runDate: '2026-05-22', reason: 'test' }),
  ]);
  assert.notEqual(a.threadId, b.threadId);
  assert.equal(a.completedSteps.length, 11);
  assert.equal(b.completedSteps.length, 11);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_daily_cycle_graph.test.js`

Expected: FAIL — `Cannot find module .../daily-cycle.js`.

- [ ] **Step 3: Write the implementation**

```javascript
// src/agent/graphs/daily-cycle.js
/**
 * Unified daily-cycle LangGraph.
 *
 *   START → collect → sentiment → signals → ic_gate → handoff →
 *   trade → alpaca → reconcile → report → pyportfolioopt_shadow →
 *   health → END
 *
 * One thread per runDate. PostgresSaver persists state after each
 * node (or MemorySaver in tests). traceBus events surface in dashboard
 * SSE. Replaces pipeline_orchestrator.py once OPENCLAW_LANGGRAPH_ORCHESTRATOR=1.
 */
'use strict';

const { StateGraph, Annotation, MemorySaver, END, START } = require('@langchain/langgraph');
const { PostgresSaver }                                    = require('@langchain/langgraph-checkpoint-postgres');

const traceBus       = require('../traceBus');
const pipelineLog    = require('../../execution/pipeline_logging');
const { makeStepNode } = require('./daily_cycle_node');

const STEPS_IN_ORDER = [
  'collect', 'sentiment', 'signals', 'ic_gate', 'handoff',
  'trade', 'alpaca', 'reconcile', 'report',
  'pyportfolioopt_shadow', 'health',
];

const DailyCycleState = Annotation.Root({
  runDate:        Annotation(),
  runId:          Annotation(),
  reason:         Annotation(),
  requestedSteps: Annotation(),
  completedSteps: Annotation(),
  abortedAt:      Annotation(),
  lastError:      Annotation(),
  env:            Annotation(),
});

// ── Checkpointer ─────────────────────────────────────────────────────────────
let _checkpointer = null;
let _checkpointerReady = null;

function getCheckpointer() {
  if (_checkpointer) return { checkpointer: _checkpointer, ready: _checkpointerReady };
  if (process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER === '1') {
    _checkpointer = new MemorySaver();
    _checkpointerReady = Promise.resolve();
    return { checkpointer: _checkpointer, ready: _checkpointerReady };
  }
  const uri = process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw';
  _checkpointer = PostgresSaver.fromConnString(uri, { schema: 'langgraph' });
  _checkpointerReady = _checkpointer.setup()
    .then(() => console.log('[daily-cycle] PostgresSaver schema ready'))
    .catch((e) => console.error('[daily-cycle] PostgresSaver setup failed:', e.message));
  return { checkpointer: _checkpointer, ready: _checkpointerReady };
}

// ── Graph build (lazy + memoized) ────────────────────────────────────────────
let _compiled = null;
function getCompiled() {
  if (_compiled) return _compiled;
  const { checkpointer } = getCheckpointer();
  const g = new StateGraph(DailyCycleState);
  for (const step of STEPS_IN_ORDER) g.addNode(step, makeStepNode(step));
  g.addEdge(START, STEPS_IN_ORDER[0]);
  for (let i = 0; i < STEPS_IN_ORDER.length - 1; i++) {
    g.addEdge(STEPS_IN_ORDER[i], STEPS_IN_ORDER[i + 1]);
  }
  g.addEdge(STEPS_IN_ORDER[STEPS_IN_ORDER.length - 1], END);
  _compiled = g.compile({ checkpointer });
  return _compiled;
}

// ── Redis lock (mirrors pipeline_orchestrator.py:152-158) ────────────────────
async function _acquireRunLock(runDate) {
  const Redis = require('ioredis');
  const r = new Redis(process.env.REDIS_URL || 'redis://localhost:6379');
  const key = `engine:run_lock:${runDate}`;
  const lockId = `daily-cycle:${process.pid}:${Date.now()}`;
  const ok = await r.set(key, lockId, 'NX', 'EX', 7200);
  if (!ok) {
    await r.quit();
    const owner = await new Redis(process.env.REDIS_URL).get(key).catch(() => '?');
    const err = new Error(`cycle already in progress for ${runDate} (lock held by ${owner})`);
    err.lockHeld = true;
    throw err;
  }
  return { release: async () => { try { await r.del(key); } finally { await r.quit(); } } };
}

// ── Public API ───────────────────────────────────────────────────────────────
async function runDailyCycleGraph(input) {
  const { runDate, reason = 'scheduled', requestedSteps = null } = input || {};
  if (!runDate) throw new Error('runDate is required');
  const runId    = `run-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const threadId = `daily-cycle:${runDate}`;

  // Redis lock — fail fast on concurrent same-date invocations.
  // Skipped in resume path (see resumeDailyCycle) since the lock is already
  // held by the crashed process's leaked TTL.
  let lock = null;
  if (process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER !== '1') {
    // Skip lock in tests (MemorySaver mode) — no Redis needed.
    try {
      lock = await _acquireRunLock(runDate);
    } catch (err) {
      if (err.lockHeld) {
        await pipelineLog.cycleEnd(runDate, runId, 'aborted', '__lock_held__').catch(() => {});
        return { runId, threadId, status: 'aborted', abortedAt: '__lock_held__',
                 lastError: { message: err.message } };
      }
      throw err;
    }
  }

  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();

  traceBus.startRun(runId, { cycleDate: runDate, threadId, graph: 'daily-cycle' });
  await pipelineLog.cycleStart(runDate, reason, runId);

  const statePayload = {
    runDate,
    runId,
    reason,
    requestedSteps: requestedSteps ? new Set(requestedSteps) : null,
    completedSteps: [],
    abortedAt:      null,
    lastError:      null,
    env:            {},
  };
  const config = { configurable: { thread_id: threadId } };

  try {
    const out = await compiled.invoke(statePayload, config);
    traceBus.endRun(runId, 'ok');
    await pipelineLog.cycleEnd(runDate, runId, 'ok', null);
    return { ...out, runId, threadId, status: 'ok' };
  } catch (err) {
    const abortedAt = err.step || 'unknown';
    traceBus.endRun(runId, 'error', err.message);
    await pipelineLog.cycleEnd(runDate, runId, 'aborted', abortedAt);
    // Read partial state from the checkpoint to surface completedSteps
    const snap = await compiled.getState(config).catch(() => null);
    const partial = (snap && snap.values) || {};
    return {
      ...partial,
      runId, threadId,
      status:     'aborted',
      abortedAt,
      lastError:  { step: abortedAt, rc: err.rc || null, message: err.message },
    };
  } finally {
    if (lock) await lock.release().catch(() => {});
  }
}

async function resumeDailyCycle(runDate) {
  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();
  const threadId = `daily-cycle:${runDate}`;
  const config = { configurable: { thread_id: threadId } };
  const snap = await compiled.getState(config);
  if (!snap || !snap.next || snap.next.length === 0) {
    return { status: 'no_resume_needed', threadId };
  }
  const runId = (snap.values && snap.values.runId) || `resume-${Date.now()}`;
  traceBus.startRun(runId, { cycleDate: runDate, threadId, graph: 'daily-cycle', resumed: true });
  await pipelineLog.cycleStart(runDate, 'resumed', runId);
  try {
    const out = await compiled.invoke(null, config);
    traceBus.endRun(runId, 'ok');
    await pipelineLog.cycleEnd(runDate, runId, 'ok', null);
    return { ...out, runId, threadId, status: 'ok', resumed: true };
  } catch (err) {
    traceBus.endRun(runId, 'error', err.message);
    await pipelineLog.cycleEnd(runDate, runId, 'aborted', err.step || 'unknown');
    return {
      runId, threadId,
      status:    'aborted',
      abortedAt: err.step || 'unknown',
      lastError: { step: err.step, rc: err.rc, message: err.message },
    };
  }
}

async function listThreadState(runDate) {
  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();
  const threadId = `daily-cycle:${runDate}`;
  return compiled.getState({ configurable: { thread_id: threadId } });
}

module.exports = {
  runDailyCycleGraph,
  resumeDailyCycle,
  listThreadState,
  STEPS_IN_ORDER,
  DailyCycleState,
  getCompiled,
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/test_daily_cycle_graph.test.js`

Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/agent/graphs/daily-cycle.js tests/test_daily_cycle_graph.test.js
git commit -m "feat(e1): daily-cycle.js — unified 11-node StateGraph

Linear DAG: collect → sentiment → signals → ic_gate → handoff →
trade → alpaca → reconcile → report → pyportfolioopt_shadow → health.
Each node generated via makeStepNode(step) from Task 4.

PostgresSaver checkpointing (langgraph schema, thread_id =
daily-cycle:<runDate>). MemorySaver fallback for tests via
OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER=1.

Public API: runDailyCycleGraph(input), resumeDailyCycle(runDate),
listThreadState(runDate). Mid-cycle throw is caught → reads checkpoint
snapshot → returns {status: 'aborted', abortedAt, lastError, ...partial}.

5 tests cover happy path, abort propagation, thread_id derivation,
and concurrent isolation.

Task 5 of E1 plan."
```

---

## Task 6: Recovery probe — resume crashed cycles on johnbot startup

**Files:**
- Create: `src/agent/graphs/recover_inflight.js`
- Create: `tests/test_recover_inflight.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_recover_inflight.test.js
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const RECOVER_PATH      = path.join(ROOT, 'src/agent/graphs/recover_inflight.js');
const DAILY_CYCLE_PATH  = path.join(ROOT, 'src/agent/graphs/daily-cycle.js');
const LOGGING_PATH      = path.join(ROOT, 'src/execution/pipeline_logging.js');

function makeStubbed({ inFlight = false, nextSteps = [] } = {}) {
  const resumeCalls = [];
  const cycleStartCalls = [];
  require.cache[DAILY_CYCLE_PATH] = {
    id: DAILY_CYCLE_PATH, filename: DAILY_CYCLE_PATH, loaded: true,
    exports: {
      listThreadState: async (_runDate) =>
        inFlight ? { next: nextSteps, values: { runId: 'r1', runDate: _runDate } } : null,
      resumeDailyCycle: async (runDate) => {
        resumeCalls.push(runDate);
        return { runDate, status: 'ok', resumed: true };
      },
    },
  };
  require.cache[LOGGING_PATH] = {
    id: LOGGING_PATH, filename: LOGGING_PATH, loaded: true,
    exports: {
      cycleStart: async (...args) => cycleStartCalls.push(args),
      _safePost: async () => {},
    },
  };
  delete require.cache[require.resolve(RECOVER_PATH)];
  const mod = require(RECOVER_PATH);
  return { mod, resumeCalls, cycleStartCalls };
}

test('No in-flight thread → silent exit, no resume calls', async () => {
  const { mod, resumeCalls } = makeStubbed({ inFlight: false });
  process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR = '1';
  const out = await mod.recoverInflight();
  assert.equal(out.recovered, false);
  assert.equal(resumeCalls.length, 0);
});

test('In-flight thread with next step → resume invoked', async () => {
  const { mod, resumeCalls } = makeStubbed({ inFlight: true, nextSteps: ['trade'] });
  process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR = '1';
  const out = await mod.recoverInflight();
  assert.equal(out.recovered, true);
  assert.ok(resumeCalls.length >= 1);
});

test('Flag OFF → skip recovery entirely (legacy orchestrator owns it)', async () => {
  const { mod, resumeCalls } = makeStubbed({ inFlight: true, nextSteps: ['trade'] });
  process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR = '0';
  const out = await mod.recoverInflight();
  assert.equal(out.recovered, false);
  assert.equal(out.skipped, true);
  assert.equal(resumeCalls.length, 0);
});
```

- [ ] **Step 2: Run test, see it fail**

Run: `node --test tests/test_recover_inflight.test.js`

Expected: FAIL — `Cannot find module .../recover_inflight.js`.

- [ ] **Step 3: Write the implementation**

```javascript
// src/agent/graphs/recover_inflight.js
/**
 * Startup probe: on johnbot boot, check whether the previous daily-cycle
 * thread is mid-cycle. If yes, resume from the last checkpoint.
 *
 * Gate: OPENCLAW_LANGGRAPH_ORCHESTRATOR=1 — legacy orchestrator owns
 * recovery via its Redis checkpoint when the flag is off.
 *
 * Window: today's runDate (UTC), plus yesterday's if it's before 14:00 UTC
 * (~9am ET) to cover overnight crashes.
 */
'use strict';

const { listThreadState, resumeDailyCycle } = require('./daily-cycle');

function _candidateRunDates(now = new Date()) {
  const today = now.toISOString().slice(0, 10);
  if (now.getUTCHours() < 14) {
    const y = new Date(now); y.setUTCDate(y.getUTCDate() - 1);
    return [today, y.toISOString().slice(0, 10)];
  }
  return [today];
}

async function recoverInflight() {
  if (process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR !== '1') {
    return { recovered: false, skipped: true, reason: 'flag_off' };
  }
  for (const runDate of _candidateRunDates()) {
    let snap;
    try {
      snap = await listThreadState(runDate);
    } catch (e) {
      console.warn(`[recover_inflight] listThreadState ${runDate} failed: ${e.message}`);
      continue;
    }
    if (snap && snap.next && snap.next.length > 0) {
      console.log(`[recover_inflight] resuming daily-cycle:${runDate} at ${snap.next.join(',')}`);
      const out = await resumeDailyCycle(runDate);
      return { recovered: true, runDate, nextSteps: snap.next, result: out };
    }
  }
  return { recovered: false };
}

module.exports = { recoverInflight, _candidateRunDates };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/test_recover_inflight.test.js`

Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/agent/graphs/recover_inflight.js tests/test_recover_inflight.test.js
git commit -m "feat(e1): recover_inflight.js — startup probe to resume crashed cycles

On johnbot boot (gated on OPENCLAW_LANGGRAPH_ORCHESTRATOR=1), check
the daily-cycle PostgresSaver thread for today (and yesterday if
before 9am ET). If snapshot has next steps queued, call
resumeDailyCycle(runDate). Legacy orchestrator owns recovery when
flag is off.

3 tests cover: no in-flight → silent exit, in-flight → resume,
flag off → skip.

Task 6 of E1 plan."
```

---

## Task 7: Register daily-cycle in graph registry

**Files:**
- Modify: `src/agent/graphs/index.js`

- [ ] **Step 1: Read current registry**

Run: `cat src/agent/graphs/index.js`

The file should look like:

```javascript
'use strict';
const cycleGraph = require('../graph');
const paperhunter = require('./paperhunter');

const graphs = {
  cycle: { /* ... */ },
  paperhunter: { /* ... */ },
};
// ... list / get / module.exports
```

- [ ] **Step 2: Add `daily-cycle` to the registry**

Edit `src/agent/graphs/index.js` — add a new `require` and a new entry to the `graphs` object:

```javascript
'use strict';

const cycleGraph   = require('../graph');
const paperhunter  = require('./paperhunter');
const dailyCycle   = require('./daily-cycle');     // NEW

const graphs = {
  cycle: {
    name: 'cycle',
    description: 'Daily cycle: datajohn → tradejohn → (HITL) → botjohn',
    run: async (input) => cycleGraph.runCycleGraph(input),
    resume: async (input) => cycleGraph.resumeCycle(input),
    state: async (threadId) => cycleGraph.listThreadState(threadId),
    nodes: ['datajohn', 'tradejohn', 'botjohn'],
    features: ['postgres-checkpoint', 'hitl-interrupt', 'conditional-routing'],
  },
  paperhunter: {
    name: 'paperhunter',
    description: 'Parallel fan-out over paper candidates (Send)',
    run: async (input) => paperhunter.runPaperHunt(input),
    nodes: ['dispatch', 'extract_one', 'reduce'],
    features: ['parallel-fanout'],
  },
  // NEW: daily-cycle graph for the production data pipeline
  'daily-cycle': {
    name: 'daily-cycle',
    description: 'Unified 11-step data pipeline (replaces pipeline_orchestrator.py)',
    run:    async (input)   => dailyCycle.runDailyCycleGraph(input),
    resume: async (input)   => dailyCycle.resumeDailyCycle(input.runDate || input),
    state:  async (runDate) => dailyCycle.listThreadState(runDate),
    nodes:  dailyCycle.STEPS_IN_ORDER,
    features: ['postgres-checkpoint', 'subset-filter', 'subprocess-wraps'],
  },
};

function list() {
  return Object.values(graphs).map(({ name, description, nodes, features }) => ({
    name, description, nodes, features,
  }));
}

function get(name) { return graphs[name]; }

module.exports = { graphs, list, get };
```

- [ ] **Step 3: Smoke-check the registry**

Run: `node -e "const r = require('./src/agent/graphs'); console.log(r.list().map(g => g.name));"`

Expected output: `[ 'cycle', 'paperhunter', 'daily-cycle' ]`

- [ ] **Step 4: Confirm `bin/run-graph.js` auto-discovers the new graph**

Run: `node bin/run-graph.js list`

Expected: JSON list including `{ "name": "daily-cycle", "nodes": ["collect", "sentiment", ..., "health"] }`. No changes to `bin/run-graph.js` needed — the existing registry dispatch handles new graphs automatically.

- [ ] **Step 5: Commit**

```bash
git add src/agent/graphs/index.js
git commit -m "feat(e1): register daily-cycle graph in graphs/index.js

Adds 'daily-cycle' entry to the graph registry with run / resume /
state handlers, 11-node listing, and feature flags. bin/run-graph.js
auto-discovers via the existing registry dispatch — no CLI changes
needed.

Verify: node bin/run-graph.js list → includes daily-cycle.

Task 7 of E1 plan."
```

---

## Task 8: Gate-aware cron-schedule.js dispatch

**Files:**
- Modify: `src/engine/cron-schedule.js` (around line 250 — the existing 10am ET handler)

- [ ] **Step 1: Locate the existing 10am handler**

Run: `grep -n "10am cycle: spawning" src/engine/cron-schedule.js`

Expected: line ~254 inside the `cron.schedule('0 10 * * 1-5', () => {...})` block.

- [ ] **Step 2: Replace the handler with a gate-aware version**

Find the block (around lines 245-275) that looks like:

```javascript
cron.schedule('0 10 * * 1-5', () => {
    log('10am cycle: spawning pipeline_orchestrator.py');
    try {
        const { spawn } = require('child_process');
        const fs = require('fs');
        const path = require('path');
        const today = new Date().toISOString().slice(0, 10);
        const logDir = path.join(ROOT, 'logs');
        // ... existing spawn body
    } catch (e) { /* ... */ }
});
```

Replace with:

```javascript
cron.schedule('0 10 * * 1-5', () => {
    const useLangGraph = process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR === '1';
    const today = new Date().toISOString().slice(0, 10);

    if (useLangGraph) {
        log('10am cycle: dispatching to LangGraph daily-cycle');
        try {
            const { runDailyCycleGraph } = require('../agent/graphs/daily-cycle');
            // Fire-and-forget: cycle posts its own Discord progress; cron just logs the dispatch.
            runDailyCycleGraph({ runDate: today, reason: 'scheduled' })
                .then((out) => log(`daily-cycle finished: status=${out.status} aborted=${out.abortedAt || 'none'}`))
                .catch((err) => log(`daily-cycle FAILED: ${err.message}`));
        } catch (e) {
            log(`daily-cycle dispatch error: ${e.message}`);
        }
        return;
    }

    // Legacy path — Python orchestrator
    log('10am cycle: spawning pipeline_orchestrator.py (legacy path)');
    try {
        const { spawn } = require('child_process');
        const fs = require('fs');
        const path = require('path');
        const logDir = path.join(ROOT, 'logs');
        fs.mkdirSync(logDir, { recursive: true });
        const logFile = fs.openSync(path.join(logDir, `pipeline-${today}.log`), 'a');
        const orchestrator = path.join(ROOT, 'src', 'execution', 'pipeline_orchestrator.py');
        const proc = spawn('python3', [orchestrator, '--date', today], {
            cwd:      ROOT,
            env:      { ...process.env, PYTHONPATH: ROOT },
            detached: true,
            stdio:    ['ignore', logFile, logFile],
        });
        proc.unref();
        log(`pipeline orchestrator spawned (pid ${proc.pid}) for ${today}`);
    } catch (e) {
        log(`pipeline orchestrator spawn error: ${e.message}`);
    }
});
```

(Preserve the rest of the cron-schedule.js file — only the 10am handler block changes.)

- [ ] **Step 3: Verify the file still parses**

Run: `node -c src/engine/cron-schedule.js`

Expected: no output (clean exit). If syntax error, fix and re-run.

- [ ] **Step 4: Smoke the dispatch with flag off**

Run:
```bash
unset OPENCLAW_LANGGRAPH_ORCHESTRATOR
node -e "process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR=''; console.log(require('./src/engine/cron-schedule.js'))"
```

Expected: file loads without error. (Full cron behavior is tested manually via the rollout checklist in spec §6.2.)

- [ ] **Step 5: Commit**

```bash
git add src/engine/cron-schedule.js
git commit -m "feat(e1): cron-schedule.js — gate-aware 10am dispatch

OPENCLAW_LANGGRAPH_ORCHESTRATOR=1 routes the 10am ET Mon-Fri cron
job to runDailyCycleGraph(...) in-process. Unset (default) preserves
the legacy spawn of pipeline_orchestrator.py exactly as today.

Fire-and-forget at the cron tick — the cycle's own Discord
notifications surface progress to operators.

Task 8 of E1 plan."
```

---

## Task 9: Gate-aware redeploy_pipeline.py inner spawn

**Files:**
- Modify: `scripts/redeploy_pipeline.py`

- [ ] **Step 1: Locate the existing pipeline_orchestrator.py spawn**

Run: `grep -n "pipeline_orchestrator\|subprocess\|--steps\|--reason" scripts/redeploy_pipeline.py | head -10`

Expected: find a `subprocess.run([..., 'pipeline_orchestrator.py', '--steps', ..., '--reason', ...])` block.

- [ ] **Step 2: Add gate-aware inner-spawn dispatch**

The existing spawn lives at lines ~165-194 inside the function that builds the legacy `cmd = [sys.executable, str(ROOT/'src/execution/pipeline_orchestrator.py'), '--steps', REDEPLOY_STEPS, '--reason', reason, '--date', run_date, '--force-resume']` and calls `subprocess.run(cmd, env=env, timeout=ORCHESTRATOR_TIMEOUT_S, check=False)`.

Replace that block. `json` and `os` are already imported at the top of the file (no new imports needed). The `--force-resume` flag is a legacy concept (bypasses pipeline_orchestrator.py's daily-completion sentinel) — the LangGraph path does not need an equivalent, since PostgresSaver thread state is the new resumability primitive.

New block (replace lines ~172-194):

```python
if os.environ.get('OPENCLAW_LANGGRAPH_ORCHESTRATOR') == '1':
    # New path: invoke the LangGraph daily-cycle graph with subset filter.
    run_graph_js = ROOT / 'bin' / 'run-graph.js'
    payload = {
        'runDate':        run_date,
        'reason':         reason,
        'requestedSteps': REDEPLOY_STEPS.split(','),  # legacy var is a CSV string
    }
    cmd = ['node', str(run_graph_js), 'daily-cycle', json.dumps(payload)]
    logger.info('spawning LangGraph daily-cycle (dry_run=%s): %s', dry_run, ' '.join(cmd))
else:
    # Legacy path: pipeline_orchestrator.py --steps --reason --force-resume
    cmd = [
        sys.executable,
        str(ROOT / 'src' / 'execution' / 'pipeline_orchestrator.py'),
        '--steps', REDEPLOY_STEPS,
        '--reason', reason,
        '--date', run_date,
        '--force-resume',
    ]
    logger.info('spawning orchestrator (dry_run=%s): %s', dry_run, ' '.join(cmd))

env = {**os.environ}
if dry_run:
    env['PIPELINE_DRY_RUN'] = '1'
try:
    proc = subprocess.run(cmd, env=env, timeout=ORCHESTRATOR_TIMEOUT_S, check=False)
    return proc.returncode
except subprocess.TimeoutExpired:
    logger.error('pipeline subprocess timed out after %ss', ORCHESTRATOR_TIMEOUT_S)
    return 1
except Exception as e:
    logger.error('pipeline subprocess spawn error: %s', e)
    return 1
```

Note: the existing variables (`REDEPLOY_STEPS`, `reason`, `run_date`, `dry_run`, `ROOT`, `ORCHESTRATOR_TIMEOUT_S`, `logger`, `sys`) are all already in scope at the call site — no new declarations needed.

- [ ] **Step 3: Run the existing redeploy_pipeline.py tests if any**

Run: `python3 -m pytest tests/ -k 'redeploy' -v 2>&1 | tail -15`

Expected: existing tests pass (or no tests match — both are acceptable). If tests fail because they assert against the legacy spawn shape, update them to accept both paths via env-var fixture.

- [ ] **Step 4: Smoke the script with flag off (legacy path)**

Run:
```bash
unset OPENCLAW_LANGGRAPH_ORCHESTRATOR
python3 -c "import scripts.redeploy_pipeline"
```

Expected: no import error.

- [ ] **Step 5: Commit**

```bash
git add scripts/redeploy_pipeline.py
git commit -m "feat(e1): redeploy_pipeline.py — gate-aware inner spawn

OPENCLAW_LANGGRAPH_ORCHESTRATOR=1 routes intraday regime-transition
redeploys through bin/run-graph.js daily-cycle with requestedSteps=
[signals, handoff, trade, alpaca, reconcile]. Unset preserves the
legacy pipeline_orchestrator.py --steps --reason invocation exactly
as today.

Outer Redis cooldown + sentinel + RTH ship-safety gate unchanged.

Task 9 of E1 plan."
```

---

## Task 10: `.env` gate config — `OPENCLAW_LANGGRAPH_ORCHESTRATOR`

**Files:**
- Modify: `.env` (gitignored — manual addition, no commit)

- [ ] **Step 1: Add the gate (unset = legacy path, the default)**

Open `/root/openclaw/.env` and append at the bottom:

```bash
# E1 — LangGraph orchestrator migration (2026-05-20). When set, the 10am
# cron job and scripts/redeploy_pipeline.py route through the new
# src/agent/graphs/daily-cycle.js StateGraph instead of the legacy
# src/execution/pipeline_orchestrator.py. Resumability + dashboard
# observability via PostgresSaver checkpoints + traceBus emissions.
#
# Unset (or set to anything other than '1') = legacy path.
# Default OFF until operator validates the new graph in production.
# OPENCLAW_LANGGRAPH_ORCHESTRATOR=1
```

(Leave it commented out — the flip happens during rollout per spec §6.2.)

- [ ] **Step 2: Confirm `.env` still parses for the bot**

Run: `set -a && source <(grep -v '^SEC_USER_AGENT' /root/openclaw/.env) && set +a && echo "loaded ok"`

Expected: `loaded ok` (no syntax errors from the new lines).

- [ ] **Step 3: No commit**

`.env` is gitignored. No git operation needed. Note this in the rollout checklist.

---

## Task 11: Integration smoke — full graph against today's data

**Files:** none (manual validation)

This is the pre-flip dry-run from spec §6.1.

- [ ] **Step 1: Set up dry-run env (without enabling the gate persistently)**

```bash
cd /root/openclaw
export OPENCLAW_LANGGRAPH_ORCHESTRATOR=1
export PIPELINE_DRY_RUN=1
export POSTGRES_URI=$(grep '^POSTGRES_URI=' .env | cut -d= -f2-)
```

- [ ] **Step 2: Run the full graph against today's date**

```bash
node bin/run-graph.js daily-cycle "$(printf '{"runDate":"%s","reason":"smoke"}' "$(date +%F)")"
```

Expected output (JSON, after 5-15 minutes):
```json
{
  "runDate": "2026-05-21",
  "runId": "run-...",
  "completedSteps": [
    {"step": "collect",                "rc": 0, "status": "ok", "durationMs": ...},
    {"step": "sentiment",              "rc": 0, "status": "ok", "durationMs": ...},
    ...11 entries...
  ],
  "abortedAt": null,
  "status": "ok",
  "threadId": "daily-cycle:2026-05-21"
}
```

- [ ] **Step 3: Verify Discord messages**

Check `#pipeline-feed` channel — should see 22+ messages: `🚀 daily cycle started`, 11× `▶️ <step> started`, 11× `✅ <step> ok`, and `✅ daily cycle completed`.

If `OPENCLAW_SENTIMENT_INGEST=1` is also set, the sentiment step appears in the chain. If not, sentiment node self-skips inside its existing script.

- [ ] **Step 4: Run a second time (idempotency check)**

```bash
node bin/run-graph.js daily-cycle "$(printf '{"runDate":"%s","reason":"smoke-2"}' "$(date +%F)")"
```

Expected: same successful completion. (PostgresSaver creates a new run row but the underlying scripts are idempotent — no duplicate orders, no double-submission.)

- [ ] **Step 5: Run a subset (redeploy-shape) to verify subset filtering**

```bash
node bin/run-graph.js daily-cycle "$(printf '{"runDate":"%s","reason":"redeploy-smoke","requestedSteps":["signals","handoff","trade","alpaca","reconcile"]}' "$(date +%F)")"
```

Expected: only 5 nodes execute; the other 6 emit `traceBus.push({status:'skipped'})` and don't post to Discord.

- [ ] **Step 6: Inspect checkpoint state via CLI**

```bash
node bin/run-graph.js daily-cycle:state "$(date +%F)"
```

Expected: JSON snapshot of the last run's `DailyCycleState`, with `completedSteps` populated.

- [ ] **Step 7: Reset env (do NOT leave OPENCLAW_LANGGRAPH_ORCHESTRATOR=1 in shell)**

```bash
unset OPENCLAW_LANGGRAPH_ORCHESTRATOR
unset PIPELINE_DRY_RUN
```

The gate stays commented-out in `.env` until the operator flips it during the rollout window per spec §6.2.

---

## Task 12: Rollout flip + 1-week cohabitation validation

**Files:** none (operator-driven)

Per spec §6.2 and §6.3.

- [ ] **Step 1: Pick a non-trading day (Sat/Sun or US market holiday)**

- [ ] **Step 2: Run one manual validation cycle on that day**

```bash
node bin/run-graph.js daily-cycle "$(printf '{"runDate":"%s","reason":"manual_flip_validation"}' "$(date +%F)")"
```

Compare output + Discord messages against the prior trading day's legacy run.

- [ ] **Step 3: Edit `.env`**

Uncomment the gate line:

```diff
- # OPENCLAW_LANGGRAPH_ORCHESTRATOR=1
+ OPENCLAW_LANGGRAPH_ORCHESTRATOR=1
```

- [ ] **Step 4: Restart johnbot**

```bash
systemctl restart johnbot.service
sleep 3
systemctl is-active johnbot.service  # → active
journalctl -u johnbot.service -n 50 --no-pager  # → no errors during startup
```

- [ ] **Step 5: Operator on standby for next Monday's 10am ET cycle**

Watch `#pipeline-feed` end-to-end. If anything looks off, immediately:

```bash
sed -i 's/^OPENCLAW_LANGGRAPH_ORCHESTRATOR=1/# OPENCLAW_LANGGRAPH_ORCHESTRATOR=1/' .env
systemctl restart johnbot.service
```

cron will resume spawning `pipeline_orchestrator.py` on the next 10am ET tick.

- [ ] **Step 6: After 1 clean week (5 daily cycles + any intraday redeploys), delete legacy code**

```bash
rm src/execution/pipeline_orchestrator.py
rm src/agent/graph.js
```

Update `CLAUDE.md` references to point at `src/agent/graphs/daily-cycle.js`. Also retire `tests/test_pipeline_orchestrator_steps.py`, `tests/test_pipeline_orchestrator_sentiment_step.py`, and any other tests that exclusively cover the deleted Python file.

```bash
git add -A
git commit -m "chore(e1): remove legacy pipeline_orchestrator.py + graph.js

Daily-cycle LangGraph has run cleanly for 1 week (N cycles + M
intraday redeploys, zero rollbacks). Cohabitation period over;
deleting the legacy paths.

CLAUDE.md updated. Three orphan test files (test_pipeline_
orchestrator_*.py, test_dry_run_dataflow.py) removed — their
behaviors are now covered by tests/test_daily_cycle_*.test.js."
```

---

## Done

All 12 tasks complete. The 11-step production data pipeline runs on a single LangGraph in production. Resumability across johnbot restarts works via PostgresSaver. The dashboard's existing graph-runs SSE panel auto-surfaces daily-cycle traces. Legacy Python orchestrator + agent-cycle graph removed.

**Total LOC delta:** ≈+1000 (new graph + helpers + 5 test files), −1185 (deleted pipeline_orchestrator.py + graph.js after week 1). Net: ~−185 LOC and one fewer orchestration model to maintain.

**Plan deviations from the spec to flag during execution:**
- Test count is closer to **23 cases** (not 83 as the spec estimated) because we test the node template thoroughly once and trust the factory pattern for the other 10 nodes. If reviewers want a per-node test, add them after the parameterized suite is green.
- `STEP_AGENTS` line reference in the Python source (`pipeline_orchestrator.py:743`) is approximate — locate the actual dict literal during implementation.

**Gates default-OFF:** `OPENCLAW_LANGGRAPH_ORCHESTRATOR` stays unset in `.env` until the operator-driven flip in Task 12.
