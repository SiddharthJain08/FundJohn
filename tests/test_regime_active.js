// tests/test_regime_active.js
// Unit tests for isRegimeEligibleNow — the dashboard's "is this strategy active
// in the current regime?" decision. It MUST mirror the live engine's gate
// (src/execution/regime_param_resolver.is_eligible): the per-(strategy,regime)
// strategy_regime_params row wins; a MISSING row fails OPEN (eligible). The old
// dashboard logic used code-level active_in_regimes, which diverged from what the
// engine actually trades (e.g. low_volatility_us: eligible=CRISIS only but
// active_in_regimes=[LOW_VOL,...] → badge said LIVE in LOW_VOL while engine skipped).
const assert = require('assert');
const { isRegimeEligibleNow } = require('../src/channels/api/regime_active');

// 1. Explicit eligible=true for the current regime → active.
{
  const rp = { LOW_VOL: true, TRANSITIONING: true, HIGH_VOL: true, CRISIS: true };
  assert.strictEqual(isRegimeEligibleNow(rp, 'LOW_VOL'), true);
}

// 2. THE BUG CASE: explicit eligible=false for the current regime → NOT active.
//    low_volatility_us: params say CRISIS-only; current market regime is LOW_VOL.
//    Engine skips it, so the badge must NOT say active.
{
  const rp = { CRISIS: true, LOW_VOL: false, TRANSITIONING: false, HIGH_VOL: false };
  assert.strictEqual(isRegimeEligibleNow(rp, 'LOW_VOL'), false, 'eligible=false → inactive');
  assert.strictEqual(isRegimeEligibleNow(rp, 'CRISIS'), true, 'eligible=true → active');
}

// 3. No strategy_regime_params rows at all (unmigrated strategy) → fail OPEN.
//    The engine's resolver returns True when get_row is None, so it trades
//    everywhere; the dashboard must agree.
{
  assert.strictEqual(isRegimeEligibleNow(null, 'CRISIS'), true);
  assert.strictEqual(isRegimeEligibleNow(undefined, 'LOW_VOL'), true);
  assert.strictEqual(isRegimeEligibleNow({}, 'HIGH_VOL'), true);
}

// 4. Partial rows present but NOT for the current regime → fail OPEN (matches
//    resolver get_row(strategy, regime) == None → is_eligible True). e.g. only a
//    CRISIS=true row exists; current regime LOW_VOL has no row → engine trades it.
{
  const rp = { CRISIS: true };
  assert.strictEqual(isRegimeEligibleNow(rp, 'LOW_VOL'), true, 'missing row → fail-open');
  assert.strictEqual(isRegimeEligibleNow(rp, 'CRISIS'), true);
}

// 5. Defensive: no current regime known → fail OPEN (don't mark inactive on bad input).
{
  assert.strictEqual(isRegimeEligibleNow({ LOW_VOL: false }, null), true);
  assert.strictEqual(isRegimeEligibleNow({ LOW_VOL: false }, undefined), true);
}

// 6. Only a strict boolean true counts as eligible (a truthy non-true value does not
//    silently pass — the resolver compares bool(row['eligible'])).
{
  assert.strictEqual(isRegimeEligibleNow({ LOW_VOL: 'true' }, 'LOW_VOL'), false, 'string is not eligible');
  assert.strictEqual(isRegimeEligibleNow({ LOW_VOL: 1 }, 'LOW_VOL'), false, 'number is not eligible');
}

console.log('ok test_regime_active');
