# Source: Safari & Schmidhuber (2026) arXiv:2606.20145
# "Trends, Volatility, Correlations, and Critical Phenomena in Financial Markets"
# Clean-room re-implementation for the FundJohn BaseStrategy contract.
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_trend_vol_quadratic_forecast'

__all__ = ['TrendVolQuadraticForecast']


class TrendVolQuadraticForecast(BaseStrategy):
    """Quadratic polynomial of exponentially-weighted trend strength forecasts next-day variance.

    Source: Safari & Schmidhuber (2026) arXiv:2606.20145 §2-3.
    phi_t = exp-weighted log-return average (w_n = n*exp(-2n/T)); predicts next-day
    variance via quadratic: predicted_var = a + b*RV + c*phi + d*phi^2.
    Position sized by vol-targeting (target_vol / sqrt(predicted_var_ann)).
    Long top-ranked stocks (highest phi), short bottom-ranked (lowest phi).
    """

    id               = STRATEGY_ID
    name             = 'TrendVolQuadraticForecast'
    description      = ('Quadratic polynomial of trend strength predicts next-day vol; '
                        'long top-trend stocks, short bottom-trend stocks, vol-targeted sizing.')
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 1260
    active_in_regimes = ['HIGH_VOL', 'LOW_VOL', 'TRANSITIONING']

    T_HALF      = 63    # exponential decay half-life for trend weights (§2: primary horizon)
    RV_WINDOW   = 63    # realized variance lookback (days)
    TOP_N       = 10    # max LONG signals
    BOTTOM_N    = 10    # max SHORT signals
    TARGET_VOL  = 0.15  # annualized vol target per position (15%)
    THRESHOLD   = 0.05  # min |phi_pct| to emit a signal (% daily return units)
    BASE_SIZE   = 0.025 # base position fraction before vol-scaling

    # Quadratic model coefficients from §3 (calibrated on pct-unit phi, annualized RV)
    _A = 0.13
    _B = 0.79
    _C = -0.06
    _D = 0.09

    def _compute_phi(self, log_rets: np.ndarray) -> float:
        """Exponentially-weighted trend strength phi_t in % daily return units.

        w(n) = n * exp(-2n/T) where n=1 is most recent lag, T=T_HALF.
        Returns phi in percentage units (multiply decimal returns by 100).
        """
        T = self.T_HALF
        n_lags = min(len(log_rets), T * 6)
        if n_lags < 20:
            return float('nan')
        # most-recent-first slice, convert to pct
        rets_pct = log_rets[-n_lags:][::-1] * 100.0
        lags = np.arange(1, n_lags + 1, dtype=np.float64)
        w = lags * np.exp(-2.0 * lags / T)
        w /= w.sum()
        return float(np.dot(w, rets_pct))

    def _forecast_ann_var(self, log_rets: np.ndarray, phi_pct: float) -> float:
        """Forecast annualized next-day variance using the quadratic model (§3).

        predicted_var_ann = A + B*rv_ann + C*phi + D*phi^2
        where phi is in % daily return units and rv_ann = 252 * daily_variance.
        """
        window = min(len(log_rets), self.RV_WINDOW)
        if window < 5:
            return float('nan')
        rv_ann = float(252.0 * np.var(log_rets[-window:] * 100.0))  # pct^2 annualized
        predicted = self._A + self._B * rv_ann + self._C * phi_pct + self._D * (phi_pct ** 2)
        return max(predicted, 0.01)  # floor at 1% annualized variance

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
        if len(tickers) < 5:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Compute phi and predicted variance for each ticker
        scored = []
        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < self.min_lookback:
                continue
            current_price = float(series.iloc[-1])
            if current_price < 2.0:
                continue

            log_rets = np.log(series.values / series.shift(1).values[1:])  # array of log returns
            log_rets = log_rets[np.isfinite(log_rets)]
            if len(log_rets) < self.RV_WINDOW:
                continue

            phi = self._compute_phi(log_rets)
            if not np.isfinite(phi):
                continue

            pred_var = self._forecast_ann_var(log_rets, phi)
            if not np.isfinite(pred_var):
                continue

            scored.append((ticker, phi, pred_var, current_price))

        if not scored:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Rank by phi; top → LONG, bottom → SHORT
        scored.sort(key=lambda x: x[1], reverse=True)

        signals: List[Signal] = []

        # LONG: top-ranked tickers with phi > threshold
        long_candidates = [(t, p, v, px) for t, p, v, px in scored[:self.TOP_N] if p > self.THRESHOLD]
        for ticker, phi, pred_var, price in long_candidates:
            # Vol-target sizing: target_vol / sqrt(pred_var) normalizes by annual vol forecast
            pred_vol = (pred_var ** 0.5)  # pct annual vol
            vol_scale_factor = self.TARGET_VOL * 100.0 / max(pred_vol, 5.0)
            pos_size = round(min(self.BASE_SIZE * vol_scale_factor * scale, 0.15), 6)
            if pos_size <= 0:
                continue
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
                position_size_pct=pos_size,
                confidence='HIGH' if abs(phi) > 0.2 else 'MED',
                signal_params={
                    'phi_pct': round(phi, 4),
                    'pred_ann_var_pct2': round(pred_var, 4),
                    'vol_scale_factor': round(vol_scale_factor, 4),
                },
            ))

        # SHORT: bottom-ranked tickers with phi < -threshold
        short_candidates = [(t, p, v, px) for t, p, v, px in scored[-self.BOTTOM_N:] if p < -self.THRESHOLD]
        for ticker, phi, pred_var, price in short_candidates:
            pred_vol = (pred_var ** 0.5)
            vol_scale_factor = self.TARGET_VOL * 100.0 / max(pred_vol, 5.0)
            pos_size = round(min(self.BASE_SIZE * vol_scale_factor * scale, 0.15), 6)
            if pos_size <= 0:
                continue
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=pos_size,
                confidence='HIGH' if abs(phi) > 0.2 else 'MED',
                signal_params={
                    'phi_pct': round(phi, 4),
                    'pred_ann_var_pct2': round(pred_var, 4),
                    'vol_scale_factor': round(vol_scale_factor, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
