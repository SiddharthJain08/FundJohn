"""
S_ivol_cross_section_quintile — Bali & Cakici (2008)
LONG bottom-quintile (low IVOL), SHORT top-quintile (high IVOL).
IVOL = std(FF3 OLS residuals) over trailing 22-day window.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['IvolCrossSectionQuintile']


class IvolCrossSectionQuintile(BaseStrategy):
    """Long low-IVOL / short high-IVOL quintiles; FF3 residual volatility cross-section."""

    id          = 'S_ivol_cross_section_quintile'
    name        = 'IvolCrossSectionQuintile'
    description = (
        'LONG bottom-quintile low-IVOL stocks, SHORT top-quintile high-IVOL stocks '
        'based on trailing-22-day FF3 residual volatility (Bali & Cakici 2008).'
    )
    tier             = 2
    min_lookback     = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'lookback': 22,           # trading-day window for IVOL estimation
            'min_price': 5.0,
            'min_vol_days': 15,       # min days with valid returns in lookback window
            'base_position_pct': 0.015,
        }

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        lookback = int(self.parameters.get('lookback', 22))
        min_price = float(self.parameters.get('min_price', 5.0))
        min_vol_days = int(self.parameters.get('min_vol_days', 15))
        base_pct  = float(self.parameters.get('base_position_pct', 0.015))

        # --- Filter universe ---
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers].copy()
        if len(prices_sub) < lookback + 5:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Latest prices for entry / screening
        last_prices = prices_sub.iloc[-1]
        price_ok = last_prices[last_prices >= min_price].index.tolist()
        if len(price_ok) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # --- Compute daily returns for the lookback window ---
        window_prices = prices_sub[price_ok].iloc[-(lookback + 1):]
        rets = window_prices.pct_change().iloc[1:]  # shape: lookback × n_tickers

        # --- Proxy MKT, SMB, HML from cross-sectional data ---
        # MKT = equal-weight market return (proxy)
        mkt = rets.mean(axis=1)

        # SMB proxy: bottom-30% mcap vs top-30% (use price as mcap rank proxy)
        price_rank = last_prices[price_ok].rank(pct=True)
        small_mask = price_rank[price_rank <= 0.30].index.tolist()
        large_mask = price_rank[price_rank >= 0.70].index.tolist()
        if small_mask and large_mask:
            smb = rets[small_mask].mean(axis=1) - rets[large_mask].mean(axis=1)
        else:
            smb = pd.Series(0.0, index=rets.index)

        # HML proxy: momentum quintile spread (value vs growth rank by recent performance)
        mom_21 = rets.sum()
        value_mask = mom_21[mom_21 <= mom_21.quantile(0.30)].index.tolist()
        growth_mask = mom_21[mom_21 >= mom_21.quantile(0.70)].index.tolist()
        if value_mask and growth_mask:
            hml = rets[value_mask].mean(axis=1) - rets[growth_mask].mean(axis=1)
        else:
            hml = pd.Series(0.0, index=rets.index)

        factors = pd.DataFrame({'mkt': mkt, 'smb': smb, 'hml': hml})

        # --- OLS FF3 residuals per ticker ---
        ivol_scores: dict[str, float] = {}
        for ticker in price_ok:
            y = rets[ticker].dropna()
            valid_idx = y.index.intersection(factors.index)
            if len(valid_idx) < min_vol_days:
                continue
            X = factors.loc[valid_idx].values
            y_vals = y.loc[valid_idx].values
            n = len(y_vals)
            # Add intercept column
            Xb = np.column_stack([np.ones(n), X])
            try:
                coef, _, _, _ = np.linalg.lstsq(Xb, y_vals, rcond=None)
                residuals = y_vals - Xb @ coef
                ivol_scores[ticker] = float(np.std(residuals, ddof=1))
            except Exception:
                continue

        if len(ivol_scores) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ivol_series = pd.Series(ivol_scores)
        q20 = ivol_series.quantile(0.20)
        q80 = ivol_series.quantile(0.80)

        long_tickers  = ivol_series[ivol_series <= q20].index.tolist()
        short_tickers = ivol_series[ivol_series >= q80].index.tolist()

        # --- Inverse-vol position sizing ---
        # Avoid zero IVOL (would produce inf weight)
        def inv_vol_weights(tickers_list: list) -> dict:
            ivols = {t: ivol_scores[t] for t in tickers_list if ivol_scores.get(t, 0) > 1e-8}
            if not ivols:
                return {}
            inv = {t: 1.0 / v for t, v in ivols.items()}
            total = sum(inv.values())
            return {t: w / total for t, w in inv.items()}

        long_weights  = inv_vol_weights(long_tickers)
        short_weights = inv_vol_weights(short_tickers)

        signals: List[Signal] = []
        today_str = str(prices_sub.index[-1].date()) if hasattr(prices_sub.index[-1], 'date') else str(prices_sub.index[-1])[:10]

        for ticker, weight in long_weights.items():
            entry = float(last_prices[ticker])
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', entry, regime_state=regime_state
            )
            pos = round(base_pct * weight * scale * 10, 4)
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=entry,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=min(pos, 0.05),
                confidence='MED',
                signal_params={
                    'ivol': round(ivol_scores[ticker], 6),
                    'quintile': 'Q1_low',
                    'signal_date': today_str,
                },
            ))

        for ticker, weight in short_weights.items():
            entry = float(last_prices[ticker])
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', entry, regime_state=regime_state
            )
            pos = round(base_pct * weight * scale * 10, 4)
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=entry,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=min(pos, 0.05),
                confidence='MED',
                signal_params={
                    'ivol': round(ivol_scores[ticker], 6),
                    'quintile': 'Q5_high',
                    'signal_date': today_str,
                },
            ))

        # Cap total signals
        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
