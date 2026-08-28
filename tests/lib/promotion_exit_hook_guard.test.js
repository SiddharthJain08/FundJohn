'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { evaluatePromotionGate, computeQualifyingRegimes } = require('../../src/lib/promotion_service');

function mkQuery(runRow, regimeRows) {
  return async (sql) => {
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_backtest_regimes/.test(sql)) return { rows: regimeRows || [] };
    throw new Error(`unexpected query: ${sql}`);
  };
}
const goodSleeves = [{ regime_state: 'LOW_VOL', sharpe: 1.2, trade_count: 150, max_dd_pct: 5, calmar: 2, benchmark_sharpe: null }];
const hookRun  = { run_id: 'r1', total_sharpe: 1.2, total_max_dd_pct: 5, total_trades: 150, config_json: { exit_hook: true, hook_exits: 40 } };
const plainRun = { run_id: 'r2', total_sharpe: 1.2, total_max_dd_pct: 5, total_trades: 150, config_json: { exit_hook: false } };

test('exit_hook run is refused while OPENCLAW_EXIT_HOOK_LIVE is unset', async () => {
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
  assert.strictEqual(r.pass, false);
  assert.deepStrictEqual(r.failedGates, ['exit_hook_live_disabled']);
  const q = await computeQualifyingRegimes({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
  assert.deepStrictEqual(q.qualifying, []);
  assert.strictEqual(q.exit_hook_live_disabled, true);
});

test('exit_hook run passes normally when OPENCLAW_EXIT_HOOK_LIVE=1', async () => {
  process.env.OPENCLAW_EXIT_HOOK_LIVE = '1';
  try {
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
    assert.strictEqual(r.pass, true);
    assert.deepStrictEqual(r.qualifyingRegimes, ['LOW_VOL']);
  } finally { delete process.env.OPENCLAW_EXIT_HOOK_LIVE; }
});

test('non-hook run is unaffected; config_json as a JSON string is tolerated', async () => {
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(plainRun, goodSleeves), sid: 'S_y', instrumentClass: 'equity' });
  assert.strictEqual(r.pass, true);
  const strRun = { ...hookRun, config_json: JSON.stringify(hookRun.config_json) };
  const r2 = await evaluatePromotionGate({ dbQuery: mkQuery(strRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
  assert.deepStrictEqual(r2.failedGates, ['exit_hook_live_disabled']);
});

test('a config_json string that will not parse fails CLOSED', async () => {
  // Every other unknown in this gate fails closed (no_backtest, no sleeves).
  // An unparseable config_json must not be the one path that silently promotes.
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const brokenRun = { ...hookRun, config_json: '{"exit_hook": tru' };
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(brokenRun, goodSleeves), sid: 'S_z', instrumentClass: 'equity' });
  assert.strictEqual(r.pass, false);
  assert.deepStrictEqual(r.failedGates, ['exit_hook_live_disabled']);
  const q = await computeQualifyingRegimes({ dbQuery: mkQuery(brokenRun, goodSleeves), sid: 'S_z', instrumentClass: 'equity' });
  assert.deepStrictEqual(q.qualifying, []);
  assert.strictEqual(q.exit_hook_live_disabled, true);
});

test('force bypasses the guard', async () => {
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity', force: true });
  assert.strictEqual(r.pass, true);
});
