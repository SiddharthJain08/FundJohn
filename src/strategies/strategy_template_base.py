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
    initial_equity: float = 100_000.0,
) -> dict:
    """Drive a StrategyTemplate subclass through `data`. Returns a dict with:
       - fills (int), total_commission (float), position_at_end (int)
       - fills_log (list[dict])
       - total_return_pct, sharpe_ratio, max_drawdown_pct
       - win_rate_pct, n_trades, avg_trade_pnl_usd
       - equity_curve (pd.Series indexed by bar timestamp)"""
    s = cls(data, commission=commission)
    s.init()

    cash = initial_equity
    equity_idx: list = []
    equity_vals: list = []
    last_buy_price: Optional[float] = None  # for round-trip PnL tracking
    last_buy_qty: int = 0
    trade_pnls: list = []  # per-round-trip PnL in USD
    total_commission = 0.0

    for i in range(len(data)):
        s.bar_idx = i

        # Fill pending orders at this bar's open
        new_pending = []
        for o in s._pending_orders:
            if o["bar"] == i:
                price = float(data.iloc[i].open)
                size = o["qty"] if o["side"] == "buy" else -o["qty"]
                fee = s._commission(size, price)
                total_commission += fee
                cash -= size * price + fee
                s._position += size  # keep ABC's no-overlap guard in sync
                s._fills.append({"bar": i, "side": o["side"], "qty": o["qty"],
                                 "price": price, "fee": fee})
                # Round-trip PnL tracking (long-only, single-position-at-a-time
                # per ABC's no-overlap guard).
                if o["side"] == "buy":
                    last_buy_price = price
                    last_buy_qty = o["qty"]
                else:  # sell — close the prior buy
                    if last_buy_price is not None:
                        gross = (price - last_buy_price) * last_buy_qty
                        # Subtract entry+exit commissions for this round-trip:
                        # the most recent two fills are this exit and its entry.
                        recent_fees = sum(f["fee"] for f in s._fills[-2:])
                        trade_pnls.append(gross - recent_fees)
                        last_buy_price = None
                        last_buy_qty = 0
            else:
                new_pending.append(o)
        s._pending_orders = new_pending

        s.next()

        # Mark equity at this bar's close
        position_value = s._position * float(data.iloc[i].close)
        equity = cash + position_value
        equity_idx.append(data.index[i])
        equity_vals.append(equity)

    # EOD close (existing behavior)
    if close_at_eod and s._position != 0:
        last_close = float(data.iloc[-1].close)
        size = -s._position
        fee = s._commission(size, last_close)
        total_commission += fee
        cash -= size * last_close + fee
        s._fills.append({"bar": len(data) - 1, "side": "sell" if size < 0 else "buy",
                         "qty": abs(size), "price": last_close, "fee": fee})
        # Round-trip PnL for the forced close — match the in-loop convention
        # (entry + exit fees from the most recent two fills).
        if last_buy_price is not None and size < 0:
            gross = (last_close - last_buy_price) * last_buy_qty
            recent_fees = sum(f["fee"] for f in s._fills[-2:])
            trade_pnls.append(gross - recent_fees)
            last_buy_price = None
            last_buy_qty = 0
        s._position = 0
        equity_vals[-1] = cash  # last bar's equity now reflects the forced close

    equity_curve = pd.Series(equity_vals, index=pd.Index(equity_idx))

    # Stats
    if len(equity_curve) > 0:
        total_return_pct = (equity_curve.iloc[-1] - initial_equity) / initial_equity * 100.0
    else:
        total_return_pct = 0.0
    daily_returns = equity_curve.pct_change().dropna()
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe_ratio = float(daily_returns.mean() / daily_returns.std() * (252 ** 0.5))
    else:
        sharpe_ratio = 0.0

    if len(equity_curve) > 0:
        rolling_peak = equity_curve.cummax()
        drawdown = (equity_curve - rolling_peak) / rolling_peak
        max_drawdown_pct = float(drawdown.min() * 100.0)
    else:
        max_drawdown_pct = 0.0

    n_trades = len(trade_pnls)
    win_rate_pct = (sum(1 for p in trade_pnls if p > 0) / n_trades * 100.0) if n_trades else 0.0
    avg_trade_pnl_usd = (sum(trade_pnls) / n_trades) if n_trades else 0.0

    return {
        "fills":              len(s._fills),
        "total_commission":   total_commission,
        "position_at_end":    s._position,
        "fills_log":          s._fills,
        "total_return_pct":   float(total_return_pct),
        "sharpe_ratio":       sharpe_ratio,
        "max_drawdown_pct":   max_drawdown_pct,
        "win_rate_pct":       float(win_rate_pct),
        "n_trades":           n_trades,
        "avg_trade_pnl_usd":  float(avg_trade_pnl_usd),
        "equity_curve":       equity_curve,
    }
