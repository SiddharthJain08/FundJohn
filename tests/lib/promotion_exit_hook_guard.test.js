'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { evaluatePromotionGate, computeQualifyingRegimes } = require('../../src/lib/promotion_service');

function mkQuery(runRow, regimeRows, holdCapMax) {
  return async (sql) => {
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_backtest_regimes/.test(sql)) return { rows: regimeRows || [] };
    // I5: the live hold cap the exit-hook time stop will actually apply.
    if (/strategy_regime_params/.test(sql)) return { rows: [{ m: holdCapMax != null ? holdCapMax : null }] };
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

// ── I5 (final review 2026-08-28): hold-cap mismatch ──────────────────────────
// An exit_hook run measured at a different max_hold_days than the live time
// stop will apply is not the strategy that was judged. X1's run was pinned
// --max-hold-days 30 while the live resolver returns 21.
const hookRun30 = { ...hookRun, config_json: { exit_hook: true, hook_exits: 40, max_hold_days: 30 } };

test('exit_hook run whose max_hold_days matches the live cap passes', async () => {
  process.env.OPENCLAW_EXIT_HOOK_LIVE = '1';
  process.env.OPENCLAW_BACKTEST_COUPLED_RECS = '1';
  try {
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun30, goodSleeves, 30), sid: 'S_x', instrumentClass: 'equity' });
    assert.strictEqual(r.pass, true);
    assert.deepStrictEqual(r.qualifyingRegimes, ['LOW_VOL']);
    const q = await computeQualifyingRegimes({ dbQuery: mkQuery(hookRun30, goodSleeves, 30), sid: 'S_x', instrumentClass: 'equity' });
    assert.deepStrictEqual(q.qualifying, ['LOW_VOL']);
    assert.strictEqual(q.exit_hook_hold_cap_mismatch, undefined);
  } finally { delete process.env.OPENCLAW_EXIT_HOOK_LIVE; delete process.env.OPENCLAW_BACKTEST_COUPLED_RECS; }
});

test('exit_hook run measured at 30 is refused when the live cap is 21', async () => {
  process.env.OPENCLAW_EXIT_HOOK_LIVE = '1';
  process.env.OPENCLAW_BACKTEST_COUPLED_RECS = '1';
  try {
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun30, goodSleeves, 21), sid: 'S_x', instrumentClass: 'equity' });
    assert.strictEqual(r.pass, false);
    assert.deepStrictEqual(r.failedGates, ['exit_hook_hold_cap_mismatch']);
    const q = await computeQualifyingRegimes({ dbQuery: mkQuery(hookRun30, goodSleeves, 21), sid: 'S_x', instrumentClass: 'equity' });
    assert.deepStrictEqual(q.qualifying, []);
    assert.strictEqual(q.exit_hook_hold_cap_mismatch, true);
  } finally { delete process.env.OPENCLAW_EXIT_HOOK_LIVE; delete process.env.OPENCLAW_BACKTEST_COUPLED_RECS; }
});

test('COUPLED_RECS unset => live cap is the resolver default 21, so a 30-run mismatches', async () => {
  // The X1 case exactly: no strategy_regime_params row is even consulted
  // because the coupling gate is off; the live time stop uses 21.
  process.env.OPENCLAW_EXIT_HOOK_LIVE = '1';
  delete process.env.OPENCLAW_BACKTEST_COUPLED_RECS;
  try {
    // mkQuery would THROW on a strategy_regime_params query — proving the
    // gate-off path never issues one.
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun30, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
    assert.strictEqual(r.pass, false);
    assert.deepStrictEqual(r.failedGates, ['exit_hook_hold_cap_mismatch']);
  } finally { delete process.env.OPENCLAW_EXIT_HOOK_LIVE; }
});

test('a run with no recorded max_hold_days, and any non-hook run, are unaffected', async () => {
  process.env.OPENCLAW_EXIT_HOOK_LIVE = '1';
  process.env.OPENCLAW_BACKTEST_COUPLED_RECS = '1';
  try {
    // hookRun records no max_hold_days -> nothing to compare, gate stays out
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun, goodSleeves, 21), sid: 'S_x', instrumentClass: 'equity' });
    assert.strictEqual(r.pass, true);
    // non-hook run at a mismatching cap is not this gate's business
    const plain30 = { ...plainRun, config_json: { exit_hook: false, max_hold_days: 30 } };
    const r2 = await evaluatePromotionGate({ dbQuery: mkQuery(plain30, goodSleeves, 21), sid: 'S_y', instrumentClass: 'equity' });
    assert.strictEqual(r2.pass, true);
  } finally { delete process.env.OPENCLAW_EXIT_HOOK_LIVE; delete process.env.OPENCLAW_BACKTEST_COUPLED_RECS; }
});
