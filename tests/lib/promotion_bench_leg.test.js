// tests/lib/promotion_bench_leg.test.js
// Spec 2026-09-25-activation-bench-relative §9 Amendment 2 (operator 2026-10-04):
// the candidate->live gate also requires sharpe[r] >= bench[r] + excess.
// Supersedes the 2026-08-29 D1 "no bench leg" pin for the PROMOTION gate.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const ps = require('../../src/lib/promotion_service');

const BENCH = { LOW_VOL: 0.9457, TRANSITIONING: 0.4409, HIGH_VOL: 0.5326, CRISIS: 1.575 };
const RUN = { run_id: 7, total_sharpe: 1, total_max_dd_pct: 10, total_trades: 500, config_json: {} };
const SL = (regime, sharpe, trades = 200, dd = 5) => ({ regime_state: regime, sharpe, trade_count: trades, max_dd_pct: dd, calmar: 2 });

// opts: bench (object|string|undefined=no row), excess, throwConfig, registryRows, throwRegistry
function mk(sleeves, opts = {}) {
  const calls = [];
  const q = async (sql) => {
    calls.push(sql);
    if (/strategy_backtest_runs/.test(sql)) return { rows: [RUN] };
    if (/universe_shrink_metrics/.test(sql)) {
      if (opts.throwShrink) throw new Error('shrink down');
      return { rows: opts.shrink || [] };
    }
    if (/strategy_backtest_regimes/.test(sql)) return { rows: opts.noRegimeRows ? [] : sleeves };
    if (/pipeline_config/.test(sql)) {
      if (opts.throwConfig) throw new Error('db down');
      const rows = [];
      if (opts.bench !== undefined) rows.push({ key: 'strategy_activation_bench_sharpe', value: typeof opts.bench === 'string' ? opts.bench : JSON.stringify(opts.bench) });
      if (opts.excess !== undefined) rows.push({ key: 'strategy_activation_excess_sharpe', value: String(opts.excess) });
      return { rows };
    }
    if (/strategy_registry/.test(sql)) {
      if (opts.throwRegistry) throw new Error('registry down');
      return { rows: opts.sleeve ? [{ '?column?': 1 }] : [] };
    }
    throw new Error(`unexpected query: ${sql}`);
  };
  q.calls = calls;
  return q;
}
async function withWarns(fn) {
  const warns = []; const orig = console.warn;
  console.warn = (...a) => warns.push(a.join(' '));
  try { return { r: await fn(), warns }; } finally { console.warn = orig; }
}
const auto = (q) => ps.computeQualifyingRegimes({ dbQuery: q, sid: 'S_x', instrumentClass: 'equity' });
const gate = (q, extra = {}) => ps.evaluatePromotionGate({ dbQuery: q, sid: 'S_x', instrumentClass: 'equity', force: false, ...extra });
test.beforeEach(() => { delete process.env.OPENCLAW_PROMOTION_BENCH_GATE; });

test('judgeRegimeSleeve: legacy legs unchanged without ctx.benchThreshold', () => {
  const thr = ps.getPromotionThreshold('equity');
  assert.deepEqual(ps.judgeRegimeSleeve({ sharpe: 0.1, trade_count: 150, max_dd_pct: 5, benchmark_sharpe: 9 }, thr, {}), []);
  assert.deepEqual(ps.judgeRegimeSleeve({ sharpe: 0, trade_count: 150, max_dd_pct: 5 }, thr), ['sharpe']);
  assert.deepEqual(ps.judgeRegimeSleeve({ sharpe: 1, trade_count: 10, max_dd_pct: 5 }, thr), ['trades']);
  assert.deepEqual(ps.judgeRegimeSleeve(null, thr, { benchThreshold: 1 }), ['no_backtest']);
  assert.equal(ps.getMinExcessSharpeVsBenchmark, undefined);
});

test('judgeRegimeSleeve: bench leg is >= at the boundary', () => {
  const thr = ps.getPromotionThreshold('equity');
  const row = (s) => ({ sharpe: s, trade_count: 150, max_dd_pct: 5 });
  assert.deepEqual(ps.judgeRegimeSleeve(row(0.9457), thr, { benchThreshold: 0.9457 }), []);
  assert.deepEqual(ps.judgeRegimeSleeve(row(0.9456), thr, { benchThreshold: 0.9457 }), ['bench']);
});

test('auto path: boundary qualifies, just below fails with bench; diag carries bench + threshold', async () => {
  const { r } = await withWarns(() => auto(mk([SL('LOW_VOL', 0.9457), SL('HIGH_VOL', 0.5325)], { bench: BENCH })));
  assert.deepEqual(r.qualifying, ['LOW_VOL']);
  assert.deepEqual(r.diag.HIGH_VOL.failed, ['bench']);
  assert.equal(r.diag.HIGH_VOL.bench, 0.5326);
  assert.equal(r.diag.HIGH_VOL.threshold, 0.5326);
  assert.equal(r.diag.LOW_VOL.threshold, 0.9457);
});

test('excess is added to the threshold (no clamp, as get_activation_excess)', async () => {
  let r = (await withWarns(() => auto(mk([SL('LOW_VOL', 1.2457)], { bench: BENCH, excess: 0.3 })))).r;
  assert.deepEqual(r.qualifying, ['LOW_VOL']);          // 0.9457 + 0.3, boundary
  assert.equal(r.diag.LOW_VOL.threshold, 1.2457);
  r = (await withWarns(() => auto(mk([SL('LOW_VOL', 1.2456)], { bench: BENCH, excess: 0.3 })))).r;
  assert.deepEqual(r.diag.LOW_VOL.failed, ['bench']);
  r = (await withWarns(() => auto(mk([SL('LOW_VOL', 1.0)], { bench: BENCH, excess: 99 })))).r;
  assert.equal(r.diag.LOW_VOL.threshold, 99.9457);      // unbounded, like the assigner
  r = (await withWarns(() => auto(mk([SL('LOW_VOL', 0.05)], { bench: BENCH, excess: -50 })))).r;
  assert.equal(r.diag.LOW_VOL.threshold, -49.0543);
  r = (await withWarns(() => auto(mk([SL('LOW_VOL', 0.9457)], { bench: BENCH, excess: 'abc' })))).r;
  assert.equal(r.diag.LOW_VOL.threshold, 0.9457);       // malformed excess => 0
  assert.deepEqual(r.qualifying, ['LOW_VOL']);
});

test('excess mirrors get_activation_excess: no 0.05 snapping; empty/NaN/abc => 0', async () => {
  let r = (await withWarns(() => auto(mk([SL('LOW_VOL', 1.0157)], { bench: BENCH, excess: '0.07' })))).r;
  assert.equal(r.diag.LOW_VOL.threshold, 1.0157);       // round(0.9457 + 0.07, 10), not snapped to 0.05
  assert.deepEqual(r.qualifying, ['LOW_VOL']);
  for (const bad of ['abc', '', '  ', 'NaN', 'Infinity']) {
    r = (await withWarns(() => auto(mk([SL('LOW_VOL', 0.9457)], { bench: BENCH, excess: bad })))).r;
    assert.equal(r.diag.LOW_VOL.threshold, 0.9457, bad);
  }
});

const FALLBACK_CASES = {
  'missing row':        { bench: undefined },
  'malformed JSON':     { bench: '{not json' },
  'missing regime key': { bench: { LOW_VOL: 0.1, TRANSITIONING: 0.1, HIGH_VOL: 0.1 } },       // CRISIS absent
  'non-finite value':   { bench: '{"LOW_VOL":0.1,"TRANSITIONING":0.1,"HIGH_VOL":0.1,"CRISIS":null}' },
  'thrown dbQuery':     { throwConfig: true },
};
for (const [name, opts] of Object.entries(FALLBACK_CASES)) {
  test(`fallback 0.5 + exactly one warn: ${name}`, async () => {
    const { r, warns } = await withWarns(() => auto(mk([SL('CRISIS', 0.5), SL('HIGH_VOL', 0.49)], opts)));
    assert.equal(warns.length, 1, warns.join('|'));
    assert.match(warns[0], /^\[promotion_service\]/);
    const partial = name === 'missing regime key' || name === 'non-finite value';   // HIGH_VOL bench is a real 0.1 there
    assert.deepEqual(r.qualifying, partial ? ['HIGH_VOL', 'CRISIS'] : ['CRISIS']);   // CRISIS 0.5 >= 0.5; never fail-open to 0
    assert.equal(r.diag.CRISIS.bench, 0.5);
    if (!partial) assert.deepEqual(r.diag.HIGH_VOL.failed, ['bench']);
  });
}

test('bench[r] absent for one regime only falls back for that regime', async () => {
  const { r, warns } = await withWarns(() => auto(mk([SL('LOW_VOL', 0.2), SL('CRISIS', 0.6)],
    { bench: { LOW_VOL: 0.1, TRANSITIONING: 0.1, HIGH_VOL: 0.1 } })));
  assert.equal(warns.length, 1);
  assert.equal(r.diag.LOW_VOL.bench, 0.1);
  assert.equal(r.diag.CRISIS.bench, 0.5);
  assert.deepEqual(r.qualifying, ['LOW_VOL', 'CRISIS']);
});

test('a healthy bench vector emits no warn and exactly one config query', async () => {
  const q = mk([SL('LOW_VOL', 2), SL('CRISIS', 2)], { bench: BENCH });
  const { warns } = await withWarns(() => auto(q));
  assert.equal(warns.length, 0);
  assert.equal(q.calls.filter(s => /pipeline_config/.test(s)).length, 1);
});

test('named-regimes path emits bench:<REGIME>', async () => {
  const q = mk([SL('LOW_VOL', 0.5), SL('CRISIS', 2), SL('HIGH_VOL', 0.6)], { bench: BENCH });
  const { r, warns } = await withWarns(() => gate(q, { eligibleRegimes: ['LOW_VOL', 'CRISIS', 'HIGH_VOL'] }));
  assert.equal(r.pass, false);
  assert.deepEqual(r.failedGates, ['bench:LOW_VOL']);
  assert.deepEqual(r.qualifyingRegimes, ['CRISIS', 'HIGH_VOL']);
  assert.equal(warns.length, 0);
  assert.equal(q.calls.filter(s => /pipeline_config/.test(s)).length, 1);
});

test('auto gate path: bench failures listed after no_qualifying_regime; passes on the boundary', async () => {
  let r = (await withWarns(() => gate(mk([SL('LOW_VOL', 0.9)], { bench: BENCH })))).r;
  assert.deepEqual(r.failedGates, ['no_qualifying_regime', 'bench:LOW_VOL']);
  r = (await withWarns(() => gate(mk([SL('LOW_VOL', 0.9457)], { bench: BENCH })))).r;
  assert.equal(r.pass, true);
  assert.deepEqual(r.qualifyingRegimes, ['LOW_VOL']);
});

test('force bypasses the bench leg and issues no query', async () => {
  const q = mk([SL('LOW_VOL', 0.01)], { bench: BENCH });
  const r = await gate(q, { force: true });
  assert.equal(r.pass, true);
  assert.equal(q.calls.length, 0);
});

test('benchmark sleeve is exempt (no bench leg, no config query)', async () => {
  const q = mk([SL('CRISIS', 0.1)], { bench: BENCH, sleeve: true });
  const { r } = await withWarns(() => auto(q));
  assert.deepEqual(r.qualifying, ['CRISIS']);
  assert.equal(r.diag.CRISIS.bench, undefined);
  assert.equal(q.calls.filter(s => /pipeline_config/.test(s)).length, 0);
});

test('failed benchmark-sleeve lookup fails closed (not exempt)', async () => {
  const { r } = await withWarns(() => auto(mk([SL('CRISIS', 0.1)], { bench: BENCH, throwRegistry: true })));
  assert.deepEqual(r.qualifying, []);
  assert.deepEqual(r.diag.CRISIS.failed, ['bench']);
});

test('kill switch =0: v2 exactly - same sets, failedGates, diag keys, no config/registry query', async () => {
  process.env.OPENCLAW_PROMOTION_BENCH_GATE = '0';
  try {
    const sleeves = [SL('CRISIS', 0.827, 860), SL('HIGH_VOL', -0.503), SL('LOW_VOL', 0.01)];
    const q = mk(sleeves, { bench: BENCH });
    const a = await auto(q);
    assert.deepEqual(a.qualifying, ['LOW_VOL', 'CRISIS'].sort((x, y) => ps.CANONICAL_REGIMES.indexOf(x) - ps.CANONICAL_REGIMES.indexOf(y)));
    assert.deepEqual(Object.keys(a.diag.CRISIS), ['sharpe', 'trade_count', 'max_dd_pct', 'failed']);
    const g = await gate(q, { eligibleRegimes: ['CRISIS', 'HIGH_VOL'] });
    assert.deepEqual(g.failedGates, ['sharpe:HIGH_VOL']);
    const g2 = await gate(mk([SL('HIGH_VOL', -1)], { bench: BENCH }));
    assert.deepEqual(g2.failedGates, ['no_qualifying_regime', 'sharpe:HIGH_VOL']);
    assert.equal(q.calls.filter(s => /pipeline_config|strategy_registry/.test(s)).length, 0);
  } finally { delete process.env.OPENCLAW_PROMOTION_BENCH_GATE; }
});

test('any other kill-switch value keeps the gate ON', async () => {
  process.env.OPENCLAW_PROMOTION_BENCH_GATE = '1';
  try {
    const { r } = await withWarns(() => auto(mk([SL('CRISIS', 0.8)], { bench: BENCH })));
    assert.deepEqual(r.qualifying, []);
  } finally { delete process.env.OPENCLAW_PROMOTION_BENCH_GATE; }
});

test('sparse-CCA fixture: no regime clears the bench => no_qualifying_regime (422 path)', async () => {
  const sleeves = [SL('CRISIS', 0.827, 860), SL('HIGH_VOL', -0.503), SL('LOW_VOL', -0.329), SL('TRANSITIONING', 0.013)];
  const { r: q } = await withWarns(() => auto(mk(sleeves, { bench: BENCH, excess: 0 })));
  assert.deepEqual(q.qualifying, []);
  assert.deepEqual(q.diag.CRISIS.failed, ['bench']);
  const { r: g } = await withWarns(() => gate(mk(sleeves, { bench: BENCH, excess: 0 })));
  assert.equal(g.pass, false);
  assert.equal(g.failedGates[0], 'no_qualifying_regime');
  assert.ok(g.failedGates.includes('bench:CRISIS'));
});

// ── MAJOR-1: the chosen universe-shrink tier is judged, like the assigner ──
test('chosen shrink-tier sleeves win over strategy_backtest_regimes', async () => {
  const q = mk([SL('CRISIS', 3)], { bench: BENCH, shrink: [SL('CRISIS', 0.8)] });
  const { r } = await withWarns(() => auto(q));
  assert.deepEqual(r.qualifying, []);                   // full sleeve would pass; chosen tier does not
  assert.deepEqual(r.diag.CRISIS.failed, ['bench']);
  assert.ok(q.calls.some(s => /universe_shrink_metrics[\s\S]*chosen[\s\S]*<> 'TOTAL'/.test(s)));
  assert.equal(q.calls.filter(s => /FROM strategy_backtest_regimes/.test(s)).length, 0);
});
test('empty shrink => falls back to strategy_backtest_regimes', async () => {
  const { r } = await withWarns(() => auto(mk([SL('CRISIS', 3)], { bench: BENCH, shrink: [] })));
  assert.deepEqual(r.qualifying, ['CRISIS']);
});
test('shrink query throws => fallback, gate not failed on the lookup alone', async () => {
  const { r } = await withWarns(() => auto(mk([SL('CRISIS', 3)], { bench: BENCH, throwShrink: true })));
  assert.deepEqual(r.qualifying, ['CRISIS']);
});
test('kill switch issues no universe_shrink_metrics query', async () => {
  process.env.OPENCLAW_PROMOTION_BENCH_GATE = '0';
  try {
    const q = mk([SL('CRISIS', 3)], { bench: BENCH, shrink: [SL('CRISIS', 0.8)] });
    const r = await auto(q);
    assert.deepEqual(r.qualifying, ['CRISIS']);
    assert.equal(q.calls.filter(s => /universe_shrink_metrics/.test(s)).length, 0);
  } finally { delete process.env.OPENCLAW_PROMOTION_BENCH_GATE; }
});

// ── MAJOR-2 / NIT-7: no sleeves => never promoted with the gate ON ──────────
test('no sleeves (empty) => no_qualifying_regime; kill switch keeps legacy total-window pass; no bench load', async () => {
  const q = mk([], { bench: BENCH, noRegimeRows: true });
  const { r, warns } = await withWarns(() => gate(q));
  assert.equal(r.pass, false);
  assert.deepEqual(r.failedGates, ['no_qualifying_regime']);
  assert.equal(q.calls.filter(s => /pipeline_config/.test(s)).length, 0);
  assert.equal(warns.length, 0);
  const c = (await withWarns(() => auto(mk([], { bench: BENCH, noRegimeRows: true })))).r;
  assert.deepEqual(c.qualifying, []);
  process.env.OPENCLAW_PROMOTION_BENCH_GATE = '0';
  try {
    const k = await gate(mk([], { bench: BENCH, noRegimeRows: true }));
    assert.equal(k.pass, true);                          // legacy total-window path as on main
  } finally { delete process.env.OPENCLAW_PROMOTION_BENCH_GATE; }
});
test('sleeve query error (null byRegime) => no_backtest; kill switch legacy pass', async () => {
  const base = mk([], { bench: BENCH });
  const q = async (sql, p) => { if (/strategy_backtest_regimes|universe_shrink_metrics/.test(sql)) throw new Error('boom'); return base(sql, p); };
  const { r } = await withWarns(() => gate(q));
  assert.deepEqual(r.failedGates, ['no_backtest']);
  process.env.OPENCLAW_PROMOTION_BENCH_GATE = '0';
  try { assert.equal((await gate(q)).pass, true); } finally { delete process.env.OPENCLAW_PROMOTION_BENCH_GATE; }
});

// ── MINOR-5: end to end through transitionStrategy (auto mode) ──────────────
function tmpManifest() {
  const p = path.join(os.tmpdir(), `bench_leg_manifest_${process.pid}_${Math.random().toString(36).slice(2)}.json`);
  fs.writeFileSync(p, JSON.stringify({ strategies: { S_x: { state: 'candidate', history: [] } } }));
  return p;
}
test('transitionStrategy auto mode: qualifying set returned when a regime clears the bench', async () => {
  const mp = tmpManifest();
  try {
    const { r } = await withWarns(() => ps.transitionStrategy({ dbQuery: mk([SL('LOW_VOL', 1.0), SL('CRISIS', 0.8)], { bench: BENCH }),
      manifestPath: mp, sid: 'S_x', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true }));
    assert.equal(r.ok, true);
    assert.deepEqual(r.qualifyingRegimes, ['LOW_VOL']);
    assert.equal(JSON.parse(fs.readFileSync(mp, 'utf8')).strategies.S_x.state, 'live');
  } finally { fs.rmSync(mp, { force: true }); }
});
test('transitionStrategy auto mode: sparse-CCA fixture is refused (422 shape), manifest untouched', async () => {
  const mp = tmpManifest();
  const sleeves = [SL('CRISIS', 0.827, 860), SL('HIGH_VOL', -0.503), SL('LOW_VOL', -0.329), SL('TRANSITIONING', 0.013)];
  try {
    const { r } = await withWarns(() => ps.transitionStrategy({ dbQuery: mk(sleeves, { bench: BENCH, excess: 0 }),
      manifestPath: mp, sid: 'S_x', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true }));
    assert.equal(r.ok, false);
    assert.equal(r.failedGates[0], 'no_qualifying_regime');
    assert.equal(JSON.parse(fs.readFileSync(mp, 'utf8')).strategies.S_x.state, 'candidate');
  } finally { fs.rmSync(mp, { force: true }); }
});
