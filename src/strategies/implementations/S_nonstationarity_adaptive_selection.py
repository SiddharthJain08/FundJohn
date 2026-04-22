from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['NonStationarityAdaptiveSelection']


class NonStationarityAdaptiveSelection(BaseStrategy):
    """Tournament-select momentum windows; LONG top quintile, SHORT bottom quintile by predicted return."""

    id          = 'S_nonstationarity_adaptive_selection'
    name        = 'NonStationarityAdaptiveSelection'
    description = 'Tournament-select momentum windows validated on rolling nonstationary windows; rank LONG/SHORT'
    tier        = 2
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    WINDOWS     = [21, 42, 63]
    EVAL_WINDOW = 21   # held-out period for IC validation
    TOP_FRAC    = 0.20
    BOT_FRAC    = 0.20
    SIZE_PER    = 0.04

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        # Filter to available tickers
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (insufficient tickers {len(tickers)})', file=sys.stderr)
            return []

        prices_sub = prices[tickers].dropna(axis=1, how='all')
        tickers = list(prices_sub.columns)

        min_rows = max(self.WINDOWS) + self.EVAL_WINDOW + 5
        if len(prices_sub) < min_rows:
            print(f'[debug] signals=0 (insufficient rows {len(prices_sub)})', file=sys.stderr)
            return []

        # Tournament: select window with best rank-IC on held-out period
        best_window = self._tournament_select(prices_sub)

        # Score tickers with winning window (lag-1 to avoid lookahead)
        scores = self._compute_scores(prices_sub, best_window)
        if scores is None or scores.empty:
            print(f'[debug] signals=0 (no scores)', file=sys.stderr)
            return []

        n = len(scores)
        top_n = max(1, int(n * self.TOP_FRAC))
        bot_n = max(1, int(n * self.BOT_FRAC))

        longs  = scores.nlargest(top_n).index.tolist()
        shorts = scores.nsmallest(bot_n).index.tolist()

        current_prices = prices_sub.iloc[-1]
        size_per = round(scale * self.SIZE_PER, 4)
        signals: List[Signal] = []

        for ticker in longs[:25]:
            cp = float(current_prices.get(ticker, 0) or 0)
            if cp <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', cp, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=cp,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_per,
                confidence='MED',
                signal_params={'window': best_window, 'score': round(float(scores.get(ticker, 0.5)), 4)},
            ))

        for ticker in shorts[:25]:
            cp = float(current_prices.get(ticker, 0) or 0)
            if cp <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', cp, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=cp,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_per,
                confidence='MED',
                signal_params={'window': best_window, 'score': round(float(scores.get(ticker, 0.5)), 4)},
            ))

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    def _tournament_select(self, prices: pd.DataFrame) -> int:
        """Select window with highest rank-IC on held-out evaluation period."""
        best_window = self.WINDOWS[0]
        best_ic = -np.inf
        eval_end = len(prices) - 1
        eval_start = eval_end - self.EVAL_WINDOW

        for w in self.WINDOWS:
            pred_idx = eval_start - 1
            if pred_idx < w:
                continue
            past = prices.iloc[pred_idx - w:pred_idx]
            if len(past) < w // 2:
                continue
            # Predicted returns (momentum over window)
            pred_ret = past.iloc[-1] / past.iloc[0].replace(0, np.nan) - 1
            # Actual forward returns over eval period
            fwd = prices.iloc[eval_start:eval_end]
            if len(fwd) < 2:
                continue
            act_ret = fwd.iloc[-1] / fwd.iloc[0].replace(0, np.nan) - 1
            common = pred_ret.dropna().index.intersection(act_ret.dropna().index)
            if len(common) < 5:
                continue
            ic = float(pred_ret[common].rank().corr(act_ret[common].rank()))
            if not np.isnan(ic) and ic > best_ic:
                best_ic = ic
                best_window = w

        return best_window

    def _compute_scores(self, prices: pd.DataFrame, window: int) -> pd.Series:
        """Compute lag-1 momentum scores for each ticker over selected window."""
        if len(prices) < window + 2:
            return None
        # Lag 1: exclude most recent row to avoid lookahead
        end = len(prices) - 1
        start = end - window
        if start < 0:
            return None
        segment = prices.iloc[start:end]
        base = segment.iloc[0].replace(0, np.nan)
        scores = segment.iloc[-1] / base - 1
        return scores.dropna()
