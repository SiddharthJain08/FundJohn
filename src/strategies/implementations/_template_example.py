"""Phase 2E — reference strategy demonstrating StrategyTemplate.

NOT registered for live trading.  The leading underscore in the filename is
a deliberate signal to lifecycle.py (and the *.py glob sites in
generate_sidecars.py / generate_signatures.py / scripts/generate_missing_requirements.py)
to skip this file during strategy discovery.

Pattern: SMA crossover with ATR-based exit and per-share commission."""
from src.strategies.strategy_template_base import StrategyTemplate


class SMACrossoverExample(StrategyTemplate):
    def init(self):
        self.sma_fast = self.I(lambda c: c.rolling(10).mean(), self.data.close)
        self.sma_slow = self.I(lambda c: c.rolling(30).mean(), self.data.close)

    def next(self):
        if self.bar_idx < 30:
            return
        if self.sma_fast.iloc[self.bar_idx] > self.sma_slow.iloc[self.bar_idx]:
            self.buy(qty=100)
        elif self.sma_fast.iloc[self.bar_idx] < self.sma_slow.iloc[self.bar_idx]:
            self.sell(qty=100)


def alpaca_fee(size: int, price: float) -> float:
    """Approximate Alpaca cost: half-cent per share + SEC §31 fee + FINRA TAF.
    Simplified: applies on both sides (real SEC/TAF only on sells)."""
    notional = abs(size) * price
    sec_fee = notional * 0.0000278
    taf_fee = abs(size) * 0.000166
    return abs(size) * 0.005 + sec_fee + taf_fee
