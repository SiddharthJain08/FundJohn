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
  fs.writeFileSync(path.join(dir, 'src/execution/handoff.py'), '# py\n');
  return dir;
}

test('Python script in src/pipeline resolves with --date and 600s timeout', () => {
  const root = makeFixture();
  const { argv, timeoutSec } = resolveScript('run_sentiment_step', '2026-05-21', {}, root);
  assert.deepEqual(argv, ['python3', path.join(root, 'src/pipeline/run_sentiment_step.py'), '--date', '2026-05-21']);
  assert.equal(timeoutSec, 600);
});

test('run_collector_once gets a raised heap limit and OPENCLAW_COLLECT_TIMEOUT_SECONDS (default 9000s)', () => {
  const root = makeFixture();
  const { argv, timeoutSec } = resolveScript('run_collector_once', '2026-05-21', {}, root);
  // run_collector_once gets a raised V8 heap limit (OOM hardening — see
  // src/execution/resolve_script.js).
  assert.deepEqual(argv, ['node', '--max-old-space-size=4096', path.join(root, 'src/pipeline/run_collector_once.js')]);
  assert.equal(timeoutSec, 9000);
  const env = { OPENCLAW_COLLECT_TIMEOUT_SECONDS: '4200' };
  assert.equal(resolveScript('run_collector_once', '2026-05-21', env, root).timeoutSec, 4200);
});

test('Fallback src/execution Python with default 300s timeout', () => {
  const root = makeFixture();
  const { argv, timeoutSec } = resolveScript('handoff', '2026-05-21', {}, root);
  assert.deepEqual(argv, ['python3', path.join(root, 'src/execution/handoff.py'), '--date', '2026-05-21']);
  assert.equal(timeoutSec, 300);
});

test('engine (signals) uses OPENCLAW_SIGNALS_TIMEOUT_SECONDS, default 900s — NOT the bare 300s fallback', () => {
  // Regression guard for 2026-08-05: the signals step sat on the generic 300s
  // literal while its runtime crept 173s→264s, then blew the cap (rc=124) for
  // a zero-signal day. Must stay in lockstep with the Python twin in
  // pipeline_orchestrator.py:_resolve_script.
  const root = makeFixture();
  const { argv, timeoutSec } = resolveScript('engine', '2026-05-21', {}, root);
  assert.deepEqual(argv, ['python3', path.join(root, 'src/execution/engine.py'), '--date', '2026-05-21']);
  assert.equal(timeoutSec, 900);
  const env = { OPENCLAW_SIGNALS_TIMEOUT_SECONDS: '1200' };
  assert.equal(resolveScript('engine', '2026-05-21', env, root).timeoutSec, 1200);
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
