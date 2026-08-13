# Source: https://quantpedia.com/strategies/momentum-factor-effect-in-stocks/
# Clean-room re-implementation for the FundJohn BaseStrategy contract.
# Original reference: QuantConnect LEAN / paperswithbacktest/awesome-systematic-trading
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import no_otc as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_ast_momentum_factor_effect_in_stocks"


class AstMomentumFactorEffectInStocks(BaseStrategy):
    """Monthly UMD momentum: long top quintile, short bottom quintile by 12-1mo return."""

    id          = STRATEGY_ID
    name        = "AstMomentumFactorEffectInStocks"
    description = (
        "Monthly rebalance: rank all stocks by 12-month return (skip most recent month); "
        "long top quintile, short bottom quintile, equal-weight."
    )
    tier = 2
    signal_frequency  = "monthly"
    min_lookback      = 273   # 252 formation + 21 skip
    active_in_regimes = ["LOW_VOL", "TRANSITIONING"]
    MAX_SIGNALS       = 50   # 25 long + 25 short

    FORMATION_DAYS = 252     # ~12 months
    SKIP_DAYS      = 21      # skip most recent month
    QUINTILE_FRAC  = 0.20    # top/bottom 20%
    MIN_UNIVERSE   = 25      # need enough stocks for quintile
    BASE_SIZE_LONG  = 0.010
    BASE_SIZE_SHORT = 0.008

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
            print("[debug] signals=0", file=sys.stderr)
            return []

        regime_state = regime.get("state", "LOW_VOL")
        if not self.should_run(regime_state):
            print("[debug] signals=0", file=sys.stderr)
            return []

        # Monthly rebalance only
        if not (self._is_month_start(prices) or self.cadence_reset(regime)):
            print("[debug] signals=0", file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Filter to equity tickers with price data
        tickers = [
            t for t in universe
            if t in prices.columns
            and not t.startswith("^")
            and not t.endswith("=F")
            and "-USD" not in t
        ]
        if len(tickers) < self.MIN_UNIVERSE:
            print("[debug] signals=0", file=sys.stderr)
            return []

        need = self.FORMATION_DAYS + self.SKIP_DAYS
        if len(prices) < need:
            print("[debug] signals=0", file=sys.stderr)
            return []

        # Compute 12-1 momentum per ticker: price[t-skip] / price[t-need] - 1
        mom: dict[str, float] = {}
        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < need:
                continue
            p_start = float(series.iloc[-need])
            p_end   = float(series.iloc[-self.SKIP_DAYS])
            if p_start <= 0:
                continue
            mom[ticker] = p_end / p_start - 1.0

        if len(mom) < self.MIN_UNIVERSE:
            print("[debug] signals=0", file=sys.stderr)
            return []

        mom_series = pd.Series(mom)
        n          = len(mom_series)
        quintile_n = max(5, int(n * self.QUINTILE_FRAC))

        # Top quintile → LONG, bottom quintile → SHORT
        sorted_asc = mom_series.sort_values()
        short_names = sorted_asc.iloc[:quintile_n].index.tolist()
        long_names  = sorted_asc.iloc[-quintile_n:].index.tolist()

        current_prices = prices.iloc[-1]
        size_long  = round(self.BASE_SIZE_LONG  * scale, 6)
        size_short = round(self.BASE_SIZE_SHORT * scale, 6)
        rank_pct   = mom_series.rank(pct=True)

        signals: List[Signal] = []
        half_cap = self.MAX_SIGNALS // 2

        # LONG signals: top momentum quintile
        for ticker in reversed(long_names):
            if len(signals) >= half_cap:
                break
            raw = current_prices.get(ticker)
            if raw is None or raw != raw or raw <= 0:
                continue
            price = float(raw)
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), "LONG", price, regime_state=regime_state
            )
            rp = float(rank_pct.get(ticker, 0.8))
            confidence = "HIGH" if rp >= 0.90 else ("MED" if rp >= 0.75 else "LOW")
            signals.append(Signal(
                ticker            = ticker,
                direction         = "LONG",
                entry_price       = price,
                stop_loss         = float(stops["stop"]),
                target_1          = float(stops["t1"]),
                target_2          = float(stops["t2"]),
                target_3          = float(stops["t3"]),
                position_size_pct = size_long,
                confidence        = confidence,
                signal_params     = {
                    "momentum_12_1": round(float(mom_series.get(ticker, 0.0)), 4),
                    "rank_pct":      round(rp, 4),
                    "leg":           "long_top_quintile",
                },
            ))

        # SHORT signals: bottom momentum quintile
        for ticker in short_names:
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current_prices.get(ticker)
            if raw is None or raw != raw or raw <= 0:
                continue
            price = float(raw)
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), "SHORT", price, regime_state=regime_state
            )
            rp = float(rank_pct.get(ticker, 0.2))
            confidence = "HIGH" if rp <= 0.10 else ("MED" if rp <= 0.25 else "LOW")
            signals.append(Signal(
                ticker            = ticker,
                direction         = "SHORT",
                entry_price       = price,
                stop_loss         = float(stops["stop"]),
                target_1          = float(stops["t1"]),
                target_2          = float(stops["t2"]),
                target_3          = float(stops["t3"]),
                position_size_pct = size_short,
                confidence        = confidence,
                signal_params     = {
                    "momentum_12_1": round(float(mom_series.get(ticker, 0.0)), 4),
                    "rank_pct":      round(rp, 4),
                    "leg":           "short_bottom_quintile",
                },
            ))

        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals
