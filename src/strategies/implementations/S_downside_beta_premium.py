"""
Downside Beta Premium — asymmetric market co-movement priced in the cross-section.

Source: Ang, Chen & Xing (2006, Review of Financial Studies), "Downside Risk."

Hypothesis: investors demand compensation for stocks that co-move MORE with
the market when it falls than when it rises. Over a trailing 252d window,
estimate beta-minus (regression beta on SPY-down days) and beta-plus (on
SPY-up days); the asymmetry spread (beta_minus - beta_plus) is the priced
downside-risk exposure. LONG the top quintile of the spread (paid to bear
downside risk), SHORT the bottom quintile (expensive crash-hedge names),
rebalanced monthly.

Data: close panel only (needs a SPY column in the engine panel — returns []
if absent; no extra parquet fields).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import liquid_pool

__all__ = ['DownsideBetaPremium']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_downside_beta_premium'


class DownsideBetaPremium(BaseStrategy):
    """LONG high (beta_minus - beta_plus) names, SHORT low-spread names."""

    id                = STRATEGY_ID
    name              = 'Downside Beta Premium'
    description       = ('Ang-Chen-Xing 2006 downside risk: beta on SPY-down days minus beta on SPY-up days '
                         'over 252d; LONG top quintile of the spread, SHORT bottom quintile; monthly, <=12/leg.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 300         # 252d conditional-beta window + buffer
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 24

    BETA_WINDOW     = 252
    MIN_SIDE_OBS    = 40            # min valid pairwise obs per (name, up/down) side
    LEG_COUNT       = 12
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012
    MARKET_TICKER   = 'SPY'

    def default_parameters(self) -> dict:
        return {
            'beta_window': self.BETA_WINDOW,
            'leg_count':   self.LEG_COUNT,
            'pool_size':   500,
        }

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of a month (monthly cadence gate)."""
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        return d1.month != d0.month

    @staticmethod
    def _cond_beta(r: pd.DataFrame, s: pd.Series, min_obs: int) -> pd.Series:
        """Per-name beta of r columns on s over pairwise-valid rows (vectorized;
        names with < min_obs valid observations return NaN)."""
        if r.empty or s.empty:
            return pd.Series(dtype='float64')
        valid = r.notna()
        n = valid.sum()
        sm = pd.DataFrame(
            np.broadcast_to(s.to_numpy(dtype='float64')[:, None], r.shape),
            index=r.index, columns=r.columns,
        ).where(valid)
        r_mean = r.mean()
        s_mean = sm.mean()
        cov = (r * sm).mean() - r_mean * s_mean
        var = (sm * sm).mean() - s_mean * s_mean
        beta = cov / var.where(var > 1e-12)
        return beta.where(n >= min_obs)

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not self._month_boundary(prices):
            print('[debug] signals=0', file=sys.stderr)
            return []

        mkt = self.MARKET_TICKER
        if mkt not in prices.columns:          # SPY guard — panel may lack it
            print('[debug] signals=0', file=sys.stderr)
            return []

        bwin = int(self.parameters.get('beta_window', self.BETA_WINDOW))
        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns and t != mkt]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Union-calendar safety: equity sessions only.
        eq = prices[pool].dropna(how='all')
        if len(eq) < bwin + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        c = eq.iloc[-(bwin + 1):].astype('float64')
        spy = prices[mkt].reindex(c.index).astype('float64')

        rets = c.pct_change().iloc[1:]
        sret = spy.pct_change().iloc[1:]
        if sret.notna().sum() < bwin * 0.8:
            print('[debug] signals=0', file=sys.stderr)
            return []

        dn = sret < 0
        up = sret > 0
        if int(dn.sum()) < self.MIN_SIDE_OBS or int(up.sum()) < self.MIN_SIDE_OBS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        beta_dn = self._cond_beta(rets[dn.values], sret[dn.values], self.MIN_SIDE_OBS)
        beta_up = self._cond_beta(rets[up.values], sret[up.values], self.MIN_SIDE_OBS)
        spread  = (beta_dn - beta_up).dropna()
        spread  = spread[np.isfinite(spread)]
        if len(spread) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = spread.rank(pct=True)
        n_leg = min(int(self.parameters.get('leg_count', self.LEG_COUNT)),
                    max(1, int(len(spread) * 0.20)))
        longs  = rank_pct[rank_pct >= 0.80].sort_values(ascending=False).head(n_leg)
        shorts = rank_pct[rank_pct <= 0.20].sort_values(ascending=True).head(n_leg)

        scale = self.position_scale(regime_state)
        current = c.iloc[-1]

        def _conf(ext: float) -> str:
            if ext >= 0.95:
                return 'HIGH'
            if ext >= 0.88:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((longs, 'LONG', self.BASE_SIZE_LONG),
                                     (shorts, 'SHORT', self.BASE_SIZE_SHORT)):
            for ticker, rp in leg.items():
                if len(signals) >= self.MAX_SIGNALS:
                    break
                raw = current.get(ticker)
                if raw is None or not np.isfinite(raw) or raw <= 0:
                    continue
                price = float(raw)
                series = prices[ticker].dropna()
                stops = self.compute_stops_and_targets(series, direction, price,
                                                       regime_state=regime_state)
                extremity = float(rp) if direction == 'LONG' else 1.0 - float(rp)
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stops['stop'],
                    target_1=stops['t1'],
                    target_2=stops['t2'],
                    target_3=stops['t3'],
                    position_size_pct=round(base * scale, 6),
                    confidence=_conf(extremity),
                    signal_params={
                        'beta_down':  round(float(beta_dn[ticker]), 4),
                        'beta_up':    round(float(beta_up[ticker]), 4),
                        'beta_asym':  round(float(spread[ticker]), 4),
                        'rank_pct':   round(float(rp), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
