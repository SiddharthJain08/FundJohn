/**
 * src/lib/bt_trades_source.js — which table the backtest-trades readers use.
 * strategy_backtest_trades_primary (migration 166, primary-run rows only) iff
 * OPENCLAW_BT_TRADES_HOT === '1'; otherwise the legacy table. Read per call,
 * never cached. Mirrors trades_source() in src/backtest/trades_primary.py.
 */
function btTradesTable() {
  return process.env.OPENCLAW_BT_TRADES_HOT === '1'
    ? 'strategy_backtest_trades_primary'
    : 'strategy_backtest_trades';
}

module.exports = { btTradesTable };
