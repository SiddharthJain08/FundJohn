'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const k    = require(path.join(ROOT, 'src/channels/api/nav_kpis.js'));

// ── deriveTradeKpis ─────────────────────────────────────────────────────────
test('profit factor, payoff and expectancy from one aggregate row', () => {
  // 3 winners totalling +0.30 (avg +0.10), 2 losers totalling -0.10 (avg -0.05)
  const out = k.deriveTradeKpis({
    gross_win: '0.30', gross_loss: '0.10',
    avg_win: '0.10', avg_loss: '0.05',
    expectancy_pct: '0.04', n_closed: '5',
  });
  assert.equal(out.profit_factor, 3);
  assert.equal(out.payoff_ratio, 2);
  assert.equal(out.expectancy_pct, 0.04);
  assert.equal(out.avg_win_pct, 0.10);
  assert.equal(out.avg_loss_pct, 0.05);
});

test('no losers means profit factor and payoff are null, not Infinity', () => {
  const out = k.deriveTradeKpis({
    gross_win: '0.30', gross_loss: '0', avg_win: '0.10', avg_loss: null,
    expectancy_pct: '0.10', n_closed: '3',
  });
  assert.equal(out.profit_factor, null);
  assert.equal(out.payoff_ratio, null);
});

test('an empty book yields nulls throughout, never NaN', () => {
  const out = k.deriveTradeKpis({
    gross_win: null, gross_loss: null, avg_win: null, avg_loss: null,
    expectancy_pct: null, n_closed: '0',
  });
  for (const v of Object.values(out)) assert.ok(v === null, JSON.stringify(out));
});

test('deriveTradeKpis tolerates a missing row', () => {
  const out = k.deriveTradeKpis(undefined);
  assert.equal(out.profit_factor, null);
  assert.equal(out.expectancy_pct, null);
});

// ── NAV strip ───────────────────────────────────────────────────────────────
const FIXTURE_DAYS = {
  '2026-07-30': { open: 100000, high: 100000, low: 100000, close: 100000 },
  '2026-07-31': { open: 100000, high: 101000, low:  99000, close: 101000 },
  '2026-08-03': { open: 101000, high: 101500, low: 100000, close: 100500 },
  '2026-08-31': { open: 100500, high: 103000, low: 100000, close: 102510 },
  '2026-09-01': { open: 102510, high: 102600, low: 101000, close: 101000 },
  '2026-09-02': { open: 101000, high: 101000, low: 101000, close: 101000 },
};

test('navMonthlyReturns is last-close over previous-month-last-close', () => {
  const rows = k.navMonthlyReturns(FIXTURE_DAYS, { limit: 12 });
  const by = Object.fromEntries(rows.map(r => [r.month, r]));
  assert.equal(by['2026-07'].return_pct, null);          // no prior month anchor
  // Aug: 102510 / 101000 - 1 = +1.4950495…%
  assert.ok(Math.abs(by['2026-08'].return_pct - 1.4950495) < 1e-6, by['2026-08'].return_pct);
  // Sep: 101000 / 102510 - 1 = -1.4730270…%
  assert.ok(Math.abs(by['2026-09'].return_pct + 1.4730270) < 1e-6, by['2026-09'].return_pct);
  assert.equal(by['2026-08'].days, 2);
});

test('navMonthlyReturns returns at most `limit` months, newest last', () => {
  const rows = k.navMonthlyReturns(FIXTURE_DAYS, { limit: 2 });
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map(r => r.month), ['2026-08', '2026-09']);
});

test('navMonthlyReturns on an empty store is an empty array', () => {
  assert.deepEqual(k.navMonthlyReturns({}, { limit: 12 }), []);
  assert.deepEqual(k.navMonthlyReturns(null, { limit: 12 }), []);
});

test('navDayCounts splits up / down / flat sessions', () => {
  const c = k.navDayCounts(FIXTURE_DAYS);
  // 07-31 up, 08-03 down, 08-31 up, 09-01 down, 09-02 flat (first day has no prior)
  assert.equal(c.win_days, 2);
  assert.equal(c.lose_days, 2);
  assert.equal(c.flat_days, 1);
});

test('navDayCounts on an empty store is all zeroes', () => {
  assert.deepEqual(k.navDayCounts({}), { win_days: 0, lose_days: 0, flat_days: 0 });
});

test('pre-epoch NAV days must be filtered out before these helpers see them', () => {
  // The endpoint filters the store by pipeline_config.account_epoch exactly as
  // _buildCandles does (server.js:2892). This pins the arithmetic that filter
  // protects: treat everything before 2026-08-01 as the OLD account.
  const epoch = '2026-08-01';
  const filtered = Object.fromEntries(
    Object.entries(FIXTURE_DAYS).filter(([d]) => d >= epoch));

  const kept = k.navMonthlyReturns(filtered, { limit: 12 });
  assert.deepEqual(kept.map(r => r.month), ['2026-08', '2026-09']);
  // August has no in-epoch anchor, so it must render as "—", not as a number
  // computed against an old-account close.
  assert.equal(kept[0].return_pct, null);

  // Unfiltered, August IS anchored to the pre-epoch 07-31 close — the exact
  // shape of the 2026-09-08 fake-jump bug.
  const leaked = k.navMonthlyReturns(FIXTURE_DAYS, { limit: 12 })
    .find(r => r.month === '2026-08');
  assert.notEqual(leaked.return_pct, null);

  // Day counts must not span the cutover either.
  assert.deepEqual(k.navDayCounts(filtered), { win_days: 1, lose_days: 1, flat_days: 1 });
});

// ── SQL guards: the new aggregates must be scoped exactly like the old ones ──
test('TRADE_KPI_SQL carries the closed / rolled / epoch clauses', () => {
  const s = k.TRADE_KPI_SQL;
  assert.match(s, /sp\.status\s*=\s*'closed'/);
  assert.match(s, /close_reason IS DISTINCT FROM 'rolled_continuation'/);
  assert.match(s, /es\.signal_date >= \$1::date/);
  assert.match(s, /JOIN execution_signals es ON es\.id = sp\.signal_id/);
  assert.ok(!/DELETE|UPDATE|INSERT/i.test(s), 'read-only');
});

test('MAE_SQL is scoped to closed, non-rolled, in-epoch signals', () => {
  const s = k.MAE_SQL;
  assert.match(s, /sp\.status\s*=\s*'closed'/);
  assert.match(s, /close_reason IS DISTINCT FROM 'rolled_continuation'/);
  assert.match(s, /es\.signal_date >= \$1::date/);
  assert.match(s, /LEAST\(MIN\(sp\.unrealized_pnl_pct\), 0\)/);
  assert.ok(!/DELETE|UPDATE|INSERT/i.test(s), 'read-only');
});
