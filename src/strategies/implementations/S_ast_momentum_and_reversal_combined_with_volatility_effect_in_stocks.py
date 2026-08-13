# Source: https://quantpedia.com/strategies/momentum-and-reversal-combined-with-volatility-effect-in-stocks/
# Clean-room re-implementation for the FundJohn BaseStrategy contract.
# Original QuantConnect reference: paperswithbacktest/awesome-systematic-trading
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import no_otc as universe_filter

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_ast_momentum_and_reversal_combined_with_volatility_effect_in_stocks"

__all__ = ["AstMomentumAndReversalCombinedWithVolatilityEffectInStocks"]


class AstMomentumAndReversalCombinedWithVolatilityEffectInStocks(BaseStrategy):
    """Long/short intersection of top-quintile 6M momentum AND top-quintile realized volatility."""

    id          = STRATEGY_ID
    name        = "AstMomentumAndReversalCombinedWithVolatilityEffectInStocks"
    description = (
        "High-volatility stocks that are also top-6M momentum winners outperform, "
        "while high-volatility losers underperform — long/short the intersection of "
        "top-quintile return and top-quintile volatility among large-cap US equities."
    )
    tier = 2
    signal_frequency  = "monthly"
    min_lookback      = 140   # 126 + 5 skip + buffer
    active_in_regimes = ["LOW_VOL", "TRANSITIONING"]
    MAX_SIGNALS       = 50   # 25 long + 25 short

    LOOKBACK_DAYS  = 126    # ~6 months of trading days
    SKIP_DAYS      = 5      # ~7 calendar days in trading days
    QUINTILE_FRAC  = 0.20   # top/bottom 20%
    MIN_UNIVERSE   = 25     # minimum stocks to form quintiles
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

        # Filter to equity tickers only
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

        need = self.LOOKBACK_DAYS + self.SKIP_DAYS
        if len(prices) < need:
            print("[debug] signals=0", file=sys.stderr)
            return []

        # Compute 6M realized return and annualized volatility per ticker.
        # Formation window: bars from -need to -(SKIP_DAYS) — skips the most recent
        # ~7 calendar days to avoid microstructure bias (mirrors the reference impl).
        perf_vol: dict[str, tuple[float, float]] = {}
        end_idx = len(prices) - self.SKIP_DAYS
        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < need:
                continue
            window = series.iloc[max(0, len(series) - need) : len(series) - self.SKIP_DAYS]
            if len(window) < 20:
                continue
            p_start = float(window.iloc[0])
            p_end   = float(window.iloc[-1])
            if p_start <= 0 or p_end <= 0:
                continue
            ret = p_end / p_start - 1.0
            daily_rets = window.pct_change().dropna()
            if len(daily_rets) < 5:
                continue
            vol = float(daily_rets.std() * np.sqrt(252))
            perf_vol[ticker] = (ret, vol)

        if len(perf_vol) < self.MIN_UNIVERSE:
            print("[debug] signals=0", file=sys.stderr)
            return []

        items = list(perf_vol.items())
        n = len(items)
        quintile_n = max(5, int(n * self.QUINTILE_FRAC))

        # Sort by 6M return; identify top and bottom quintile tickers
        sorted_by_perf = sorted(items, key=lambda x: x[1][0], reverse=True)
        top_perf = {t for t, _ in sorted_by_perf[:quintile_n]}
        bot_perf = {t for t, _ in sorted_by_perf[-quintile_n:]}

        # Sort by annualized realized vol; identify top quintile (highest vol)
        sorted_by_vol = sorted(items, key=lambda x: x[1][1], reverse=True)
        top_vol = {t for t, _ in sorted_by_vol[:quintile_n]}

        # Intersection: long=high-mom+high-vol; short=low-mom+high-vol
        long_names  = list(top_perf & top_vol)
        short_names = list(bot_perf & top_vol)

        if not long_names and not short_names:
            print("[debug] signals=0", file=sys.stderr)
            return []

        current_prices = prices.iloc[-1]
        ret_series = pd.Series({t: v[0] for t, v in perf_vol.items()})
        rank_pct = ret_series.rank(pct=True)

        size_long  = round(self.BASE_SIZE_LONG  * scale, 6)
        size_short = round(self.BASE_SIZE_SHORT * scale, 6)

        signals: List[Signal] = []
        half_cap = self.MAX_SIGNALS // 2

        # LONG signals — sorted best momentum first
        for ticker in sorted(long_names, key=lambda t: perf_vol[t][0], reverse=True):
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
                    "momentum_6m":   round(perf_vol[ticker][0], 4),
                    "realized_vol":  round(perf_vol[ticker][1], 4),
                    "rank_pct":      round(rp, 4),
                    "leg":           "long_high_mom_high_vol",
                },
            ))

        # SHORT signals — sorted worst momentum first
        for ticker in sorted(short_names, key=lambda t: perf_vol[t][0]):
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
                    "momentum_6m":   round(perf_vol[ticker][0], 4),
                    "realized_vol":  round(perf_vol[ticker][1], 4),
                    "rank_pct":      round(rp, 4),
                    "leg":           "short_low_mom_high_vol",
                },
            ))

        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals
