'use strict';
const test = require('node:test');
const assert = require('node:assert');
const pg = require.resolve('../../src/database/postgres');
let sigRows;
require.cache[pg] = { exports: { query: async (sql) => {
  if (/FROM daily_signal_summary/.test(sql)) return { rows: sigRows(sql) };
  if (/COUNT\(\*\) FILTER \(WHERE status='open'\)/.test(sql)) return { rows: [{ open_count: 0, closed_count: 0 }] };
  if (/AVG\(realized_pnl_pct\)/.test(sql)) return { rows: [{ avg_pnl: null, wins: 0, total: 0 }] };
  return { rows: [] };
} } };
const { buildDigest } = require('../../src/engine/daily-health-digest');
const today = new Date();

async function digest(rowsFn) { sigRows = rowsFn; const r = await buildDigest(today); return typeof r === 'string' ? r : JSON.stringify(r); }

test('digest line has no EV/green text when the gate is retired', async () => {
  const txt = await digest((sql) => /OFFSET 1/.test(sql)
    ? [{ n_signals: 10 }]
    : [{ run_date: today, regime: 'LOW_VOL', n_signals: 12, avg_ev: null, ev_pos: 0, high_conv_count: 0 }]);
  assert.match(txt, /Latest run \(\d{4}-\d{2}-\d{2}\): 12 signals/);
  assert.match(txt, /Δ vs prior: \+2 signals/);
  assert.doesNotMatch(txt, /NaN|avg EV|green|high-conv|quality gate/);
});

test('zero signals still raises the alarm line', async () => {
  const txt = await digest(() => [{ run_date: today, regime: 'LOW_VOL', n_signals: 0 }]);
  assert.match(txt, /\*\*0 signals\*\*/);
});
