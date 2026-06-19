# Source: https://quantpedia.com/strategies/betting-against-beta-factor-in-stocks/
# Clean-room re-implementation for the FundJohn BaseStrategy contract.
# Original reference: QuantConnect LEAN / paperswithbacktest/awesome-systematic-trading
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import no_otc as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_ast_betting_against_beta_factor_in_stocks"


class BettingAgainstBetaFactorInStocks(BaseStrategy):
    """Long lowest-beta decile, short highest-beta decile; both rescaled to beta≈1 for a zero-beta spread."""

    id          = STRATEGY_ID
    name        = "BettingAgainstBetaFactorInStocks"
    description = (
        "Monthly rebalance: long bottom-beta-decile (leverage = 1/mean_beta_long, cap 2×), "
        "short top-beta-decile (leverage = 1/mean_beta_short, cap 2×), zero-net-beta portfolio."
    )
    tier = 2
    signal_frequency  = "monthly"
    min_lookback      = 252
    active_in_regimes = ["LOW_VOL", "TRANSITIONING"]
    MAX_SIGNALS       = 100   # up to 50 long + 50 short

    BETA_WINDOW     = 252      # 12×21 ≈ 252 trading days
    LEVERAGE_CAP    = 2.0
    MIN_UNIVERSE    = 30       # need enough stocks for deciles
    BASE_SIZE_LONG  = 0.006    # base per-stock pct (adjusted by leverage)
    BASE_SIZE_SHORT = 0.006

    def default_parameters(self) -> dict:
        return {
            "beta_window":   self.BETA_WINDOW,
            "leverage_cap":  self.LEVERAGE_CAP,
            "min_universe":  self.MIN_UNIVERSE,
        }

    def _is_month_start(self, prices: pd.DataFrame) -> bool:
        """True if the last date in prices is the first trading day of its month."""
        if prices.index.empty:
            return False
        last_date = prices.index[-1]
        prev_dates = prices.index[prices.index < last_date]
        if len(prev_dates) == 0:
            return True
        return prev_dates[-1].month != last_date.month

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        regime_state = regime.get("state", "LOW_VOL")
        if not self.should_run(regime_state):
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        # Only rebalance at month-start
        if not self._is_month_start(prices):
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        beta_window   = int(self.parameters.get("beta_window",  self.BETA_WINDOW))
        leverage_cap  = float(self.parameters.get("leverage_cap", self.LEVERAGE_CAP))
        min_universe  = int(self.parameters.get("min_universe",  self.MIN_UNIVERSE))
        scale         = self.position_scale(regime_state)

        # Need SPY as market proxy
        if "SPY" not in prices.columns:
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        # Filter universe to available tickers with enough history
        tickers = [t for t in universe if t in prices.columns and t != "SPY"]
        if len(tickers) < min_universe:
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        # Use only the beta_window lookback
        window_prices = prices.tail(beta_window + 1)
        if len(window_prices) < beta_window // 2:
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        spy_returns = window_prices["SPY"].pct_change().dropna()
        market_var  = float(spy_returns.var())
        if market_var == 0 or np.isnan(market_var):
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        # Compute beta for each ticker
        betas: dict[str, float] = {}
        for ticker in tickers:
            col = window_prices[ticker].dropna()
            if len(col) < beta_window // 2:
                continue
            stock_ret = col.pct_change().dropna()
            # Align
            aligned = pd.concat([stock_ret, spy_returns], axis=1, join="inner")
            aligned.columns = ["stock", "mkt"]
            if len(aligned) < 60:
                continue
            cov = float(aligned["stock"].cov(aligned["mkt"]))
            beta = cov / market_var
            if np.isnan(beta) or np.isinf(beta):
                continue
            betas[ticker] = beta

        if len(betas) < 10:
            print(f"[debug] signals=0", file=sys.stderr)
            return []

        sorted_betas = sorted(betas.items(), key=lambda x: x[1])
        decile_n     = max(1, len(sorted_betas) // 10)

        long_stocks  = sorted_betas[:decile_n]   # lowest beta
        short_stocks = sorted_betas[-decile_n:]  # highest beta

        # Leverage: 1 / mean_beta, capped
        long_mean_beta  = float(np.mean([b for _, b in long_stocks]))
        short_mean_beta = float(np.mean([b for _, b in short_stocks]))

        long_lvg  = min(leverage_cap, 1.0 / long_mean_beta)  if long_mean_beta  > 0 else leverage_cap
        short_lvg = min(leverage_cap, 1.0 / short_mean_beta) if short_mean_beta > 0 else leverage_cap
        long_lvg  = max(0.1, long_lvg)
        short_lvg = max(0.1, short_lvg)

        n_long  = len(long_stocks)
        n_short = len(short_stocks)

        signals: List[Signal] = []
        last_prices = window_prices.iloc[-1]

        for ticker, beta in long_stocks:
            price = float(last_prices.get(ticker, 0))
            if price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                window_prices[ticker].dropna(), "LONG", price, regime_state=regime_state
            )
            size = float(scale * long_lvg / n_long)
            size = min(size, 0.05)   # single-name cap
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
                signal_params     = {"beta": round(beta, 4), "leg": "long_low_beta",
                                     "long_lvg": round(long_lvg, 3)},
            ))

        for ticker, beta in short_stocks:
            price = float(last_prices.get(ticker, 0))
            if price <= 0:
                continue
            stops = self.compute_stops_and_targets(
                window_prices[ticker].dropna(), "SHORT", price, regime_state=regime_state
            )
            size = float(scale * short_lvg / n_short)
            size = min(size, 0.05)
            signals.append(Signal(
                ticker            = ticker,
                direction         = "SHORT",
                entry_price       = price,
                stop_loss         = float(stops["stop"]),
                target_1          = float(stops["t1"]),
                target_2          = float(stops["t2"]),
                target_3          = float(stops["t3"]),
                position_size_pct = size,
                confidence        = "MED",
                signal_params     = {"beta": round(beta, 4), "leg": "short_high_beta",
                                     "short_lvg": round(short_lvg, 3)},
            ))

        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
