/* tests/engine/test_sameday_exec_crons.test.js — same-day exec cron gating
 * (2026-07-29 pivot). Mocks node-cron to capture registrations under
 * OPENCLAW_SAMEDAY_EXEC, mirroring test_close_exec_crons.test.js. */
const test = require('node:test');
const assert = require('node:assert');
const Module = require('module');

const GATES = ['OPENCLAW_SAMEDAY_EXEC', 'OPENCLAW_EOD_SIGNAL_REGISTER',
               'OPENCLAW_CLOSE_EXEC_LIVE', 'OPENCLAW_EOD_PREMARKET_GATE',
               'OPENCLAW_EOD_RECONCILE'];

function captureCronExprs(env) {
  const exprs = [];
  const cronMock = { schedule: (expr) => { exprs.push(expr); return { stop() {} }; } };
  const origLoad = Module._load;
  Module._load = function (request, ...rest) {
    if (request === 'node-cron') return cronMock;
    return origLoad.call(this, request, ...rest);
  };
  const csPath = require.resolve('../../src/engine/cron-schedule');
  delete require.cache[csPath];
  const prev = {};
  for (const g of GATES) { prev[g] = process.env[g]; delete process.env[g]; }
  Object.assign(process.env, env);
  let thrown = null;
  try {
    const cs = require('../../src/engine/cron-schedule');
    try { cs.start({ bots: {} }, () => 'id', async () => {}); } catch (e) { thrown = e; }
  } finally {
    Module._load = origLoad;
    delete require.cache[csPath];
    for (const g of GATES) {
      if (prev[g] === undefined) delete process.env[g];
      else process.env[g] = prev[g];
    }
  }
  return { exprs, thrown };
}

test('sameday ON registers compute/execute/eod-collect/SOD + premarket trio', () => {
  const { exprs, thrown } = captureCronExprs({
    OPENCLAW_SAMEDAY_EXEC: '1',
    OPENCLAW_EOD_PREMARKET_GATE: '1',
    OPENCLAW_EOD_RECONCILE: '1',
  });
  assert.ok(!(thrown && /mutually exclusive/.test(thrown.message)), 'no exclusivity throw');
  assert.ok(exprs.includes('0 15 * * 1-5'), '15:00 compute');
  assert.ok(exprs.includes('55 15 * * 1-5'), '15:55 execute');
  assert.ok(exprs.includes('15 16 * * 1-5'), '16:15 EOD collect');
  assert.ok(exprs.includes('35 9 * * 1-5'), '9:35 SOD refresh');
  assert.ok(exprs.includes('15 9 * * 1-5'), '9:15 premarket gate survives the mode');
  assert.ok(exprs.includes('25 9 * * 1-5'), '9:25 reconcile survives the mode');
  assert.ok(exprs.includes('32 9 * * 1-5'), '9:32 OPG sweep survives the mode');
  assert.ok(!exprs.includes('0 10 * * 1-5'), 'legacy 10am cycle suppressed');
  const n1555 = exprs.filter((e) => e === '55 15 * * 1-5').length;
  assert.strictEqual(n1555, 1, 'exactly ONE 15:55 executor registered');
});

test('sameday OFF registers none of the sameday crons', () => {
  const { exprs } = captureCronExprs({});
  assert.ok(!exprs.includes('0 15 * * 1-5'), 'no 15:00 compute when OFF');
});

test('sameday + EOD mode together throws mutual exclusion', () => {
  const { thrown } = captureCronExprs({
    OPENCLAW_SAMEDAY_EXEC: '1',
    OPENCLAW_EOD_SIGNAL_REGISTER: '1',
  });
  assert.ok(thrown && /mutually exclusive/.test(thrown.message),
            'expected exclusivity throw, got: ' + (thrown && thrown.message));
});
