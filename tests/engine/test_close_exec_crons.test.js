/* tests/test_close_exec_crons.test.js — close-exec cron gating.
 * Mocks node-cron to capture which cron expressions cron-schedule.start()
 * registers, under OPENCLAW_CLOSE_EXEC_LIVE OFF vs ON. */
const test = require('node:test');
const assert = require('node:assert');
const Module = require('module');
const path = require('path');

function captureCronExprs(gateOn) {
  const exprs = [];
  const cronMock = { schedule: (expr) => { exprs.push(expr); return { stop() {} }; } };
  const origLoad = Module._load;
  Module._load = function (request, ...rest) {
    if (request === 'node-cron') return cronMock;
    return origLoad.call(this, request, ...rest);
  };
  const csPath = require.resolve('../../src/engine/cron-schedule');
  delete require.cache[csPath];
  const prev = process.env.OPENCLAW_CLOSE_EXEC_LIVE;
  process.env.OPENCLAW_CLOSE_EXEC_LIVE = gateOn ? '1' : '0';
  try {
    const cs = require('../../src/engine/cron-schedule');
    // start() registers crons; later setup may throw on stubbed deps — the
    // schedules we care about are registered before any such throw.
    try { cs.start({ bots: {} }, () => 'id', async () => {}); } catch (_) { /* ignore late deps */ }
  } finally {
    Module._load = origLoad;
    delete require.cache[csPath];
    if (prev === undefined) delete process.env.OPENCLAW_CLOSE_EXEC_LIVE;
    else process.env.OPENCLAW_CLOSE_EXEC_LIVE = prev;
  }
  return exprs;
}

test('gate OFF registers the 10:00 cycle, not the close-exec crons', () => {
  const exprs = captureCronExprs(false);
  assert.ok(exprs.includes('0 10 * * 1-5'), 'expected 10:00 cron when gate OFF');
  assert.ok(!exprs.includes('10 15 * * 1-5'), 'no compute cron when gate OFF');
  assert.ok(!exprs.includes('55 15 * * 1-5'), 'no execute cron when gate OFF');
  assert.ok(!exprs.includes('35 9 * * 1-5'), 'no SOD cron when gate OFF');
});

test('gate ON registers compute/execute/SOD, not the 10:00 cycle', () => {
  const exprs = captureCronExprs(true);
  assert.ok(exprs.includes('10 15 * * 1-5'), 'expected compute cron when gate ON');
  assert.ok(exprs.includes('55 15 * * 1-5'), 'expected execute cron when gate ON');
  assert.ok(exprs.includes('35 9 * * 1-5'), 'expected SOD cron when gate ON');
  assert.ok(!exprs.includes('0 10 * * 1-5'), 'no 10:00 cron when gate ON');
});
