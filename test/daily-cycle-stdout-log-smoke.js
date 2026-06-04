/**
 * Smoke test: daily_cycle_node persists step STDOUT to disk on EVERY
 * completion (success included), not just stderr on aborts.
 *
 * Why: rc=0 zero-order days were un-diagnosable twice (2026-06-02/03) —
 * the sizer's own log line naming the cause ("dropped N tickers below
 * min_cum_sharpe=4.00 (kept=0)") was captured by runSubprocess and then
 * discarded. See /root/sp6_phaseA_conviction_gate_diagnosis_2026-06-04.md §7.
 *
 *   1. rc=0 step  → logs/daily_cycle_steps_<runDate>.log gains a header
 *                   (step, rc, runId) + the subprocess stdout tail.
 *   2. rc=2 step  → SAME steps log gains the failing step's stdout too
 *                   (and the pre-existing aborts log still gets stderr).
 *   3. stdout tail is bounded (~4k chars) so chatty steps can't bloat it.
 *
 * Run: node test/daily-cycle-stdout-log-smoke.js
 */
'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..');
const RUN_DATE = '2099-01-01'; // clearly-fake date so we never collide with prod logs
const STEPS_LOG = path.join(ROOT, 'logs', `daily_cycle_steps_${RUN_DATE}.log`);
const ABORTS_LOG = path.join(ROOT, 'logs', `daily_cycle_aborts_${RUN_DATE}.log`);

// Behavior of the fake step script, switched per test case.
let scriptMode = 'ok';

// Monkey-patch daily_cycle_node's collaborators BEFORE requiring it.
// resolve_script → a real subprocess (node -e) so runSubprocess runs for real;
// pipeline_logging / traceBus → no-ops (no Discord, no SSE).
const origLoad = Module._load;
Module._load = function (request, parent, ...rest) {
  if (parent && parent.filename && parent.filename.includes('daily_cycle_node.js')) {
    if (request === '../../execution/resolve_script') {
      return {
        resolveScript: () => {
          if (scriptMode === 'ok') {
            return {
              argv: ['node', '-e',
                'console.log("STDOUT_MARKER_OK kept=0 below min_cum_sharpe=4.00")'],
              timeoutSec: 30,
            };
          }
          if (scriptMode === 'long') {
            return {
              argv: ['node', '-e',
                'process.stdout.write("x".repeat(50000) + "TAIL_MARKER_END\\n")'],
              timeoutSec: 30,
            };
          }
          // 'fail' — stdout AND stderr, rc=2
          return {
            argv: ['node', '-e',
              'console.log("STDOUT_MARKER_FAIL pre-crash breadcrumb");' +
              'console.error("STDERR_MARKER boom");process.exit(2)'],
            timeoutSec: 30,
          };
        },
      };
    }
    if (request === '../../execution/pipeline_logging') {
      return {
        feedStart: async () => {},
        feedEnd: async () => {},
        notifyFailure: async () => {},
      };
    }
    if (request === '../traceBus') {
      return { push: () => {} };
    }
  }
  return origLoad.call(this, request, parent, ...rest);
};

const { makeStepNode } = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_node.js'));

function cleanup() {
  for (const f of [STEPS_LOG, ABORTS_LOG]) {
    try { fs.unlinkSync(f); } catch (_) {}
  }
}

(async () => {
  cleanup();
  const state = { runId: 'run-stdoutlog-test', runDate: RUN_DATE, requestedSteps: ['trade'] };

  // ── 1. rc=0: stdout persisted ────────────────────────────────────────────
  scriptMode = 'ok';
  const node = makeStepNode('trade');
  const out = await node(state, {});
  assert.ok(out.completedSteps && out.completedSteps[0].status === 'ok',
    'sanity: rc=0 step completes ok');
  assert.ok(fs.existsSync(STEPS_LOG),
    `rc=0 must create the steps log at ${STEPS_LOG}`);
  let log = fs.readFileSync(STEPS_LOG, 'utf8');
  assert.ok(log.includes('STDOUT_MARKER_OK kept=0 below min_cum_sharpe=4.00'),
    'steps log must contain the rc=0 stdout');
  assert.ok(log.includes('step=trade') && log.includes('rc=0'),
    'steps log must carry a header with step + rc');
  assert.ok(log.includes('run-stdoutlog-test'),
    'steps log header must carry the runId');

  // ── 2. rc=2: stdout persisted to steps log; stderr still in aborts log ──
  scriptMode = 'fail';
  let threw = false;
  try {
    await node(state, {});
  } catch (e) {
    threw = true;
    assert.strictEqual(e.rc, 2, 'sanity: rc=2 aborts');
  }
  assert.ok(threw, 'sanity: rc=2 must throw');
  log = fs.readFileSync(STEPS_LOG, 'utf8');
  assert.ok(log.includes('STDOUT_MARKER_FAIL pre-crash breadcrumb'),
    'failing step stdout must ALSO land in the steps log');
  assert.ok(log.includes('rc=2'), 'steps log must record the failing rc');
  const aborts = fs.readFileSync(ABORTS_LOG, 'utf8');
  assert.ok(aborts.includes('STDERR_MARKER boom'),
    'pre-existing abort-stderr persistence must be unchanged');

  // ── 3. stdout tail is bounded ────────────────────────────────────────────
  cleanup();
  scriptMode = 'long';
  await node(state, {});
  log = fs.readFileSync(STEPS_LOG, 'utf8');
  assert.ok(log.includes('TAIL_MARKER_END'),
    'tail must keep the END of long stdout (where exit-path breadcrumbs live)');
  assert.ok(log.length < 10000,
    `50k stdout must be tail-truncated in the log (got ${log.length} chars)`);

  cleanup();
  console.log('daily-cycle-stdout-log-smoke: ALL PASS');
})().catch((e) => {
  cleanup();
  console.error('FAIL:', e.message);
  process.exit(1);
});
