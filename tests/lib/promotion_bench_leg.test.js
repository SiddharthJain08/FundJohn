// tests/lib/promotion_no_benchmark_leg.test.js
// D1 (2026-08-29): the candidate->live sleeve judge must not read benchmark_sharpe.
const test = require('node:test');
const assert = require('node:assert/strict');
const ps = require('../../src/lib/promotion_service');

test('a sleeve below the market still qualifies', () => {
  const thr = ps.getPromotionThreshold('equity');
  const fails = ps.judgeRegimeSleeve(
    { sharpe: 1.2, trade_count: 150, max_dd_pct: 5, calmar: 2, benchmark_sharpe: 2.03 },
    thr, { instrumentClass: 'equity', sid: 's', regime: 'LOW_VOL' });
  assert.deepEqual(fails, []);
});

test('benchmark exports are gone', () => {
  assert.equal(ps.getMinExcessSharpeVsBenchmark, undefined);
  assert.equal(ps.MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS, undefined);
});

test('legacy legs unchanged', () => {
  const thr = ps.getPromotionThreshold('equity');
  assert.deepEqual(ps.judgeRegimeSleeve({ sharpe: 0, trade_count: 150, max_dd_pct: 5 }, thr), ['sharpe']);
  assert.deepEqual(ps.judgeRegimeSleeve({ sharpe: 1, trade_count: 10, max_dd_pct: 5 }, thr), ['trades']);
  assert.deepEqual(ps.judgeRegimeSleeve(null, thr), ['no_backtest']);
});
