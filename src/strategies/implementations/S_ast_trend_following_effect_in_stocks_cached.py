"""measurement twin — never register; emits the parent's signals

`_cached` twin of S_ast_trend_following_effect_in_stocks (six-repo D2d). Only the
per-bar full-history scans (dropna / max / diff.abs.rolling per ticker) are
replaced by BaseStrategy.cached() slices; every gate, rounding and print is the
parent's. Used solely by scripts/measure_indicator_cache.py; registering it would double the
parent's signals live. The ONLY sanctioned measurement command is `scripts/measure_indicator_cache.py --strategy <id>_cached --compare-to <id>`: `unified_backtest --strategy-file` derives the sid from the file stem, so no manifest tier cap applies and the run is not comparable to the parent (full static universe, heavy on the 8 GB box).
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import Signal
import strategies.implementations.S_ast_trend_following_effect_in_stocks as _orig

INSTRUMENT_CLASS = _orig.INSTRUMENT_CLASS
universe_filter = _orig.universe_filter

__all__ = ['AstTrendFollowingEffectInStocks']


class AstTrendFollowingEffectInStocks(_orig.AstTrendFollowingEffectInStocks):
    """Measurement twin: parent's ATH-breakout signals via cached indicator slices."""

    id = 'S_ast_trend_following_effect_in_stocks_cached'

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        last = prices.index[-1]
        n_valid = self.cached('nvalid', lambda p: p.notna().cumsum(), prices).loc[last]
        cur = self.cached('ffill', lambda p: p.ffill(), prices).loc[last]
        hmax = self.cached('cummax_ffill', lambda p: p.cummax().ffill(), prices).loc[last]

        # Identify ATH breakout candidates
        candidates = []
        for ticker in tickers:
            if n_valid[ticker] < self.ATR_PERIOD + 5:
                continue

            if float(cur[ticker]) < self.MIN_PRICE:
                continue

            # ATH signal: today's close must equal the historical maximum
            if float(cur[ticker]) < float(hmax[ticker]):
                continue

            series = prices[ticker].dropna()
            current_close = float(series.iloc[-1])

            # ATR(10) proxy using close-to-close abs diff
            atr_raw = series.diff().abs().rolling(self.ATR_PERIOD).mean().iloc[-1]
            atr_val = float(atr_raw) if (pd.notna(atr_raw) and float(atr_raw) > 0) else current_close * 0.02

            candidates.append((ticker, current_close, atr_val))

        if not candidates:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Equal-weight across all candidates, capped at MAX_BASKET
        basket = candidates[:self.MAX_BASKET]
        n = len(basket)
        per_position = round(min(1.0 / n, self.BASE_SIZE) * scale, 6)

        signals: List[Signal] = []
        for ticker, price, atr_val in basket:
            trailing_stop = price - atr_val
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=per_position,
                confidence='HIGH',
                signal_params={
                    'atr10': round(atr_val, 4),
                    'trailing_stop': round(trailing_stop, 4),
                    'hist_max': round(float(prices[ticker].dropna().max()), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
