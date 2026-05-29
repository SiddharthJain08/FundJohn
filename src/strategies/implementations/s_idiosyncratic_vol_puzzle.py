from __future__ import annotations

import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import no_otc as universe_filter

__all__ = ['IdiosyncraticVolPuzzle']

INSTRUMENT_CLASS = 'equity'

STRATEGY_ID = 'S_idiosyncratic_vol_puzzle'


class IdiosyncraticVolPuzzle(BaseStrategy):
    """Long low-IVOL / short high-IVOL cross-sectional portfolio (Ang et al. 2006).

    IVOL = std-dev of daily residuals vs equal-weight market proxy over 22 days.
    Monthly quintile sort: LONG bottom-20%, SHORT top-20%.
    """

    id               = STRATEGY_ID
    name             = 'IdiosyncraticVolPuzzle'
    description      = (
        'Long low-IVOL / short high-IVOL cross-sectional portfolio; '
        'IVOL = std of market-residual returns over 22 days (Ang et al. 2006)'
    )
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    instrument_class = INSTRUMENT_CLASS

    IVOL_WINDOW    = 22        # ~1 calendar month of trading days
    QUINTILE_FRAC  = 0.20      # top/bottom 20%
    MIN_UNIVERSE   = 50        # minimum tickers for a valid cross-section
    BASE_SIZE_LONG  = 0.012
    BASE_SIZE_SHORT = 0.010
    MAX_SIGNALS     = 40       # cap per full cycle (20 long + 20 short)

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

        scale = self.position_scale(regime_state)

        # Intersect universe with available price columns
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < self.MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers]
        if len(prices_sub) < self.IVOL_WINDOW + 5:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Daily returns over the IVOL estimation window
        window_ret = prices_sub.pct_change().iloc[-(self.IVOL_WINDOW + 1):]

        # Market proxy: equal-weight cross-sectional mean return
        mkt_ret = window_ret.mean(axis=1)

        # Residuals: simplified beta=1 market-adjusted returns
        resid = window_ret.sub(mkt_ret, axis=0)

        # IVOL = annualised std of residuals in the window
        ivol = resid.std(ddof=1).dropna()
        ivol = ivol[ivol > 0]

        if len(ivol) < self.MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        q20 = float(ivol.quantile(self.QUINTILE_FRAC))
        q80 = float(ivol.quantile(1.0 - self.QUINTILE_FRAC))
        rank_pct = ivol.rank(pct=True)

        low_ivol  = ivol[ivol <= q20].sort_values(ascending=True)
        high_ivol = ivol[ivol >= q80].sort_values(ascending=False)

        current_prices = prices_sub.iloc[-1]
        size_long  = round(self.BASE_SIZE_LONG  * scale, 6)
        size_short = round(self.BASE_SIZE_SHORT * scale, 6)

        signals: List[Signal] = []

        # LONG: lowest-IVOL quintile
        for ticker in low_ivol.index:
            if len(signals) >= self.MAX_SIGNALS // 2:
                break
            raw = current_prices.get(ticker)
            if raw is None or (raw != raw) or raw <= 0:
                continue
            price = float(raw)
            rp = float(rank_pct[ticker])
            conf = 'HIGH' if rp <= 0.10 else ('MED' if rp <= 0.20 else 'LOW')
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', price,
                regime_state=regime_state,
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_long,
                confidence=conf,
                signal_params={
                    'ivol_22d': round(float(ivol[ticker]), 6),
                    'rank_pct': round(rp, 4),
                    'signal_leg': 'low_ivol_long',
                },
            ))

        # SHORT: highest-IVOL quintile
        for ticker in high_ivol.index:
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current_prices.get(ticker)
            if raw is None or (raw != raw) or raw <= 0:
                continue
            price = float(raw)
            rp = float(rank_pct[ticker])
            conf = 'HIGH' if rp >= 0.90 else ('MED' if rp >= 0.80 else 'LOW')
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', price,
                regime_state=regime_state,
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_short,
                confidence=conf,
                signal_params={
                    'ivol_22d': round(float(ivol[ticker]), 6),
                    'rank_pct': round(rp, 4),
                    'signal_leg': 'high_ivol_short',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
