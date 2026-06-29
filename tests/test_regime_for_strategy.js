// tests/test_regime_for_strategy.js — crypto strategies must be badged against the
// CRYPTO regime (engine gates them on crypto_regime_latest.json), not the equity one.
const assert = require('assert');
const { regimeForStrategy } = require('../src/channels/api/regime_active');

assert.strictEqual(regimeForStrategy('crypto', 'LOW_VOL', 'HIGH_VOL'), 'HIGH_VOL', 'crypto → crypto regime');
assert.strictEqual(regimeForStrategy('equity', 'LOW_VOL', 'HIGH_VOL'), 'LOW_VOL', 'equity → equity regime');
assert.strictEqual(regimeForStrategy(undefined, 'LOW_VOL', 'HIGH_VOL'), 'LOW_VOL', 'default → equity regime');
assert.strictEqual(regimeForStrategy('etp', 'LOW_VOL', 'HIGH_VOL'), 'LOW_VOL', 'non-crypto → equity regime');
assert.strictEqual(regimeForStrategy('crypto', 'LOW_VOL', null), 'LOW_VOL', 'crypto regime missing → equity fallback');
console.log('ok test_regime_for_strategy');
