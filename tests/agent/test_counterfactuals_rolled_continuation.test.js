// SP-6 Phase C — rolled_continuation exclusion from comprehensive_review's
// _counterfactuals (the one stat site that filters in JS, not SQL).
//
// rolled_continuation rows are roll segments of an ongoing position (SP-6 D1),
// not trades; they must be excluded from the per-strategy counterfactual stats.
// This is the JS-array analogue of the SQL `IS DISTINCT FROM` predicate. The
// NULL trap differs in mechanism here: JS `null !== 'rolled_continuation'` is
// true, so a NULL-close_reason closed row MUST survive the filter.
//
// Three-row pin (mirrors tests/test_rolled_continuation_stat_exclusion.py):
//   1. rolled_continuation row EXCLUDED,
//   2. NULL-close_reason row STILL INCLUDED (the NULL trap),
//   3. real-reason (stop_loss) row included.
const { test } = require('node:test');
const assert   = require('node:assert/strict');
const { _counterfactuals } = require('../../src/agent/curators/comprehensive_review');

function pnlRow(close_reason, unrealized_pnl_pct, days_held) {
  return { status: 'closed', unrealized_pnl_pct, days_held, close_reason };
}

// 3 keeper rows (clears the function's `closed.length < 3` guard so the full
// stats compute) + 1 roll segment. Keepers: {NULL, stop_loss, target_1}.
test('_counterfactuals excludes rolled_continuation, keeps NULL + real reasons', () => {
  const pnl = [
    pnlRow(null,                   5.0, 3),  // NULL  → must STAY
    pnlRow('stop_loss',           -2.0, 4),  // real  → must STAY
    pnlRow('target_1',             6.0, 2),  // real  → must STAY
    pnlRow('rolled_continuation',  8.0, 1),  // roll  → EXCLUDED
  ];
  const { base } = _counterfactuals(pnl);
  // 3 keepers counted; roll segment dropped (would be 4 if it leaked in).
  assert.equal(base.n_closed, 3, JSON.stringify(base));
  // avg_hold_days over {3,4,2} == 3.0; the roll's days=1 would drag it to 2.5.
  assert.equal(base.avg_hold_days, 3, JSON.stringify(base));
  // win_rate over {+5,-2,+6}: 2 wins / 3 ≈ 0.667. The roll (+8) would make 3/4=0.75.
  assert.equal(base.win_rate, 0.667, JSON.stringify(base));
  // The roll must not appear in any reason bucket.
  assert.equal(base.stops_hit, 1, JSON.stringify(base));
});

test('NULL-trap: NULL close_reason closed rows are NOT dropped', () => {
  // Pure NULL-trap pin: three NULL-reason keepers + one roll. A NULL-hostile
  // equality filter would vanish the NULLs → n_closed < 3 → the "too few"
  // branch (no avg_hold_days). The JS `!== 'rolled_continuation'` keeps NULLs.
  const pnl = [
    pnlRow(null, 2.0, 2),
    pnlRow(null, 4.0, 6),
    pnlRow(null, 1.0, 4),
    pnlRow('rolled_continuation', 9.0, 1),
  ];
  const { base } = _counterfactuals(pnl);
  assert.equal(base.n_closed, 3, JSON.stringify(base));      // all NULLs survived
  assert.equal(base.avg_hold_days, 4, JSON.stringify(base)); // (2+6+4)/3
});
