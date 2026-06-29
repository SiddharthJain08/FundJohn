// tests/test_leverage.js — realized broker leverage from account market values.
const assert = require('assert');
const { realizedLeverage } = require('../src/channels/api/leverage');

// 1. Long/short book → gross uses absolute values (live audit: 1.698x / 0.243x)
{
  const r = realizedLeverage({ long_market_value: 113302.37, short_market_value: -84914.84, equity: 116734.52 });
  assert.ok(Math.abs(r.gross - 1.6980) < 1e-3, `gross ~1.698 got ${r.gross}`);
  assert.ok(Math.abs(r.net   - 0.2432) < 1e-3, `net ~0.243 got ${r.net}`);
}
// 2. equity <= 0 → null (no div-by-zero / nonsense)
{
  const r = realizedLeverage({ long_market_value: 100, short_market_value: 0, equity: 0 });
  assert.strictEqual(r.gross, null); assert.strictEqual(r.net, null);
}
// 3. Flat book → 0
{
  const r = realizedLeverage({ long_market_value: 0, short_market_value: 0, equity: 1000 });
  assert.strictEqual(r.gross, 0); assert.strictEqual(r.net, 0);
}
// 4. String inputs (Alpaca CLI returns strings) are coerced
{
  const r = realizedLeverage({ long_market_value: '200', short_market_value: '-50', equity: '100' });
  assert.strictEqual(r.gross, 2.5); assert.strictEqual(r.net, 1.5);
}
console.log('ok test_leverage');
