'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { engineTicker, mapBrokerPositions, buildBrokerBrackets } =
  require('../src/channels/api/broker_symbols');

test('engineTicker: crypto broker forms map to BASE-USD', () => {
  assert.equal(engineTicker('BTCUSD', 'crypto'), 'BTC-USD');
  assert.equal(engineTicker('BTC/USD', 'crypto'), 'BTC-USD');
  assert.equal(engineTicker('BTC/USD'), 'BTC-USD');          // slash rule alone
  assert.equal(engineTicker('btc-usd', 'crypto'), 'BTC-USD');
  assert.equal(engineTicker('BTC-USD', 'crypto'), 'BTC-USD');
});
test('engineTicker: crypto without USD suffix is upper-cased/trimmed only', () => {
  assert.equal(engineTicker(' eth/btc ', 'crypto'), 'ETH/BTC');
  assert.equal(engineTicker('USD', 'crypto'), 'USD');
});
test('engineTicker: equities untouched', () => {
  assert.equal(engineTicker('SPY', 'us_equity'), 'SPY');
  assert.equal(engineTicker('BRK.B', 'us_equity'), 'BRK.B');
  assert.equal(engineTicker('BRK.B'), 'BRK.B');
  assert.equal(engineTicker('AUDUSD'), 'AUDUSD');            // no asset class, no slash
});
test('engineTicker: null/undefined', () => {
  assert.equal(engineTicker(null), null);
  assert.equal(engineTicker(undefined, 'crypto'), null);
});

test('mapBrokerPositions keys crypto by engine ticker, parses fields', () => {
  const m = mapBrokerPositions([
    { symbol: 'BTCUSD', asset_class: 'crypto', qty: '0.148292179', market_value: '8844.83',
      side: 'long', unrealized_pl: '12.5', current_price: 'x' },
    { symbol: 'SPY', asset_class: 'us_equity', qty: '-3', market_value: '-1500' },
    null, { qty: '1' },
  ], 100000);
  assert.deepEqual(Object.keys(m).sort(), ['BTC-USD', 'SPY']);
  assert.equal(m['BTC-USD'].qty, 0.148292179);
  assert.equal(m['BTC-USD'].side, 'long');
  assert.equal(m['BTC-USD'].unrealized_pl, 12.5);
  assert.equal(m['BTC-USD'].current_price, null);
  assert.ok(Math.abs(m['BTC-USD'].size_pct - 0.0884483) < 1e-9);
  assert.equal(m.SPY.side, 'short');
  assert.equal(m.SPY.size_pct, 0.015);
  assert.equal(mapBrokerPositions([{ symbol: 'A', qty: '1', market_value: '5' }], null).A.size_pct, null);
});

test('buildBrokerBrackets: crypto order symbol, legs, pending_cancel, emex_', () => {
  const b = buildBrokerBrackets([
    { symbol: 'BTC/USD', asset_class: 'crypto', limit_price: '99000', status: 'new',
      legs: [{ symbol: 'BTC/USD', stop_price: '80000', status: 'held' }] },
    { symbol: 'SPY', stop_price: '400', status: 'new' },
    { symbol: 'SPY', limit_price: '500', status: 'pending_cancel' },
    { symbol: 'AAPL', client_order_id: 'emex_1', limit_price: '190' },
  ]);
  assert.deepEqual(b['BTC-USD'], { stop: 80000, target: 99000, exiting: null });
  assert.deepEqual(b.SPY, { stop: 400, target: null, exiting: null });
  assert.deepEqual(b.AAPL, { stop: null, target: null, exiting: 190 });
});
