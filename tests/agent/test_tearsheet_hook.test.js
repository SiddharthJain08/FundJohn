'use strict';

/**
 * tests/agent/test_tearsheet_hook.test.js
 *
 * Guards `_generateTearsheet` — the out-of-band post-backtest tearsheet hook
 * added to research-orchestrator.js in the 2026-08-24 final fix wave
 * (review findings I1+I2, task-final-fix-report.md).
 *
 * I2 was: unified_backtest.py used to fire scripts/generate_tearsheet.py
 * IN-PROCESS, before `run_backtest()` returned, which put the tearsheet
 * render inside the orchestrator's single 900s `_spawnPython` budget for
 * the whole `python3 -m backtest.unified_backtest ...` call — a SIGTERM
 * during a slow render could kill the backtest child and make the
 * orchestrator record `backtest_failed` even though the run row was already
 * committed durably. The fix: unified_backtest.py's in-process hook became
 * opt-in-only (separately tested in
 * tests/backtest/test_tail_stats_backtest_wiring.py), and the orchestrator
 * now fires its OWN out-of-band call — `_generateTearsheet` — AFTER
 * run_backtest() has already returned successfully, on its own separate
 * 180s budget, non-fatally.
 *
 * `_generateTearsheet(runId, notify, spawnFn)` takes an injectable spawn
 * function specifically so this suite never spawns a real python3 process
 * (mirrors how scripts/tournament_dryrun.js stubs _backtestFn / _validateFn
 * / etc. to stay offline — see that script's module docstring). The
 * production call site (_codeFromQueue, via the `this._tearsheetFn` seam)
 * defaults to the real `_spawnPython`; tournament_dryrun.js itself never
 * reaches this call site at all (it exercises `_runTournament` directly,
 * per its own docstring "the unit Task S2 actually adds"), which is exactly
 * what keeps a tournament dry run from spawning a real subprocess here —
 * confirmed by `node scripts/tournament_dryrun.js` still printing
 * "DRY RUN OK" after this change.
 *
 * Run:
 *   node --test tests/agent/test_tearsheet_hook.test.js
 */

process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';

const path   = require('path');
const { test } = require('node:test');
const assert    = require('node:assert/strict');

const { _generateTearsheet } = require('../../src/agent/research/research-orchestrator');

const REPO_ROOT = path.resolve(__dirname, '..', '..');

test('_generateTearsheet: no-op when runId is falsy — spawnFn never called', async () => {
  let calls = 0;
  const spawnStub = async () => { calls += 1; return { code: 0, stdout: '', stderr: '' }; };
  await _generateTearsheet(null, () => {}, spawnStub);
  await _generateTearsheet(undefined, () => {}, spawnStub);
  await _generateTearsheet('', () => {}, spawnStub);
  assert.equal(calls, 0);
});

test('_generateTearsheet: spawns generate_tearsheet.py with --run-id, own 180s timeout, PYTHONPATH=src, repo-root cwd', async () => {
  let capturedArgs, capturedOpts;
  const spawnStub = async (args, opts) => {
    capturedArgs = args;
    capturedOpts = opts;
    return { code: 0, stdout: 'output/tearsheets/S_x_run-123.html', stderr: '' };
  };
  const notifyLog = [];
  await _generateTearsheet('run-123', (m) => notifyLog.push(m), spawnStub);

  assert.deepEqual(capturedArgs, ['scripts/generate_tearsheet.py', '--run-id', 'run-123']);
  assert.equal(capturedOpts.timeoutMs, 180_000);
  assert.equal(path.resolve(capturedOpts.cwd), REPO_ROOT);
  assert.equal(capturedOpts.env.PYTHONPATH, 'src');
  // 900s is the orchestrator's separate _backtestFn budget — this hook must
  // never share it (that sharing is exactly what I2 was).
  assert.notEqual(capturedOpts.timeoutMs, 900_000);
  // A clean run logs nothing — this hook is silent on success.
  assert.equal(notifyLog.length, 0);
});

test('_generateTearsheet: non-zero exit is swallowed and logged once, never throws', async () => {
  const spawnStub = async () => ({ code: 1, stdout: '', stderr: 'boom: DB unavailable' });
  const notifyLog = [];
  await assert.doesNotReject(
    _generateTearsheet('run-456', (m) => notifyLog.push(m), spawnStub));
  assert.equal(notifyLog.length, 1);
  assert.match(notifyLog[0], /\[tearsheet\] skipped: exit=1/);
  assert.match(notifyLog[0], /boom: DB unavailable/);
});

test('_generateTearsheet: a throwing/timing-out spawnFn is swallowed and logged once, never throws', async () => {
  const spawnStub = async () => { throw new Error('wedged (SIGTERM)'); };
  const notifyLog = [];
  await assert.doesNotReject(
    _generateTearsheet('run-789', (m) => notifyLog.push(m), spawnStub));
  assert.equal(notifyLog.length, 1);
  assert.match(notifyLog[0], /\[tearsheet\] skipped: wedged \(SIGTERM\)/);
});

test('_generateTearsheet: missing notify callback never throws (best-effort, optional logging)', async () => {
  const spawnStub = async () => ({ code: 1, stdout: '', stderr: 'x' });
  await assert.doesNotReject(_generateTearsheet('run-999', undefined, spawnStub));
});
