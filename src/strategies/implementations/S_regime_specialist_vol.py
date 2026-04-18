"""
Regime-specialist volatility forecasting ensemble.
HIGH_VOL: GARCH-t / FIGARCH / HAR-RV (quasi-likelihood + underprediction penalty)
LOW_VOL / NEUTRAL: GRU / HAR-RV / XGBoost ensemble
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['RegimeSpecialistVol']


class RegimeSpecialistVol(BaseStrategy):
    """Regime-specialist vol ensemble: quasi-likelihood + underprediction penalty [§3.2]."""

    id                = 'S_regime_specialist_vol'
    name              = 'RegimeSpecialistVol'
    description       = 'IF HIGH_VOL: GARCH-t/FIGARCH/HAR-RV; ELSE GRU/HAR-RV/XGBoost vol ensemble'
    tier              = 2
    min_lookback      = 22
    active_in_regimes = ['HIGH_VOL', 'LOW_VOL', 'TRANSITIONING']

    _STOP_PCT   = 0.06
    _TARGET_PCT = 0.15
    _BUY_THRESHOLD  = 1.10   # forecast/current RV ratio to trigger BUY_VOL
    _SELL_THRESHOLD = 0.90   # ratio below which to trigger SELL_VOL
    _MAX_PER_SIDE   = 10

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
            return []

        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            return []

        closes = prices[tickers].ffill().dropna(how='all')
        if len(closes) < self.min_lookback:
            return []

        # Keep only tickers with sufficient history to avoid row-wise dropna killing everything
        min_obs = max(self.min_lookback, len(closes) // 2)
        valid = closes.columns[closes.notna().sum() >= min_obs].tolist()
        if not valid:
            return []
        closes = closes[valid].ffill().dropna(how='all')

        log_rets = np.log(closes / closes.shift(1)).dropna(how='all').iloc[1:]
        if log_rets.empty:
            return []

        rv_sq = log_rets ** 2
        rv_d = rv_sq.rolling(1).mean()
        rv_w = rv_sq.rolling(5).mean()
        rv_m = rv_sq.rolling(21).mean()

        # Require at least 21 days of RV history
        if rv_m.iloc[-1].isna().all():
            return []

        har_rv = 0.4 * rv_d + 0.3 * rv_w + 0.3 * rv_m

        if regime_state == 'HIGH_VOL':
            # GARCH-t approximation: short-span EWM (captures recent clustering)
            garch_approx = rv_sq.ewm(span=5, min_periods=5).mean()
            # FIGARCH approximation: long-memory EWM (hyperbolic decay)
            figarch_approx = rv_sq.ewm(span=63, min_periods=21).mean()
            # Ensemble: underprediction penalty upweights short-memory component
            vol_forecast = 0.45 * garch_approx + 0.25 * figarch_approx + 0.30 * har_rv
            ensemble_tag = 'garch_figarch_har'
        else:
            # GRU approximation: momentum-adjusted medium EWM
            gru_approx = (rv_sq.ewm(span=10, min_periods=5).mean()
                          * (1 + log_rets.rolling(5).mean())).clip(lower=0)
            # XGBoost approximation: weekly RV (regime-conditioned rank predictor)
            xgb_approx = rv_w
            vol_forecast = 0.35 * gru_approx + 0.35 * xgb_approx + 0.30 * har_rv
            ensemble_tag = 'gru_har_xgb'

        current_rv = rv_w.iloc[-1].replace(0, np.nan)
        forecast_now = vol_forecast.iloc[-1]
        vol_ratio = (forecast_now / current_rv).dropna()
        if vol_ratio.empty:
            return []

        last_close = closes.iloc[-1]
        scale = self.position_scale(regime_state)
        base_sz = round(min(scale * 0.04, 0.08), 4)

        signals: List[Signal] = []

        # BUY_VOL: forecast > current realized (vol expansion expected)
        buy_cands = vol_ratio[vol_ratio > self._BUY_THRESHOLD].sort_values(ascending=False).head(self._MAX_PER_SIDE)
        for ticker in buy_cands.index:
            price = float(last_close.get(ticker, np.nan))
            if np.isnan(price) or price <= 0:
                continue
            ratio = float(vol_ratio[ticker])
            conf = 'HIGH' if ratio > 1.30 else ('MED' if ratio > 1.15 else 'LOW')
            signals.append(Signal(
                ticker=ticker,
                direction='BUY_VOL',
                entry_price=round(price, 4),
                stop_loss=round(price * (1 - self._STOP_PCT), 4),
                target_1=round(price * 1.05, 4),
                target_2=round(price * 1.10, 4),
                target_3=round(price * (1 + self._TARGET_PCT), 4),
                position_size_pct=base_sz,
                confidence=conf,
                signal_params={'vol_ratio': round(ratio, 4), 'regime': regime_state, 'ensemble': ensemble_tag},
            ))

        # SELL_VOL: forecast < current realized (vol contraction expected)
        sell_cands = vol_ratio[vol_ratio < self._SELL_THRESHOLD].sort_values().head(self._MAX_PER_SIDE)
        for ticker in sell_cands.index:
            if len(signals) >= self.MAX_SIGNALS:
                break
            price = float(last_close.get(ticker, np.nan))
            if np.isnan(price) or price <= 0:
                continue
            ratio = float(vol_ratio[ticker])
            conf = 'HIGH' if ratio < 0.70 else ('MED' if ratio < 0.85 else 'LOW')
            signals.append(Signal(
                ticker=ticker,
                direction='SELL_VOL',
                entry_price=round(price, 4),
                stop_loss=round(price * (1 + self._STOP_PCT), 4),
                target_1=round(price * 0.95, 4),
                target_2=round(price * 0.90, 4),
                target_3=round(price * (1 - self._TARGET_PCT), 4),
                position_size_pct=base_sz,
                confidence=conf,
                signal_params={'vol_ratio': round(ratio, 4), 'regime': regime_state, 'ensemble': ensemble_tag},
            ))

        return signals[:self.MAX_SIGNALS]
