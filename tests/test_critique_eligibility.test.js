'use strict';

const { test, mock } = require('node:test');
const assert         = require('node:assert/strict');
const path           = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const mod  = require(path.join(ROOT, 'src/agent/curators/_critique_eligibility.js'));

test('filter returns sorted strategy IDs with ≥1 closed trade in last 7 days', async () => {
  // Stub the internal _query implementation
  const fakeRows = [
    { strategy_id: 'S9_dual_momentum' },
    { strategy_id: 'S12_insider' },
    { strategy_id: 'S5_max_pain' },
  ];
  mod._setQueryForTests(async (sql, params) => {
    // Verify SQL shape includes signal_pnl + 7d window + IS NOT NULL exit_date
    assert.ok(sql.includes('signal_pnl'),                'should query signal_pnl');
    assert.ok(sql.includes('exit_date IS NOT NULL'),      'should filter null exit_date');
    assert.ok(sql.includes("INTERVAL '7 days'"),          'should use 7-day window');
    return { rows: fakeRows };
  });
  const result = await mod.filter();
  assert.deepEqual(result, ['S12_insider', 'S5_max_pain', 'S9_dual_momentum']);
  mod._setQueryForTests(null);
});

test('filter returns empty array on quiet week', async () => {
  mod._setQueryForTests(async () => ({ rows: [] }));
  const result = await mod.filter();
  assert.deepEqual(result, []);
  mod._setQueryForTests(null);
});

test('filter propagates DB errors', async () => {
  mod._setQueryForTests(async () => { throw new Error('connection refused'); });
  await assert.rejects(() => mod.filter(), /connection refused/);
  mod._setQueryForTests(null);
});
