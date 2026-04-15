"""
Backtesting Engine — R8 implementation (full).

FundJohn Pipeline Audit 2026-04-13

PIT-safe vectorised replay loop with:
  - Point-in-time data access (never reads data beyond the current bar)
  - Half-Kelly position sizing (fraction = 0.5)
  - Per-position max 5 % of portfolio
  - Vol-regime-aware allocation (LOW_VOL / TRANSITIONING / HIGH_VOL / CRISIS)
  - Slippage + commission model
  - Drawdown tracking and Sharpe/Sortino computation
  - Strategy-agnostic: any class implementing BaseBacktester can be run

Usage
-----
    from backtesting.engine import BacktestConfig, BacktestEngine

    cfg = BacktestConfig(
        symbol        = "AAPL",
        start_date    = "2023-01-01",
        end_date      = "2024-12-31",
        initial_cash  = 1_000_000,
    )
    engine = BacktestEngine(cfg)
    result = await engine.run(my_strategy_func)
    print(result.summary())
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Vol regime multipliers ────────────────────────────────────────────────────
# Scale position sizes down in higher-volatility regimes
VOL_REGIME_MULTIPLIER: Dict[str, float] = {
    "LOW_VOL":      1.0,
    "TRANSITIONING": 0.75,
    "HIGH_VOL":     0.50,
    "CRISIS":       0.25,
}

DEFAULT_COMMISSION = 0.001    # 0.10 % per side
DEFAULT_SLIPPAGE   = 0.0005   # 0.05 % per side
HALF_KELLY         = 0.5
MAX_POSITION_PCT   = 0.05     # 5 % of portfolio per position
RISK_FREE_RATE     = 0.05     # annualised, for Sharpe computation
TRADING_DAYS_YEAR  = 252


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    symbol:        str
    start_date:    str
    end_date:      str
    initial_cash:  float  = 1_000_000.0
    commission:    float  = DEFAULT_COMMISSION
    slippage:      float  = DEFAULT_SLIPPAGE
    kelly_fraction: float = HALF_KELLY
    max_pos_pct:   float  = MAX_POSITION_PCT
    vol_regime:    str    = "HIGH_VOL"   # current live regime for sizing
    benchmark:     str    = "SPY"


@dataclass
class Trade:
    date:       str
    symbol:     str
    direction:  str          # "BUY" or "SELL"
    shares:     float
    price:      float
    commission: float
    slippage:   float

    @property
    def net_cost(self) -> float:
        gross = self.shares * self.price
        costs = self.commission + self.slippage
        return gross + costs if self.direction == "BUY" else gross - costs


@dataclass
class BacktestResult:
    config:           BacktestConfig
    equity_curve:     pd.Series                    # index = date, values = portfolio value
    trades:           List[Trade] = field(default_factory=list)
    positions:        Dict[str, float] = field(default_factory=dict)  # symbol → shares
    daily_returns:    pd.Series = field(default_factory=pd.Series)

    # ── Metrics (computed on first access) ───────────────────────────────────

    @property
    def total_return(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        return (self.equity_curve.iloc[-1] / self.equity_curve.iloc[0]) - 1.0

    @property
    def cagr(self) -> float:
        if self.equity_curve.empty or len(self.equity_curve) < 2:
            return 0.0
        n_years = len(self.equity_curve) / TRADING_DAYS_YEAR
        return (1 + self.total_return) ** (1 / max(n_years, 1e-6)) - 1

    @property
    def sharpe(self) -> float:
        dr = self.daily_returns.dropna()
        if len(dr) < 2:
            return 0.0
        excess = dr - (RISK_FREE_RATE / TRADING_DAYS_YEAR)
        return float(excess.mean() / (excess.std() + 1e-9) * np.sqrt(TRADING_DAYS_YEAR))

    @property
    def sortino(self) -> float:
        dr = self.daily_returns.dropna()
        if len(dr) < 2:
            return 0.0
        excess  = dr - (RISK_FREE_RATE / TRADING_DAYS_YEAR)
        neg_ret = dr[dr < 0]
        downside_std = neg_ret.std() if len(neg_ret) > 1 else 1e-9
        return float(excess.mean() / (downside_std + 1e-9) * np.sqrt(TRADING_DAYS_YEAR))

    @property
    def max_drawdown(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        roll_max = self.equity_curve.cummax()
        drawdown = (self.equity_curve - roll_max) / (roll_max + 1e-9)
        return float(drawdown.min())

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        sells = [t for t in self.trades if t.direction == "SELL"]
        if not sells:
            return 0.0
        # We approximate win/loss via sign of daily return on trade day
        wins = sum(1 for t in sells if self.daily_returns.get(t.date, 0) > 0)
        return wins / len(sells)

    def summary(self) -> Dict:
        return {
            "symbol":        self.config.symbol,
            "start":         self.config.start_date,
            "end":           self.config.end_date,
            "total_return":  f"{self.total_return:.2%}",
            "cagr":          f"{self.cagr:.2%}",
            "sharpe":        f"{self.sharpe:.3f}",
            "sortino":       f"{self.sortino:.3f}",
            "max_drawdown":  f"{self.max_drawdown:.2%}",
            "win_rate":      f"{self.win_rate:.2%}",
            "n_trades":      len(self.trades),
            "final_equity":  f"${self.equity_curve.iloc[-1]:,.0f}" if not self.equity_curve.empty else "N/A",
        }


# ── Strategy callable type ────────────────────────────────────────────────────

# A strategy function receives:
#   prices_to_date : pd.DataFrame  — OHLCV data up to (and including) current bar
#   current_bar    : pd.Series     — today's OHLCV row
#   positions      : dict          — current open positions {symbol: shares}
#   cash           : float         — available cash
#   config         : BacktestConfig
#
# Returns: list of (symbol, direction, fraction_of_portfolio) tuples
#   fraction_of_portfolio: 0..1 — desired allocation (will be Kelly-capped)
StrategyFn = Callable[
    [pd.DataFrame, pd.Series, dict, float, BacktestConfig],
    Awaitable[List[Tuple[str, str, float]]],
]


# ── Engine ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Vectorised PIT-safe backtesting engine.

    The engine iterates over daily bars in chronological order.  For each bar:
      1. The strategy function is called with only data up to that bar.
      2. Orders are sized using Half-Kelly, vol-regime multiplier, and max_pos_pct.
      3. Trades are executed at the *next* bar's open (realistic simulation).
      4. Portfolio value is marked to the closing price of each bar.
    """

    def __init__(self, config: BacktestConfig):
        self.config = config

    # ── Sizing ────────────────────────────────────────────────────────────────

    def _size_order(
        self,
        target_fraction: float,
        portfolio_value: float,
        price:           float,
    ) -> float:
        """
        Convert a target portfolio fraction to share count.
        Applies Half-Kelly shrinkage, vol-regime multiplier, and position cap.
        """
        regime_mult  = VOL_REGIME_MULTIPLIER.get(self.config.vol_regime, 0.5)
        kelly_adj    = target_fraction * self.config.kelly_fraction * regime_mult
        capped       = min(kelly_adj, self.config.max_pos_pct)
        dollar_value = portfolio_value * capped
        shares       = dollar_value / max(price, 1e-6)
        return max(0.0, shares)

    # ── Trade execution ───────────────────────────────────────────────────────

    def _execute_buy(
        self,
        symbol:          str,
        shares:          float,
        price:           float,
        cash:            float,
        positions:       dict,
        trades:          list,
        bar_date:        str,
    ) -> float:
        """Execute a BUY, return updated cash."""
        exec_price = price * (1 + self.config.slippage)
        commission = shares * exec_price * self.config.commission
        total_cost = shares * exec_price + commission

        if total_cost > cash:
            shares     = (cash * 0.99) / (exec_price * (1 + self.config.commission))
            total_cost = shares * exec_price + shares * exec_price * self.config.commission

        if shares <= 0:
            return cash

        trades.append(Trade(
            date=bar_date, symbol=symbol, direction="BUY",
            shares=shares, price=exec_price,
            commission=shares * exec_price * self.config.commission,
            slippage=shares * price * self.config.slippage,
        ))
        positions[symbol] = positions.get(symbol, 0.0) + shares
        return cash - total_cost

    def _execute_sell(
        self,
        symbol:    str,
        shares:    float,
        price:     float,
        cash:      float,
        positions: dict,
        trades:    list,
        bar_date:  str,
    ) -> float:
        """Execute a SELL (up to held shares), return updated cash."""
        held = positions.get(symbol, 0.0)
        shares = min(shares, held)
        if shares <= 0:
            return cash

        exec_price = price * (1 - self.config.slippage)
        commission = shares * exec_price * self.config.commission
        proceeds   = shares * exec_price - commission

        trades.append(Trade(
            date=bar_date, symbol=symbol, direction="SELL",
            shares=shares, price=exec_price,
            commission=commission,
            slippage=shares * price * self.config.slippage,
        ))
        positions[symbol] = held - shares
        if positions[symbol] < 1e-6:
            del positions[symbol]

        return cash + proceeds

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(
        self,
        strategy_fn:  StrategyFn,
        price_data:   Optional[pd.DataFrame] = None,
    ) -> BacktestResult:
        """
        Run the backtest.

        Parameters
        ----------
        strategy_fn : StrategyFn
            Async function returning target allocations for each bar.
        price_data : pd.DataFrame, optional
            Pre-loaded OHLCV DataFrame.  If None, loaded from MarketDataStore.
        """
        # Load price data if not provided
        if price_data is None:
            price_data = await self._load_prices()

        if price_data.empty:
            logger.error("BacktestEngine: no price data for %s", self.config.symbol)
            return BacktestResult(
                config       = self.config,
                equity_curve = pd.Series(dtype=float),
            )

        # Ensure date column and sort
        if "date" in price_data.columns:
            price_data["date"] = pd.to_datetime(price_data["date"])
            price_data = price_data.sort_values("date").reset_index(drop=True)
        else:
            logger.error("BacktestEngine: price_data has no 'date' column")
            return BacktestResult(config=self.config, equity_curve=pd.Series(dtype=float))

        # Filter to backtest window (PIT safety: strict <=)
        start = pd.Timestamp(self.config.start_date)
        end   = pd.Timestamp(self.config.end_date)
        price_data = price_data[
            (price_data["date"] >= start) & (price_data["date"] <= end)
        ].reset_index(drop=True)

        if price_data.empty:
            logger.warning("BacktestEngine: no data in window %s–%s", self.config.start_date, self.config.end_date)
            return BacktestResult(config=self.config, equity_curve=pd.Series(dtype=float))

        # Resolve OHLCV column names (handle various naming conventions)
        close_col = self._find_col(price_data, ["close", "Close", "c", "adjClose"])
        open_col  = self._find_col(price_data, ["open", "Open", "o"])
        if close_col is None:
            logger.error("BacktestEngine: cannot find close-price column")
            return BacktestResult(config=self.config, equity_curve=pd.Series(dtype=float))

        # Initialise state
        cash: float       = self.config.initial_cash
        positions: dict   = {}
        trades: list      = []
        equity_values     = []
        equity_dates      = []

        n_bars = len(price_data)

        for i in range(n_bars):
            bar    = price_data.iloc[i]
            d_str  = bar["date"].strftime("%Y-%m-%d")
            close  = float(bar[close_col])

            # ── PIT slice: only data up to and including bar i ───────────────
            history = price_data.iloc[: i + 1]

            # ── Strategy generates target allocations ─────────────────────────
            try:
                signals = await strategy_fn(history, bar, positions.copy(), cash, self.config)
            except Exception as exc:
                logger.warning("BacktestEngine: strategy error on %s — %s", d_str, exc)
                signals = []

            # ── Execution at next bar's open (lookahead-safe) ─────────────────
            # We use the CURRENT bar's close as execution price for simplicity
            # (next-open execution is implemented when open_col is available)
            exec_price = close
            if open_col and i + 1 < n_bars:
                exec_price = float(price_data.iloc[i + 1][open_col])

            for symbol, direction, fraction in (signals or []):
                portfolio_value = cash + sum(
                    positions.get(s, 0) * float(price_data.iloc[i][close_col])
                    for s in positions
                )
                shares = self._size_order(abs(fraction), portfolio_value, exec_price)

                if direction.upper() == "BUY" and fraction > 0:
                    cash = self._execute_buy(symbol, shares, exec_price, cash, positions, trades, d_str)
                elif direction.upper() in ("SELL", "SHORT") and fraction != 0:
                    cash = self._execute_sell(symbol, shares, exec_price, cash, positions, trades, d_str)

            # ── Mark portfolio to market ───────────────────────────────────────
            position_value = positions.get(self.config.symbol, 0.0) * close
            total_value    = cash + position_value
            equity_values.append(total_value)
            equity_dates.append(bar["date"])

        equity_curve  = pd.Series(equity_values, index=equity_dates, name="equity")
        daily_returns = equity_curve.pct_change().dropna()

        logger.info(
            "BacktestEngine: %s complete — %d trades, final equity $%.0f",
            self.config.symbol, len(trades), equity_values[-1] if equity_values else 0
        )

        return BacktestResult(
            config        = self.config,
            equity_curve  = equity_curve,
            trades        = trades,
            positions     = positions,
            daily_returns = daily_returns,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
        for c in candidates:
            if c in df.columns:
                return c
        return None

    async def _load_prices(self) -> pd.DataFrame:
        """Load prices from MarketDataStore."""
        try:
            from data.data_store import get_store
            store = get_store()
            return await store.read_prices(
                symbol   = self.config.symbol,
                as_of    = self.config.end_date,
                lookback = (
                    date.fromisoformat(self.config.end_date)
                    - date.fromisoformat(self.config.start_date)
                ).days + 30,
            )
        except Exception as exc:
            logger.error("BacktestEngine: failed to load prices — %s", exc)
            return pd.DataFrame()


# ── Base strategy class ───────────────────────────────────────────────────────

class BaseBacktester:
    """
    Convenience base class for strategy-based backtesting.

    Subclasses implement `generate_signals()` and call `run()` to execute.

    Example
    -------
        class MyStrategy(BaseBacktester):
            async def generate_signals(self, history, bar, positions, cash, config):
                # Simple momentum: buy if price > 20d MA, sell otherwise
                ma20 = history["close"].tail(20).mean()
                if bar["close"] > ma20 and config.symbol not in positions:
                    return [(config.symbol, "BUY", 0.95)]
                elif bar["close"] < ma20 and config.symbol in positions:
                    return [(config.symbol, "SELL", 1.0)]
                return []

        strat  = MyStrategy(cfg)
        result = await strat.run()
        print(result.summary())
    """

    def __init__(self, config: BacktestConfig, price_data: Optional[pd.DataFrame] = None):
        self.config     = config
        self.engine     = BacktestEngine(config)
        self._price_data = price_data

    async def generate_signals(
        self,
        history:   pd.DataFrame,
        bar:       pd.Series,
        positions: dict,
        cash:      float,
        config:    BacktestConfig,
    ) -> List[Tuple[str, str, float]]:
        """Override in subclasses. Return list of (symbol, direction, fraction) tuples."""
        raise NotImplementedError

    async def run(self) -> BacktestResult:
        return await self.engine.run(self.generate_signals, self._price_data)


# ── Walk-forward harness ──────────────────────────────────────────────────────

async def walk_forward(
    strategy_fn:   StrategyFn,
    config:        BacktestConfig,
    price_data:    pd.DataFrame,
    train_days:    int = 252,
    test_days:     int = 63,
) -> List[BacktestResult]:
    """
    Rolling walk-forward validation.

    Splits `price_data` into sequential train/test windows, runs the strategy
    on each test window after training (currently strategy_fn is stateless —
    pass a factory if state is needed), and returns the list of results.
    """
    if price_data.empty:
        return []

    if "date" in price_data.columns:
        price_data = price_data.sort_values("date").reset_index(drop=True)

    results   = []
    n         = len(price_data)
    window    = train_days + test_days
    start_idx = 0

    while start_idx + window <= n:
        test_slice = price_data.iloc[start_idx + train_days: start_idx + window].copy()

        if test_slice.empty:
            break

        slice_start = test_slice["date"].iloc[0].strftime("%Y-%m-%d")
        slice_end   = test_slice["date"].iloc[-1].strftime("%Y-%m-%d")

        slice_cfg = BacktestConfig(
            symbol       = config.symbol,
            start_date   = slice_start,
            end_date     = slice_end,
            initial_cash = config.initial_cash,
            commission   = config.commission,
            slippage     = config.slippage,
            vol_regime   = config.vol_regime,
        )

        engine = BacktestEngine(slice_cfg)
        result = await engine.run(strategy_fn, test_slice)
        results.append(result)

        logger.info(
            "WalkForward: window %s→%s  Sharpe=%.2f  Return=%s",
            slice_start, slice_end, result.sharpe, result.total_return
        )

        start_idx += test_days

    return results
