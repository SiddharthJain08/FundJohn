from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['VolumeReturnAutocorrLMSW']


class VolumeReturnAutocorrLMSW(BaseStrategy):
    """Volume-return autocorrelation: C2 predicts continuation (informed) vs reversal (risk-sharing)."""

    id          = 'S_volume_return_autocorr_lmsw'
    name        = 'VolumeReturnAutocorrLMSW'
    description = 'LMSW (2002): C2 coefficient from volume-interacted return regression ranks stocks for long/short.'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    # Lookback windows
    EMA_WINDOW    = 63    # volume detrending
    REG_WINDOW    = 252   # rolling OLS window
    MIN_OBS       = 180   # min valid observations in window
    QUINTILE      = 0.20  # top/bottom 20%

    def generate_signals(self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        # Need 'close' and 'volume' columns
        if 'close' not in prices.columns or 'volume' not in prices.columns:
            print(f'[debug] signals=0 (missing close/volume columns)', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns.get_level_values(0)]
        if not tickers and prices.index.name != 'date':
            # Try flat structure: prices has MultiIndex columns (ticker, field)
            tickers = universe

        scores = {}
        last_close = {}

        # Determine data shape: MultiIndex columns (ticker, field) or flat
        has_multiindex = isinstance(prices.columns, pd.MultiIndex)

        for ticker in universe:
            try:
                if has_multiindex:
                    if ticker not in prices.columns.get_level_values(0):
                        continue
                    close_s = prices[ticker]['close'].dropna()
                    vol_s   = prices[ticker]['volume'].dropna()
                else:
                    # Flat single-ticker or wide format
                    if 'close' not in prices.columns:
                        continue
                    close_s = prices['close'].dropna()
                    vol_s   = prices['volume'].dropna()

                # Align
                idx = close_s.index.intersection(vol_s.index)
                if len(idx) < self.REG_WINDOW + 10:
                    continue
                close_s = close_s.loc[idx]
                vol_s   = vol_s.loc[idx]

                # Log returns
                ret = np.log(close_s / close_s.shift(1)).dropna()
                log_vol = np.log(vol_s.clip(lower=1))

                # Detrend log volume: subtract EMA(63)
                vol_ema = log_vol.ewm(span=self.EMA_WINDOW, adjust=False).mean()
                v_detrend = log_vol - vol_ema

                # Align all series
                common = ret.index.intersection(v_detrend.index)
                ret = ret.loc[common]
                v_detrend = v_detrend.loc[common]

                if len(ret) < self.REG_WINDOW + 10:
                    continue

                # Rolling OLS over last REG_WINDOW observations
                # r_t = C0 + C1*r_{t-1} + C2*V_{t-1}*r_{t-1} + eps
                window_ret = ret.iloc[-self.REG_WINDOW:]
                window_v   = v_detrend.iloc[-self.REG_WINDOW:]

                r_t   = window_ret.iloc[1:].values
                r_lag = window_ret.iloc[:-1].values
                v_lag = window_v.iloc[:-1].values

                n = len(r_t)
                if n < self.MIN_OBS:
                    continue

                X = np.column_stack([np.ones(n), r_lag, v_lag * r_lag])
                XtX = X.T @ X
                Xty = X.T @ r_t

                try:
                    cond = np.linalg.cond(XtX)
                    if cond > 1e12:
                        continue
                    beta = np.linalg.solve(XtX, Xty)
                except np.linalg.LinAlgError:
                    continue

                c2 = float(beta[2])

                # Signal score: C2 * V_{t-1} * r_{t-1} (last available)
                last_v = float(v_lag[-1])
                last_r = float(r_lag[-1])
                score = c2 * last_v * last_r

                if has_multiindex:
                    last_price = float(prices[ticker]['close'].dropna().iloc[-1])
                else:
                    last_price = float(close_s.iloc[-1])

                scores[ticker]     = score
                last_close[ticker] = last_price

            except Exception:
                continue

        if len(scores) < 10:
            print(f'[debug] signals=0 (insufficient scored tickers={len(scores)})', file=sys.stderr)
            return []

        score_series = pd.Series(scores)
        n_tickers    = len(score_series)
        n_quintile   = max(1, int(n_tickers * self.QUINTILE))

        top_tickers    = score_series.nlargest(n_quintile).index.tolist()
        bottom_tickers = score_series.nsmallest(n_quintile).index.tolist()

        base_size = (scale * 0.80) / max(1, len(top_tickers) + len(bottom_tickers))
        base_size = min(base_size, 0.04)

        signals: List[Signal] = []

        for ticker in top_tickers:
            price = last_close[ticker]
            stops = self.compute_stops_and_targets(
                pd.Series([price]), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop_loss'],
                target_1=stops['target_1'],
                target_2=stops['target_2'],
                target_3=stops['target_3'],
                position_size_pct=base_size,
                confidence='MED',
                signal_params={
                    'c2_score': float(score_series[ticker]),
                    'strategy': 'volume_return_autocorr_lmsw',
                },
            ))

        for ticker in bottom_tickers:
            price = last_close[ticker]
            stops = self.compute_stops_and_targets(
                pd.Series([price]), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop_loss'],
                target_1=stops['target_1'],
                target_2=stops['target_2'],
                target_3=stops['target_3'],
                position_size_pct=base_size,
                confidence='MED',
                signal_params={
                    'c2_score': float(score_series[ticker]),
                    'strategy': 'volume_return_autocorr_lmsw',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
