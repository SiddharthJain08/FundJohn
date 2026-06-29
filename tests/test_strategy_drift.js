// tests/test_strategy_drift.js — manifest trade-intent (state∈live/monitoring) vs
// registry trade-reality (status==='approved', what the engine actually trades).
const assert = require('assert');
const { classifyDrift, summarizeDrift } = require('../src/channels/api/strategy_drift');

// agree → none
assert.strictEqual(classifyDrift('live', 'approved'), 'none');
assert.strictEqual(classifyDrift('monitoring', 'approved'), 'none');
assert.strictEqual(classifyDrift('candidate', 'pending_approval'), 'none');
assert.strictEqual(classifyDrift('candidate', 'deprecated'), 'none');
// manifest live but registry NOT approved → shown_live_not_trading (the phantom)
assert.strictEqual(classifyDrift('live', 'deprecated'), 'shown_live_not_trading');
// registry approved but manifest NOT live → trading_not_shown (the 15)
assert.strictEqual(classifyDrift('candidate', 'approved'), 'trading_not_shown');
// case-insensitive + null safety
assert.strictEqual(classifyDrift('LIVE', 'APPROVED'), 'none');
assert.strictEqual(classifyDrift(null, null), 'none');
assert.strictEqual(classifyDrift(undefined, 'approved'), 'trading_not_shown');
// summarize counts by drift class
const rows = [{drift:'none'},{drift:'trading_not_shown'},{drift:'trading_not_shown'},{drift:'shown_live_not_trading'}];
assert.deepStrictEqual(summarizeDrift(rows), { shown_live_not_trading:1, trading_not_shown:2, total:3 });
assert.deepStrictEqual(summarizeDrift([]), { shown_live_not_trading:0, trading_not_shown:0, total:0 });
console.log('ok test_strategy_drift');
