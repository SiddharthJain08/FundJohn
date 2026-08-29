// tests/test_blend_scope.js
// Unit tests for blendScope — the per-regime → single-scope metric blender.
const assert = require('assert');
const { blendScope } = require('../src/channels/api/blend_scope');

// Helper: a per-regime breakdown map keyed by regime.
// Fields mirror strategy_backtest_regimes (percent units for max_dd_pct/return_pct).
const BD = {
  LOW_VOL:       { sharpe: 2.0, max_dd_pct: 10, return_pct: 30, hit_rate: 0.60,
                   avg_pnl_pct: 0.012, avg_holding_days: 4, trade_count: 80, oos_days_in_regime: 600 },
  TRANSITIONING: { sharpe: 1.0, max_dd_pct: 5,  return_pct: 8,  hit_rate: 0.50,
                   avg_pnl_pct: 0.004, avg_holding_days: 6, trade_count: 20, oos_days_in_regime: 400 },
  HIGH_VOL:      { sharpe: null, max_dd_pct: 0, return_pct: 0,  hit_rate: null,
                   avg_pnl_pct: null, avg_holding_days: null, trade_count: 0, oos_days_in_regime: 300 },
  CRISIS:        { sharpe: null, max_dd_pct: 0, return_pct: 0,  hit_rate: null,
                   avg_pnl_pct: null, avg_holding_days: null, trade_count: 0, oos_days_in_regime: 100 },
};

// 1. Single-regime blend == identity for that regime
{
  const m = blendScope(BD, ['LOW_VOL']);
  assert.strictEqual(m.closed_count, 80);
  assert.ok(Math.abs(m.sharpe - 2.0) < 1e-9, 'single-regime sharpe = that regime');
  assert.ok(Math.abs(m.win_rate - 0.60) < 1e-9);
  assert.ok(Math.abs(m.arr_pct - 1.2) < 1e-9, 'arr = avg_pnl_pct*100');
  assert.ok(Math.abs(m.act_days - 4) < 1e-9);
  assert.ok(Math.abs(m.adr_pct - 0.3) < 1e-9, 'adr = arr/max(1,act)');
  assert.ok(Math.abs(m.max_dd_pct - 10) < 1e-9);
  // Default since 2026-08-29 (spec D2): cadence normalization retired -> raw sharpe.
  assert.ok(Math.abs(m.effective_sharpe - 2.0) < 1e-9, 'eff = raw sharpe by default');
}

// 2. Two-regime eligible blend (LOW_VOL + TRANSITIONING)
{
  const m = blendScope(BD, ['LOW_VOL', 'TRANSITIONING']);
  // sharpe: day-freq weighted by oos_days_in_regime: (2.0*600 + 1.0*400)/(1000) = 1.6
  assert.ok(Math.abs(m.sharpe - 1.6) < 1e-9, `sharpe blend got ${m.sharpe}`);
  // max_dd = max(10,5) = 10
  assert.ok(Math.abs(m.max_dd_pct - 10) < 1e-9);
  // closed = 80+20 = 100
  assert.strictEqual(m.closed_count, 100);
  // win_rate trade-count weighted: (0.60*80 + 0.50*20)/100 = 0.58
  assert.ok(Math.abs(m.win_rate - 0.58) < 1e-9, `win got ${m.win_rate}`);
  // arr trade-count weighted: avg_pnl=(0.012*80 + 0.004*20)/100 = 0.0104 → *100 = 1.04
  assert.ok(Math.abs(m.arr_pct - 1.04) < 1e-9, `arr got ${m.arr_pct}`);
  // act trade-count weighted: (4*80 + 6*20)/100 = 4.4
  assert.ok(Math.abs(m.act_days - 4.4) < 1e-9, `act got ${m.act_days}`);
  // adr = 1.04 / max(1,4.4)
  assert.ok(Math.abs(m.adr_pct - (1.04 / 4.4)) < 1e-9);
  // eff = raw sharpe by default (cadence normalization retired 2026-08-29, spec D2)
  assert.ok(Math.abs(m.effective_sharpe - 1.6) < 1e-9);
}

// 3. Null-sharpe regime is skipped in sharpe blend but counts in closed
{
  const m = blendScope(BD, ['LOW_VOL', 'HIGH_VOL']);
  // HIGH_VOL sharpe null → sharpe blend = LOW_VOL only = 2.0
  assert.ok(Math.abs(m.sharpe - 2.0) < 1e-9, `sharpe skip-null got ${m.sharpe}`);
  // closed still includes HIGH_VOL's 0 trades → 80
  assert.strictEqual(m.closed_count, 80);
}

// 4. Empty regimeKeys → null; keys with no matching rows → null
{
  assert.strictEqual(blendScope(BD, []), null);
  assert.strictEqual(blendScope(BD, ['NONSENSE']), null);
  assert.strictEqual(blendScope(null, ['LOW_VOL']), null);
}

// 5. All-included regimes have zero trades → win/arr/act null, sharpe null, closed 0
{
  const m = blendScope(BD, ['HIGH_VOL', 'CRISIS']);
  assert.strictEqual(m.closed_count, 0);
  assert.strictEqual(m.sharpe, null);
  assert.strictEqual(m.win_rate, null);
  assert.strictEqual(m.arr_pct, null);
  assert.strictEqual(m.act_days, null);
  assert.strictEqual(m.effective_sharpe, null);
}

// 6. Revert flag (OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1) restores the
// legacy sharpe / sqrt(act_days) divisor.
{
  const prev = process.env.OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM;
  process.env.OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM = '1';
  try {
    const m1 = blendScope(BD, ['LOW_VOL']);
    assert.ok(Math.abs(m1.effective_sharpe - (2.0 / Math.sqrt(4))) < 1e-9,
              'revert flag: eff = sharpe/sqrt(act) single-regime');
    const m2 = blendScope(BD, ['LOW_VOL', 'TRANSITIONING']);
    assert.ok(Math.abs(m2.effective_sharpe - (1.6 / Math.sqrt(4.4))) < 1e-9,
              'revert flag: eff = sharpe/sqrt(act) blended scope');
  } finally {
    if (prev === undefined) delete process.env.OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM;
    else process.env.OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM = prev;
  }
}

console.log('ok test_blend_scope');
