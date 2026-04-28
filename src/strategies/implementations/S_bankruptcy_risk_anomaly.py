from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['BankruptcyRiskAnomaly']


class BankruptcyRiskAnomaly(BaseStrategy):
    """LONG low-distress (high Z-score), SHORT high-distress (low Z-score) — Dichev (1998) bankruptcy anomaly."""

    id               = 'S_bankruptcy_risk_anomaly'
    name             = 'BankruptcyRiskAnomaly'
    description      = 'LONG low-distress / SHORT high-distress quintiles via Altman Z-score — Dichev (1998)'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 21

    # All-weather: distress spread is not regime-dependent
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    QUINTILE_FRAC = 0.20   # top/bottom 20%
    BASE_SIZE     = 0.012  # 1.2% per position before regime scaling

    @staticmethod
    def _altman_z(fin: dict) -> float | None:
        """Compute Altman Z-score. Returns None if required fields are missing."""
        total_assets = fin.get('totalAssets') or 0
        if total_assets <= 0:
            return None

        working_capital    = fin.get('workingCapital') or 0
        retained_earnings  = fin.get('retainedEarnings') or 0
        ebit               = fin.get('operatingIncome') or 0
        market_cap         = fin.get('marketCap') or 0
        total_liabilities  = fin.get('totalLiabilities') or fin.get('totalDebt') or 0
        revenue            = fin.get('revenue') or 0

        x1 = working_capital / total_assets
        x2 = retained_earnings / total_assets
        x3 = ebit / total_assets
        x4 = market_cap / total_liabilities if total_liabilities > 0 else 0.0
        x5 = revenue / total_assets

        return 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5

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
        scale = self.position_scale(regime_state)

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print(f'[debug] signals=0 (no financials data)', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        # Compute Z-scores
        scores: dict[str, float] = {}
        for ticker in tickers:
            fin = financials.get(ticker)
            if not fin:
                continue
            z = self._altman_z(fin)
            if z is not None:
                scores[ticker] = z

        if len(scores) < 20:
            print(f'[debug] signals=0 (too few Z-scores: {len(scores)})', file=sys.stderr)
            return []

        sorted_tickers = sorted(scores, key=lambda t: scores[t])
        n = len(sorted_tickers)
        n_quintile = max(1, int(n * self.QUINTILE_FRAC))
        # highest distress (lowest Z) → SHORT
        short_tickers = sorted_tickers[:n_quintile]
        # lowest distress (highest Z) → LONG
        long_tickers  = sorted_tickers[-n_quintile:]

        signals: List[Signal] = []
        half_cap = self.MAX_SIGNALS // 2

        for ticker in long_tickers[:half_cap]:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            px = float(series.iloc[-1])
            if px <= 0:
                continue
            z = scores[ticker]
            conf = 'HIGH' if z > 3.0 else ('MED' if z > 1.81 else 'LOW')
            st = self.compute_stops_and_targets(series, 'LONG', px, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=px,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(self.BASE_SIZE * scale),
                confidence=conf,
                signal_params={'altman_z': round(z, 4), 'distress_quintile': 'low'},
            ))

        for ticker in short_tickers[:half_cap]:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            px = float(series.iloc[-1])
            if px <= 0:
                continue
            z = scores[ticker]
            conf = 'HIGH' if z < 1.23 else ('MED' if z < 1.81 else 'LOW')
            st = self.compute_stops_and_targets(series, 'SHORT', px, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=px,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(self.BASE_SIZE * scale),
                confidence=conf,
                signal_params={'altman_z': round(z, 4), 'distress_quintile': 'high'},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
