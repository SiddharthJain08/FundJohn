"""Phase 2E — clean-room StrategyTemplate ABC.

Contract-equivalent to backtesting.py Strategy.init/next API; written from
scratch (no AGPL code copied; behavior contract reproduced).  Used as the
code-gen template for StrategyCoderJohn-emitted strategies.

The ABC enforces:
  - init() runs once before the first bar.  Indicator declarations via self.I().
  - next() runs once per bar; self.bar_idx is the current row.
  - self.buy(qty) / self.sell(qty) place orders for the next bar's open.
  - commission callable: fn(size: int, price: float) -> float (USD)
  - close_at_eod=True forces flat at end of data.

NOT a backtest engine.  Use src/backtest/quick_backtest.run_single_bracket
or src/backtest/unified_backtest.py for production strategy evaluation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional
import pandas as pd


CommissionFn = Callable[[int, float], float]


class StrategyTemplate(ABC):
    def __init__(self, data: pd.DataFrame, commission: Optional[CommissionFn] = None):
        self.data = data
        self._commission = commission or (lambda size, price: 0.0)
        self.bar_idx = 0
        self._position = 0
        self._fills: list[dict] = []
        self._pending_orders: list[dict] = []

    def I(self, fn: Callable, *series) -> pd.Series:
        """Declare an indicator.  Computed once at init time."""
        return fn(*series)

    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def next(self) -> None: ...

    def buy(self, qty: int) -> None:
        if self._position != 0:
            return
        self._pending_orders.append({"side": "buy", "qty": qty, "bar": self.bar_idx + 1})

    def sell(self, qty: int) -> None:
        if self._position == 0:
            return
        self._pending_orders.append({"side": "sell", "qty": qty, "bar": self.bar_idx + 1})


def run(
    cls: type[StrategyTemplate],
    data: pd.DataFrame,
    commission: Optional[CommissionFn] = None,
    close_at_eod: bool = False,
) -> dict:
    """Drive a StrategyTemplate subclass through `data`.  Returns:
       {fills, total_commission, position_at_end, fills_log}"""
    s = cls(data, commission=commission)
    s.init()
    total_commission = 0.0

    for i in range(len(data)):
        s.bar_idx = i
        new_pending = []
        for o in s._pending_orders:
            if o["bar"] == i:
                price = float(data.iloc[i].open)
                size = o["qty"] if o["side"] == "buy" else -o["qty"]
                s._position += size
                fee = s._commission(size, price)
                total_commission += fee
                s._fills.append({"bar": i, "side": o["side"], "qty": o["qty"],
                                 "price": price, "fee": fee})
            else:
                new_pending.append(o)
        s._pending_orders = new_pending
        s.next()

    if close_at_eod and s._position != 0:
        last_close = float(data.iloc[-1].close)
        size = -s._position
        fee = s._commission(size, last_close)
        total_commission += fee
        s._fills.append({"bar": len(data) - 1, "side": "sell" if size < 0 else "buy",
                         "qty": abs(size), "price": last_close, "fee": fee})
        s._position = 0

    return {
        "fills":            len(s._fills),
        "total_commission": total_commission,
        "position_at_end":  s._position,
        "fills_log":        s._fills,
    }
