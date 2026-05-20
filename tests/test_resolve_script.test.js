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
