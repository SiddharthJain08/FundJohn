const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const Module = require('module');

const SRC = path.resolve(__dirname, '../../src/engine/cron-schedule.js');
const text = fs.readFileSync(SRC, 'utf8');

test('cron.schedule appears exactly once (inside _sched)', () => {
  assert.strictEqual((text.match(/cron\.schedule\(/g) || []).length, 1);
});

const EXPECTED_NAMES = [
  'token-budget-reset', 'legacy-daily-cycle-1000', 'closeexec-compute-1510', 'closeexec-execute-1555',
  'closeexec-sod-refresh-0935', 'eod-signal-register-1615', 'premarket-news-0905', 'premarket-gate-0915',
  'open-reconcile-0925', 'opg-sweep-0932', 'eod-into-close-fill-1555', 'tier1-acting-ingest-1430',
  'sameday-compute-1500', 'sameday-execute-1555', 'sameday-eod-collect-1615', 'sameday-evening-marks-1620',
  'sameday-sod-refresh-0935', 'morning-regime-0900', 'intraday-hmm-tick', 'crypto-regime-tick',
  'intraday-hmm-refit-1800', 'options-aggregates-refresh-0605', 'cadence-recompute-2355',
  'circuit-breaker-tick', 'weekly-maintenance-sun0800', 'curator-priors-snapshot-0305',
];

test('26 _sched names, all unique, set-equal to the Task 6 brief table', () => {
  const names = [...text.matchAll(/_sched\('([a-z0-9-]+)'/g)].map((m) => m[1]);
  assert.strictEqual(names.length, 26);
  assert.strictEqual(new Set(names).size, 26);
  assert.strictEqual(EXPECTED_NAMES.length, 26);
  assert.deepStrictEqual([...names].sort(), [...EXPECTED_NAMES].sort());
});

test('a captured callback writes a cron start row named for its job', async () => {
  const cbs = {};
  const cronMock = { schedule: (expr, fn) => { cbs[expr] = fn; return { stop() {} }; } };
  const queries = [];
  class FakePool { query(q) { queries.push(q); return Promise.resolve({ rows: [{ id: 1 }] }); } on() {} }
  const cpMock = { execSync: () => Buffer.from(''), spawn: () => ({ unref() {}, on() {}, stdout: { on() {} }, stderr: { on() {} } }) };
  const origLoad = Module._load;
  Module._load = function (request, ...rest) {
    if (request === 'node-cron') return cronMock;
    if (request === 'pg') return { Pool: FakePool };
    if (request === 'child_process') return cpMock;
    return origLoad.call(this, request, ...rest);
  };
  const prev = process.env.OPENCLAW_SAMEDAY_EXEC;
  process.env.OPENCLAW_SAMEDAY_EXEC = '1';
  delete require.cache[SRC];
  try {
    const cs = require(SRC);
    try { cs.start({ bots: {} }, () => 'id', async () => {}); } catch (_) { /* gates */ }
    assert.ok(cbs['35 9 * * 1-5'], 'SOD callback registered');
    try { cbs['35 9 * * 1-5'](); } catch (_) { /* callback body may fail under mocks */ }
    const ins = queries.find((q) => /INSERT INTO scheduled_job_runs/.test(q.text));
    assert.ok(ins, 'start INSERT issued');
    assert.strictEqual(ins.values[0], 'sameday-sod-refresh-0935');
    assert.strictEqual(ins.values[1], 'cron');
  } finally {
    Module._load = origLoad;
    delete require.cache[SRC];
    if (prev === undefined) delete process.env.OPENCLAW_SAMEDAY_EXEC; else process.env.OPENCLAW_SAMEDAY_EXEC = prev;
  }
});
