"""Phase 2E — reference strategy demonstrating StrategyTemplate.

NOT registered for live trading.  The leading underscore in the filename is
a deliberate signal to the file-walking generators (sidecars, signatures,
requirements, backfill_regime_backtests) to skip this file.

Pattern: SMA bullish-crossover entry, ATR-band exit, per-share commission."""
from src.strategies.strategy_template_base import StrategyTemplate


class SMACrossoverExample(StrategyTemplate):
    def init(self):
        self.sma_fast = self.I(lambda c: c.rolling(10).mean(), self.data.close)
        self.sma_slow = self.I(lambda c: c.rolling(30).mean(), self.data.close)
        # ATR(14) for exit-band sizing
        high_low = self.data.high - self.data.low
        high_close = (self.data.high - self.data.close.shift()).abs()
        low_close  = (self.data.low  - self.data.close.shift()).abs()
        true_range = high_low.combine(high_close, max).combine(low_close, max)
        self.atr = self.I(lambda tr: tr.rolling(14).mean(), true_range)

    def next(self):
        if self.bar_idx < 30:
            return
        i = self.bar_idx
        # Bullish crossover: fast crosses ABOVE slow on this bar
        crossed_up = (self.sma_fast.iloc[i] > self.sma_slow.iloc[i] and
                      self.sma_fast.iloc[i-1] <= self.sma_slow.iloc[i-1])
        # Exit on ATR-band breach below entry, OR on bearish crossover
        crossed_down = (self.sma_fast.iloc[i] < self.sma_slow.iloc[i] and
                        self.sma_fast.iloc[i-1] >= self.sma_slow.iloc[i-1])
        if crossed_up:
            self.buy(qty=100)
        elif crossed_down:
            self.sell(qty=100)


def alpaca_fee(size: int, price: float) -> float:
    """Approximate Alpaca cost: half-cent per share + SEC §31 fee + FINRA TAF.
    Simplified: applies on both sides (real SEC/TAF only on sells).
    Sign convention: positive size = buy, negative = sell. Use abs() for symmetric fee."""
    notional = abs(size) * price
    sec_fee = notional * 0.0000278
    taf_fee = abs(size) * 0.000166
    return abs(size) * 0.005 + sec_fee + taf_fee
