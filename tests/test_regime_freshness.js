// tests/test_regime_freshness.js — flag a stale DAILY regime block (frozen
// date/stress/roro) so the dashboard greys it instead of showing it as current.
const assert = require('assert');
const { regimeFreshness } = require('../src/channels/api/regime_freshness');

// 1. THE LIVE CASE: daily date 2026-06-08 frozen, intraday fresh 2026-06-26 → stale.
{
  const r = regimeFreshness({ date: '2026-06-08', intraday_updated_at: '2026-06-26 23:45:00+00:00' },
                            Date.parse('2026-06-29T04:00:00Z'));
  assert.strictEqual(r.daily_stale, true);
  assert.strictEqual(r.daily_date, '2026-06-08');
  assert.ok(r.daily_age_hours > 24 * 20);
}
// 2. Daily block fresh (same day as intraday) → not stale.
{
  const r = regimeFreshness({ date: '2026-06-29', intraday_updated_at: '2026-06-29 13:00:00+00:00' },
                            Date.parse('2026-06-29T14:00:00Z'));
  assert.strictEqual(r.daily_stale, false);
}
// 3. No intraday field → fall back to absolute age (>48h stale).
{
  const r = regimeFreshness({ date: '2026-06-20' }, Date.parse('2026-06-29T00:00:00Z'));
  assert.strictEqual(r.daily_stale, true);
}
// 4. Garbage / missing → safe defaults, never throws.
{
  assert.deepStrictEqual(regimeFreshness(null, Date.now()), { daily_date: null, daily_age_hours: null, daily_stale: false });
  assert.strictEqual(regimeFreshness({}, Date.now()).daily_stale, false);
}
console.log('ok test_regime_freshness');
