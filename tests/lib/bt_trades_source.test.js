const test = require('node:test');
const assert = require('node:assert/strict');
const { btTradesTable } = require('../../src/lib/bt_trades_source');

test('btTradesTable default legacy, =1 hot, other values legacy', () => {
  const prev = process.env.OPENCLAW_BT_TRADES_HOT;
  try {
    delete process.env.OPENCLAW_BT_TRADES_HOT;
    assert.equal(btTradesTable(), 'strategy_backtest_trades');
    process.env.OPENCLAW_BT_TRADES_HOT = '1';
    assert.equal(btTradesTable(), 'strategy_backtest_trades_primary');
    for (const v of ['0', 'true', '']) {
      process.env.OPENCLAW_BT_TRADES_HOT = v;
      assert.equal(btTradesTable(), 'strategy_backtest_trades');
    }
  } finally {
    if (prev === undefined) delete process.env.OPENCLAW_BT_TRADES_HOT;
    else process.env.OPENCLAW_BT_TRADES_HOT = prev;
  }
});
