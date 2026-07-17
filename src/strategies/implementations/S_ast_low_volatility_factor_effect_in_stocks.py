# Source: https://quantpedia.com/strategies/low-volatility-factor-effect-in-stocks-long-only-version/
# Clean-room re-implementation for the FundJohn BaseStrategy contract.
# Original reference: QuantConnect LEAN / paperswithbacktest/awesome-systematic-trading
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import large_cap as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_ast_low_volatility_factor_effect_in_stocks"


class AstLowVolatilityFactorEffectInStocks(BaseStrategy):
    """Long the bottom-quartile (lowest 3-year weekly-return volatility) of large-cap US stocks, equally weighted, rebalancing monthly."""

    id          = STRATEGY_ID
    name        = "AstLowVolatilityFactorEffectInStocks"
    description = (
        "At month-end, rank large-cap US stocks by 3-year weekly-return volatility (std-dev of "
        "non-overlapping 5-day return blocks); go long the bottom quartile equally weighted."
    )
    tier = 2
    signal_frequency  = "monthly"
    min_lookback      = 252
    active_in_regimes = ["LOW_VOL", "TRANSITIONING"]
    MAX_SIGNALS       = 50

    VOL_WINDOW   = 252   # ~12*21 trading days (3-year weekly-return vol lookback)
    MIN_UNIVERSE = 20    # minimum tickers after filtering for a valid quartile selection

    def default_parameters(self) -> dict:
        return {
            "vol_window":   self.VOL_WINDOW,
            "min_universe": self.MIN_UNIVERSE,
        }

    def _is_month_end(self, prices: pd.DataFrame) -> bool:
        """True if the last date in prices is the last trading day of its month."""
        if prices.index.empty:
            return False
        last_date = prices.index[-1]
        next_dates = prices.index[prices.index > last_date]
        if len(next_dates) == 0:
            return True   # end of data — treat as month-end
        return next_dates[0].month != last_date.month

    def _weekly_vol(self, closes: pd.Series, vol_window: int) -> float:
        """Std-dev of non-overlapping 5-day (weekly) return blocks over lookback window."""
        arr = closes.values[-vol_window:]
        if len(arr) < 10:
            return np.nan
        # Split into non-overlapping 5-day blocks
        blocks = [arr[i:i + 5] for i in range(0, len(arr) - 4, 5) if len(arr[i:i + 5]) == 5]
        if len(blocks) < 2:
            return np.nan
        # Weekly return: (first_price - last_price) / last_price  (LEAN convention: closes[0] is newest in window)
        weekly_returns = [(b[0] - b[-1]) / b[-1] for b in blocks if b[-1] != 0]
        if len(weekly_returns) < 2:
            return np.nan
        return float(np.std(weekly_returns))

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print("[debug] signals=0", file=sys.stderr)
            return []

        regime_state = regime.get("state", "LOW_VOL")
        if not self.should_run(regime_state):
            print("[debug] signals=0", file=sys.stderr)
            return []

        # Only rebalance at month-end
        if not self._is_month_end(prices):
            print("[debug] signals=0", file=sys.stderr)
            return []

        vol_window   = int(self.parameters.get("vol_window",   self.VOL_WINDOW))
        min_universe = int(self.parameters.get("min_universe", self.MIN_UNIVERSE))
        scale        = self.position_scale(regime_state)

        # Filter to universe tickers with enough price history
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < min_universe:
            print("[debug] signals=0", file=sys.stderr)
            return []

        # Need at least vol_window bars
        recent = prices.tail(vol_window + 5)
        if len(recent) < vol_window // 2:
            print("[debug] signals=0", file=sys.stderr)
            return []

        # Compute weekly volatility for each ticker
        vol_scores: dict[str, float] = {}
        for ticker in tickers:
            col = recent[ticker].dropna()
            if len(col) < vol_window // 2:
                continue
            v = self._weekly_vol(col, vol_window)
            if np.isnan(v) or v <= 0:
                continue
            vol_scores[ticker] = v

        if len(vol_scores) < min_universe:
            print("[debug] signals=0", file=sys.stderr)
            return []

        # Sort ascending by volatility; take bottom quartile (lowest vol)
        sorted_vol = sorted(vol_scores.items(), key=lambda x: x[1])
        quartile_n = max(1, len(sorted_vol) // 4)
        long_stocks = sorted_vol[:quartile_n]

        last_prices = recent.iloc[-1]
        n_long = len(long_stocks)
        base_size = float(scale / n_long)

        signals: List[Signal] = []
        for ticker, vol in long_stocks:
            price = float(last_prices.get(ticker, 0))
            if price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                recent[ticker].dropna(), "LONG", price, regime_state=regime_state
            )
            size = min(base_size, 0.05)   # single-name cap
            signals.append(Signal(
                ticker            = ticker,
                direction         = "LONG",
                entry_price       = price,
                stop_loss         = float(stops["stop"]),
                target_1          = float(stops["t1"]),
                target_2          = float(stops["t2"]),
                target_3          = float(stops["t3"]),
                position_size_pct = size,
                confidence        = "MED",
                signal_params     = {
                    "weekly_vol":    round(vol, 6),
                    "quartile_rank": sorted_vol.index((ticker, vol)) + 1,
                    "n_universe":    len(vol_scores),
                },
            ))

        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
