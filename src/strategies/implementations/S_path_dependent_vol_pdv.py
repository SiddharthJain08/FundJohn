"""
S_path_dependent_vol_pdv — Path-Dependent Volatility (Guyon & Lekeufack 2023)
Up to 90% of IV variance is explained by power-law-weighted sums of past returns
and squared returns; trade ATM straddles when market IV deviates from PDV forecast.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['PathDependentVolPDV']


class PathDependentVolPDV(BaseStrategy):
    """BUY/SELL_VOL straddles when ATM IV deviates from PDV power-law vol forecast."""

    id          = 'S_path_dependent_vol_pdv'
    name        = 'Path-Dependent Volatility PDV'
    description = (
        'BUY_VOL/SELL_VOL ATM straddle when market IV z-score vs PDV forecast '
        'exceeds 1.5σ; PDV = power-law kernel on squared returns — Guyon 2023'
    )
    tier         = 2
    min_lookback = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'gamma_rv':    0.60,   # power-law decay exponent for squared-return kernel
            'lookback':    504,    # bars for PDV factor weight span
            'zscore_win':  63,     # rolling window for IV-PDV spread z-score
            'threshold':   1.5,   # |z| threshold for signal entry
            'base_size':   0.025,  # base position size pct per signal
            'max_signals': 10,
        }

    # ── PDV kernel ──────────────────────────────────────────────────────────────

    def _pdv_vol_series(self, sq_rets: np.ndarray, gamma: float, lb: int) -> np.ndarray:
        """
        Rolling power-law weighted vol forecast: sqrt(Σ_k k^{-gamma} * r_{t-k}^2) * sqrt(252).
        Uses causal np.convolve for efficiency; first (lb-1) bars are NaN (partial window).
        """
        weights = np.array([(k + 1) ** (-gamma) for k in range(lb)], dtype=float)
        weights /= weights.sum()  # normalise → weighted-average of sq_rets
        T = len(sq_rets)
        if T < lb:
            return np.full(T, np.nan)
        conv = np.convolve(sq_rets, weights, mode='full')[:T]
        conv[:lb - 1] = np.nan
        return np.sqrt(np.maximum(conv, 0.0)) * np.sqrt(252)

    # ── IV extraction ──────────────────────────────────────────────────────────

    def _atm_iv_series(self, options_df, ticker: str) -> 'pd.Series | None':
        """Return date-indexed ATM IV series for ticker from options_eod DataFrame."""
        if options_df is None or options_df.empty:
            return None
        try:
            tkr_col  = next((c for c in ['ticker', 'symbol', 'underlyingSymbol']
                             if c in options_df.columns), None)
            iv_col   = next((c for c in ['impliedVolatility', 'implied_volatility', 'iv']
                             if c in options_df.columns), None)
            date_col = next((c for c in ['date', 'quoteDate', 'quote_date']
                             if c in options_df.columns), None)
            if tkr_col is None or iv_col is None or date_col is None:
                return None
            sub = options_df[options_df[tkr_col] == ticker][[date_col, iv_col]].copy()
            if sub.empty:
                return None
            sub[date_col] = pd.to_datetime(sub[date_col])
            iv_s = sub.groupby(date_col)[iv_col].median().sort_index().astype(float)
            iv_s = iv_s[iv_s > 0]
            return iv_s if len(iv_s) >= 10 else None
        except Exception:
            return None

    # ── Main signal engine ─────────────────────────────────────────────────────

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

        p       = self.parameters
        opts_df = (aux_data or {}).get('options_eod')
        scale   = self.position_scale(regime_state)
        scored: list[tuple[str, float, str]] = []

        candidates = [t for t in universe if t in prices.columns][:self.MAX_SIGNALS * 5]

        for ticker in candidates:
            ts = prices[ticker].dropna()
            if len(ts) < p['lookback']:
                continue

            log_rets = np.log(ts / ts.shift(1)).dropna().values
            sq_rets  = log_rets ** 2

            # Rolling PDV series
            pdv_arr = self._pdv_vol_series(sq_rets, p['gamma_rv'], p['lookback'])

            # ATM IV series (required — PDV signal needs real IV to trade)
            iv_series = self._atm_iv_series(opts_df, ticker)
            if iv_series is None:
                continue

            # Align IV and PDV on date index
            ret_dates = ts.index[1:]  # one shorter due to diff
            pdv_s = pd.Series(pdv_arr, index=ret_dates)
            common = iv_series.index.intersection(pdv_s.index)
            if len(common) < p['zscore_win'] + 5:
                continue

            spread = iv_series.reindex(common) - pdv_s.reindex(common)
            spread = spread.dropna()
            if len(spread) < p['zscore_win'] + 5:
                continue

            roll     = spread.rolling(p['zscore_win'])
            z_series = (spread - roll.mean()) / (roll.std() + 1e-9)
            z_valid  = z_series.dropna()
            if z_valid.empty:
                continue

            latest_z = float(z_valid.iloc[-1])
            if np.isfinite(latest_z) and abs(latest_z) >= p['threshold']:
                direction = 'SELL_VOL' if latest_z > 0 else 'BUY_VOL'
                scored.append((ticker, latest_z, direction))

        # Rank by |z|, cap at max_signals
        scored.sort(key=lambda x: abs(x[1]), reverse=True)
        selected = scored[:p['max_signals']]

        signals: List[Signal] = []
        for ticker, z, direction in selected:
            ts    = prices[ticker].dropna()
            price = float(ts.iloc[-1])
            st_dir = 'LONG' if direction == 'BUY_VOL' else 'SHORT'
            stops  = self.compute_stops_and_targets(ts, st_dir, price,
                                                    regime_state=regime_state)
            conf   = 'HIGH' if abs(z) >= 2.5 else ('MED' if abs(z) >= 1.8 else 'LOW')
            size   = round(min(p['base_size'] * scale * min(abs(z) / p['threshold'], 2.0),
                               p['base_size'] * 2.0), 4)
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=round(price, 4),
                stop_loss=round(stops['stop'], 4),
                target_1=round(stops['t1'], 4),
                target_2=round(stops['t2'], 4),
                target_3=round(stops['t3'], 4),
                position_size_pct=size,
                confidence=conf,
                signal_params={
                    'pdv_zscore': round(z, 3),
                    'regime':     regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
