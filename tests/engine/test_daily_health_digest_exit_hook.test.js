'use strict';
const test = require('node:test');
const assert = require('node:assert');
// The digest module requires the DB client at load; stub it before requiring.
require.cache[require.resolve('../../src/database/postgres')] = { exports: { query: async () => ({ rows: [] }) } };
const { exitHookLine } = require('../../src/engine/daily-health-digest');

test('no rows → null', () => { assert.strictEqual(exitHookLine([]), null); assert.strictEqual(exitHookLine(null), null); });

test('formats strategy exits by reason and max_hold', () => {
  const rows = [{ close_reason: 'strategy_exit:pair_decohered', n: '7' },
                { close_reason: 'strategy_exit:z_revert', n: 9 },
                { close_reason: 'max_hold', n: '2' }];
  assert.strictEqual(exitHookLine(rows), '🪝 Exit hook: 16 strategy exits (z_revert=9, pair_decohered=7), 2 max_hold');
});

test('only max_hold', () => {
  assert.strictEqual(exitHookLine([{ close_reason: 'max_hold', n: 3 }]), '🪝 Exit hook: 0 strategy exits, 3 max_hold');
});
