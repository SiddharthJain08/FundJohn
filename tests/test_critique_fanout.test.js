'use strict';

const { test, mock } = require('node:test');
const assert         = require('node:assert/strict');
const path           = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const mod  = require(path.join(ROOT, 'src/agent/curators/critique_fanout.js'));

const FAKE_MEMO = {
  id: 7,
  strategy_id: 'S9_dual_momentum',
  memo_date: '2026-05-20',
  markdown_body: '## Recommendation\nSize 3.0% NAV.',
  recommendations: { recommended_size_pct: 0.030 },
};

const FAKE_TRADES = [
  { ticker: 'AAPL', entry_date: '2026-05-13', exit_date: '2026-05-19', realized_pnl_pct: 1.2 },
  { ticker: 'MSFT', entry_date: '2026-05-12', exit_date: '2026-05-18', realized_pnl_pct: -2.4 },
];

test('runOne invokes 3 critics in parallel and persists 3 rows', async () => {
  let calls = [];
  const fakeRunner = async (criticRole, _prompt) => {
    calls.push(criticRole);
    return JSON.stringify({
      critique_text: `mock ${criticRole} critique`,
      cited_metrics: { proposed_size_pct_delta: 0.0 },
    });
  };
  let persisted = [];
  const fakeWriter = async (row) => persisted.push(row);
  mod._setRunnerForTests(fakeRunner);
  mod._setWriterForTests(fakeWriter);

  await mod.runOne(FAKE_MEMO, FAKE_TRADES, [], { weekOf: '2026-05-20' });

  assert.equal(calls.length, 3);
  assert.deepEqual(calls.sort(), ['aggressive', 'conservative', 'neutral']);
  assert.equal(persisted.length, 3);
  for (const role of ['aggressive', 'conservative', 'neutral']) {
    assert.ok(persisted.some(p => p.critic_role === role),
              `should persist row for ${role}`);
  }
  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('runOne tolerates one critic failure — persists only successful rows', async () => {
  const fakeRunner = async (criticRole) => {
    if (criticRole === 'conservative') throw new Error('LLM timeout');
    return JSON.stringify({ critique_text: `mock ${criticRole}`, cited_metrics: {} });
  };
  let persisted = [];
  mod._setRunnerForTests(fakeRunner);
  mod._setWriterForTests(async (row) => persisted.push(row));

  await mod.runOne(FAKE_MEMO, FAKE_TRADES, [], { weekOf: '2026-05-20' });
  assert.equal(persisted.length, 2);
  const roles = persisted.map(p => p.critic_role).sort();
  assert.deepEqual(roles, ['aggressive', 'neutral']);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('runOne handles all 3 critics failing — persists nothing, returns failure info', async () => {
  mod._setRunnerForTests(async () => { throw new Error('LLM down'); });
  let persisted = [];
  mod._setWriterForTests(async (row) => persisted.push(row));

  const result = await mod.runOne(FAKE_MEMO, FAKE_TRADES, [], { weekOf: '2026-05-20' });
  assert.equal(persisted.length, 0);
  assert.equal(result.success_count, 0);
  assert.equal(result.failure_count, 3);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});
