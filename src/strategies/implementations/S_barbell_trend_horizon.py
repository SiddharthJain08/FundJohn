from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['BarbellTrendHorizon']


class BarbellTrendHorizon(BaseStrategy):
    """Multi-horizon momentum barbell: LONG top-decile, SHORT bottom-decile with adaptive horizon weights."""

    id                = 'S_barbell_trend_horizon'
    name              = 'BarbellTrendHorizon'
    description       = 'Multi-horizon momentum barbell with adaptive Bayesian horizon weights: LONG top-decile, SHORT bottom-decile'
    tier              = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    LOOKBACKS  = [21, 125, 252]
    STOP_PCT   = 0.06
    TARGET_PCT = 0.15

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
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        price_df = prices[tickers].ffill().dropna(how='all')
        if len(price_df) < max(self.LOOKBACKS) + 5:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Adaptive horizon weights via recent Sharpe-ratio softmax (Bayesian-inspired)
        horizon_weights = self._horizon_weights(price_df)

        # Multi-horizon momentum composite (cross-sectionally z-scored per horizon)
        # Skip last 21 days for horizons > 21 (standard Jegadeesh-Titman reversal avoidance)
        composite = pd.Series(0.0, index=tickers)
        for lb, w in zip(self.LOOKBACKS, horizon_weights):
            skip = 21 if lb > 21 else 0
            if len(price_df) < lb + skip + 2:
                continue
            p_now  = price_df.iloc[-(1 + skip)]
            p_then = price_df.iloc[-(lb + 1)]
            mom    = p_now / p_then - 1
            mom    = mom.replace([np.inf, -np.inf], np.nan).dropna()
            std    = mom.std()
            if std > 0:
                composite[mom.index] += w * (mom - mom.mean()) / std

        composite = composite.reindex(tickers).dropna()
        if len(composite) < 10:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        n_side  = max(int(len(composite) * 0.10), 5)
        n_side  = min(n_side, self.MAX_SIGNALS // 2)
        scale   = self.position_scale(regime_state)
        pos_pct = round(scale * 0.02, 4)

        top_tickers = composite.nlargest(n_side).index.tolist()
        bot_tickers = composite.nsmallest(n_side).index.tolist()
        last_row    = price_df.iloc[-1]

        signals: List[Signal] = []

        for ticker in top_tickers:
            cp = last_row.get(ticker)
            if cp is None or pd.isna(cp):
                continue
            cp   = float(cp)
            st   = self.compute_stops_and_targets(price_df[ticker].dropna(), 'LONG', cp, regime_state=regime_state)
            conf = 'HIGH' if float(composite[ticker]) > 1.5 else 'MED'
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=cp, stop_loss=st['stop'],
                target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_pct, confidence=conf,
                signal_params={'mom_score': round(float(composite[ticker]), 4),
                               'hz_weights': [round(w, 3) for w in horizon_weights]},
            ))

        for ticker in bot_tickers:
            cp = last_row.get(ticker)
            if cp is None or pd.isna(cp):
                continue
            cp   = float(cp)
            st   = self.compute_stops_and_targets(price_df[ticker].dropna(), 'SHORT', cp, regime_state=regime_state)
            conf = 'HIGH' if float(composite[ticker]) < -1.5 else 'MED'
            signals.append(Signal(
                ticker=ticker, direction='SHORT',
                entry_price=cp, stop_loss=st['stop'],
                target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=pos_pct, confidence=conf,
                signal_params={'mom_score': round(float(composite[ticker]), 4),
                               'hz_weights': [round(w, 3) for w in horizon_weights]},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    def _horizon_weights(self, price_df: pd.DataFrame) -> list:
        """Sharpe-ratio softmax across [21, 125, 252] momentum horizons (Bayesian-inspired weighting)."""
        window = 63
        sharpes = []
        for lb in self.LOOKBACKS:
            if len(price_df) >= lb + window + 1:
                lb_returns = price_df.pct_change(lb).iloc[-window:].values.flatten()
                lb_returns = lb_returns[np.isfinite(lb_returns)]
                if len(lb_returns) > 10:
                    s = (lb_returns.mean() / (lb_returns.std() + 1e-8)) * (252 / lb) ** 0.5
                    sharpes.append(max(float(s), 0.0))
                else:
                    sharpes.append(0.0)
            else:
                sharpes.append(0.0)

        total = sum(sharpes)
        if total < 1e-6:
            return [1.0 / 3, 1.0 / 3, 1.0 / 3]
        return [s / total for s in sharpes]
