'use strict';
// Twin of promotion_exit_hook_guard.test.js for the intraday session simulator
// (spec 2026-10-19 s6). Pins policy v2 (no bench leg) via the kill switch.
process.env.OPENCLAW_PROMOTION_BENCH_GATE = '0';
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
const base = { run_id: 'r1', total_sharpe: 1.2, total_max_dd_pct: 5, total_trades: 150 };
const intradayRun = { ...base, config_json: { exit_hook: false, intraday_trades: 120, intraday_no_bars: 3 } };
const zeroRun     = { ...base, config_json: { exit_hook: false, intraday_trades: 0 } };
const absentRun   = { ...base, config_json: { exit_hook: false } };
const sid = 'S_x', instrumentClass = 'equity';

test('intraday run is refused while OPENCLAW_INTRADAY_EXIT_LIVE is unset (both functions)', async () => {
  delete process.env.OPENCLAW_INTRADAY_EXIT_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(intradayRun, goodSleeves), sid, instrumentClass });
  assert.strictEqual(r.pass, false);
  assert.deepStrictEqual(r.failedGates, ['intraday_exit_live_disabled']);
  assert.deepStrictEqual(r.qualifyingRegimes, []);
  const q = await computeQualifyingRegimes({ dbQuery: mkQuery(intradayRun, goodSleeves), sid, instrumentClass });
  assert.deepStrictEqual(q.qualifying, []);
  assert.strictEqual(q.intraday_exit_live_disabled, true);
});

test('intraday run passes normally when OPENCLAW_INTRADAY_EXIT_LIVE=1', async () => {
  process.env.OPENCLAW_INTRADAY_EXIT_LIVE = '1';
  try {
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(intradayRun, goodSleeves), sid, instrumentClass });
    assert.strictEqual(r.pass, true);
    assert.deepStrictEqual(r.qualifyingRegimes, ['LOW_VOL']);
    const q = await computeQualifyingRegimes({ dbQuery: mkQuery(intradayRun, goodSleeves), sid, instrumentClass });
    assert.deepStrictEqual(q.qualifying, ['LOW_VOL']);
    assert.strictEqual(q.intraday_exit_live_disabled, undefined);
  } finally { delete process.env.OPENCLAW_INTRADAY_EXIT_LIVE; }
});

test('intraday_trades 0 or absent is unaffected', async () => {
  delete process.env.OPENCLAW_INTRADAY_EXIT_LIVE;
  for (const run of [zeroRun, absentRun]) {
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(run, goodSleeves), sid, instrumentClass });
    assert.strictEqual(r.pass, true);
    const q = await computeQualifyingRegimes({ dbQuery: mkQuery(run, goodSleeves), sid, instrumentClass });
    assert.deepStrictEqual(q.qualifying, ['LOW_VOL']);
  }
});

test('config_json as a JSON string is tolerated (refused when intraday, passes when not)', async () => {
  delete process.env.OPENCLAW_INTRADAY_EXIT_LIVE;
  const strRun = { ...intradayRun, config_json: JSON.stringify(intradayRun.config_json) };
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(strRun, goodSleeves), sid, instrumentClass });
  assert.deepStrictEqual(r.failedGates, ['intraday_exit_live_disabled']);
  const strZero = { ...zeroRun, config_json: JSON.stringify(zeroRun.config_json) };
  const r2 = await evaluatePromotionGate({ dbQuery: mkQuery(strZero, goodSleeves), sid, instrumentClass });
  assert.strictEqual(r2.pass, true);
});

test('a config_json string that will not parse fails CLOSED', async () => {
  delete process.env.OPENCLAW_INTRADAY_EXIT_LIVE;
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const broken = { ...base, config_json: '{"intraday_trades": 12' };
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(broken, goodSleeves), sid, instrumentClass });
  assert.strictEqual(r.pass, false);
  // the exit-hook guard runs first and also fails closed on a broken config
  assert.ok(r.failedGates.length === 1 && /_live_disabled$/.test(r.failedGates[0]));
  // with the hook guard satisfied, the intraday guard must still refuse it
  process.env.OPENCLAW_EXIT_HOOK_LIVE = '1';
  try {
    const r2 = await evaluatePromotionGate({ dbQuery: mkQuery(broken, goodSleeves), sid, instrumentClass });
    assert.deepStrictEqual(r2.failedGates, ['intraday_exit_live_disabled']);
    const q = await computeQualifyingRegimes({ dbQuery: mkQuery(broken, goodSleeves), sid, instrumentClass });
    assert.strictEqual(q.intraday_exit_live_disabled, true);
  } finally { delete process.env.OPENCLAW_EXIT_HOOK_LIVE; }
});

test('force bypasses the guard', async () => {
  delete process.env.OPENCLAW_INTRADAY_EXIT_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(intradayRun, goodSleeves), sid, instrumentClass, force: true });
  assert.strictEqual(r.pass, true);
});
