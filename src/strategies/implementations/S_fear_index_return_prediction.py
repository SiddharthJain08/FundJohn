"""
S_fear_index_return_prediction — Fear Index Return Prediction
Rubbaniy et al. (2014): Extreme VIX spikes predict positive 20-60 day
forward returns as fear-driven risk premium subsequently compresses.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['FearIndexReturnPrediction']


class FearIndexReturnPrediction(BaseStrategy):
    """Extreme VIX z-score spikes signal positive forward returns via fear-premium compression."""

    id          = 'S_fear_index_return_prediction'
    name        = 'Fear Index Return Prediction'
    description = (
        'LONG when VIX z-score (252d) > threshold — fear-driven selling creates '
        'risk premium that compresses as sentiment normalizes (Rubbaniy 2014)'
    )
    tier        = 2
    min_lookback = 504
    active_in_regimes = ['HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'zscore_threshold': 1.5,    # z-score above which fear spike is confirmed
            'lookback_days':    252,    # rolling window for VIX z-score
            'top_pct':          0.25,   # top quartile of universe by RV sensitivity
            'base_size_pct':    0.03,   # base position size per signal
            'max_size_pct':     0.06,   # cap per signal
            'rv_window':        21,     # realized vol window for cross-sectional ranking
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = (regime or {}).get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        macro_df = (aux_data or {}).get('macro')

        # ── 1. Extract VIX series ────────────────────────────────────────────
        vix_series = self._get_vix(macro_df)
        if vix_series is None or len(vix_series) < p['lookback_days']:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── 2. Compute rolling z-score ───────────────────────────────────────
        roll = vix_series.rolling(p['lookback_days'])
        zscore = (vix_series - roll.mean()) / (roll.std() + 1e-9)
        latest_z = float(zscore.iloc[-1])

        if latest_z < p['zscore_threshold']:
            print(f'[debug] signals=0 vix_z={latest_z:.2f}', file=sys.stderr)
            return []

        # ── 3. Rank universe by realized vol (IV-sensitivity proxy) ─────────
        scale = self.position_scale(regime_state)
        candidates = [t for t in universe if t in prices.columns]
        if not candidates:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rv_map: dict[str, float] = {}
        for ticker in candidates:
            ts = prices[ticker].dropna()
            if len(ts) < p['rv_window'] + 1:
                continue
            log_ret = np.log(ts / ts.shift(1)).dropna()
            rv = float(log_ret.iloc[-p['rv_window']:].std() * np.sqrt(252))
            rv_map[ticker] = rv

        if not rv_map:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sorted_tickers = sorted(rv_map, key=lambda t: rv_map[t], reverse=True)
        top_n = max(1, int(len(sorted_tickers) * p['top_pct']))
        selected = sorted_tickers[:top_n]

        # ── 4. Build signals ─────────────────────────────────────────────────
        conf = 'HIGH' if latest_z >= 2.0 else 'MED'
        signals: List[Signal] = []

        for ticker in selected:
            ts = prices[ticker].dropna()
            if ts.empty:
                continue
            current_price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(
                ts, 'LONG', current_price, regime_state=regime_state
            )
            size = min(p['base_size_pct'] * scale * min(latest_z / 1.5, 2.0),
                       p['max_size_pct'])
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=round(current_price, 4),
                stop_loss=round(stops['stop'], 4),
                target_1=round(stops['t1'], 4),
                target_2=round(stops['t2'], 4),
                target_3=round(stops['t3'], 4),
                position_size_pct=round(size, 4),
                confidence=conf,
                signal_params={
                    'vix_zscore':   round(latest_z, 3),
                    'rv_annualized': round(rv_map[ticker], 4),
                    'regime':       regime_state,
                },
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_vix(self, macro_df) -> 'pd.Series | None':
        """Extract VIX as a date-indexed Series from the long-format macro DataFrame."""
        if macro_df is None:
            return None
        if not isinstance(macro_df, pd.DataFrame):
            return None
        if macro_df.empty or 'series' not in macro_df.columns:
            return None
        vix_rows = macro_df[macro_df['series'] == 'VIX'].copy()
        if vix_rows.empty:
            return None
        vix_rows['date'] = pd.to_datetime(vix_rows['date'])
        vix_rows = vix_rows.sort_values('date').drop_duplicates('date')
        vix_series = vix_rows.set_index('date')['value'].astype(float)
        return vix_series
