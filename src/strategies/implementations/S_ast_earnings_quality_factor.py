"""
S_ast_earnings_quality_factor
Source: https://quantpedia.com/strategies/earnings-quality-factor/

Composite earnings-quality score (accruals, ROE, CF/A, D/A) predicts cross-sectional
returns: long high-quality firms, short low-quality firms, rebalanced annually.
Universe: top 3000 non-financial US stocks by market cap. Annual rebalance end of June.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['EarningsQualityFactor']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_ast_earnings_quality_factor'


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _percentile_rank(series: pd.Series) -> pd.Series:
    """Rank values to [0, 100] percentile (higher rank = higher value)."""
    return series.rank(pct=True) * 100.0


class EarningsQualityFactor(BaseStrategy):
    """Long high earnings-quality firms; short low quality, annually rebalanced end of June."""

    id                = STRATEGY_ID
    name              = 'EarningsQualityFactor'
    description       = ('Composite earnings-quality score (accruals, ROE, CF/A, D/A) predicts '
                         'cross-sectional returns: long high-quality firms, short low-quality '
                         'firms, rebalanced annually.')
    tier              = 2
    signal_frequency  = 'annual'
    min_lookback      = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

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
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Annual rebalance: fire only in July (after end-of-June rebalance)
        last_date = pd.Timestamp(prices.index[-1])
        if last_date.month != 7 or last_date.day > 5:
            print('[debug] signals=0 (not rebalance window)', file=sys.stderr)
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            print('[debug] signals=0 (no financials)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # ── Build quality scores per ticker ─────────────────────────────────
        records = []
        for ticker in universe:
            fin = financials.get(ticker)
            if not fin:
                continue

            roe          = _safe_float(fin.get('roe'))
            total_assets = _safe_float(fin.get('total_assets'))
            total_liab   = _safe_float(fin.get('total_liabilities'))
            net_income   = _safe_float(fin.get('net_income'))
            oper_income  = _safe_float(fin.get('operating_income'))
            mcap         = _safe_float(fin.get('market_cap'))

            if total_assets is None or total_assets <= 0:
                continue
            if mcap is None or mcap <= 0:
                continue
            if ticker not in prices.columns:
                continue

            price_series = prices[ticker].dropna()
            if len(price_series) < 5:
                continue
            entry_price = float(price_series.iloc[-1])
            if entry_price < 1.0:
                continue

            # Accruals proxy: (NI - OperIncome) / TotalAssets
            # Lower accruals (NI < OCF proxy) → better quality → higher score (inverted)
            accruals = None
            if net_income is not None and oper_income is not None:
                accruals = (net_income - oper_income) / total_assets

            # CF/A proxy: OperatingIncome / TotalAssets (higher = better)
            cfa = oper_income / total_assets if oper_income is not None else None

            # D/A: TotalLiabilities / TotalAssets (lower = better quality)
            da = total_liab / total_assets if total_liab is not None else None

            records.append({
                'ticker':   ticker,
                'roe':      roe,
                'accruals': accruals,
                'cfa':      cfa,
                'da':       da,
                'mcap':     mcap,
                'entry_price': entry_price,
            })

        if len(records) < 20:
            print(f'[debug] signals=0 (only {len(records)} valid tickers)', file=sys.stderr)
            return []

        df = pd.DataFrame(records).set_index('ticker')

        # ── Large-cap / small-cap split (90/10 by total market cap) ─────────
        total_mcap = df['mcap'].sum()
        df_sorted = df.sort_values('mcap', ascending=False).copy()
        df_sorted['cumcap'] = df_sorted['mcap'].cumsum()
        df_sorted['large_cap'] = df_sorted['cumcap'] <= (0.90 * total_mcap)
        df_large = df_sorted[df_sorted['large_cap']]
        df_small = df_sorted[~df_sorted['large_cap']]

        signals: List[Signal] = []
        for cohort_df, cohort_name in [(df_large, 'large'), (df_small, 'small')]:
            if len(cohort_df) < 10:
                continue
            scored = self._score_cohort(cohort_df)
            if scored is None or scored.empty:
                continue

            p70 = scored['composite'].quantile(0.70)
            p30 = scored['composite'].quantile(0.30)

            long_set  = scored[scored['composite'] >= p70].copy()
            short_set = scored[scored['composite'] <= p30].copy()

            signals.extend(self._make_signals(
                long_set, prices, regime_state, scale, 'LONG', cohort_name))
            signals.extend(self._make_signals(
                short_set, prices, regime_state, scale, 'SHORT', cohort_name))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _score_cohort(self, df: pd.DataFrame) -> pd.DataFrame | None:
        scored = df.copy()
        composite = pd.Series(0.0, index=scored.index)
        n_metrics = 0

        # ROE: higher = better → higher percentile = high quality
        if scored['roe'].notna().sum() >= 5:
            composite += _percentile_rank(scored['roe'].fillna(scored['roe'].median()))
            n_metrics += 1

        # Accruals: lower accruals = better quality → invert (negate before ranking)
        if scored['accruals'].notna().sum() >= 5:
            composite += _percentile_rank(-scored['accruals'].fillna(
                scored['accruals'].median()))
            n_metrics += 1

        # CF/A: higher = better
        if scored['cfa'].notna().sum() >= 5:
            composite += _percentile_rank(scored['cfa'].fillna(scored['cfa'].median()))
            n_metrics += 1

        # D/A: lower = better quality → invert
        if scored['da'].notna().sum() >= 5:
            composite += _percentile_rank(-scored['da'].fillna(scored['da'].median()))
            n_metrics += 1

        if n_metrics < 2:
            return None

        scored['composite'] = composite
        return scored

    def _make_signals(
        self,
        pool: pd.DataFrame,
        prices: pd.DataFrame,
        regime_state: str,
        scale: float,
        direction: str,
        cohort: str,
    ) -> List[Signal]:
        if pool.empty:
            return []
        total_mcap = pool['mcap'].sum()
        signals = []
        for ticker, row in pool.iterrows():
            if ticker not in prices.columns:
                continue
            price_series = prices[ticker].dropna()
            if len(price_series) < 5:
                continue
            entry_price = float(price_series.iloc[-1])
            stops = self.compute_stops_and_targets(
                price_series, direction, entry_price, regime_state=regime_state)
            weight = (row['mcap'] / total_mcap) * 0.50 * scale
            pos_size = float(min(weight, 0.08))
            if pos_size < 0.001:
                continue
            composite = float(row['composite'])
            confidence = 'HIGH' if composite >= 350 or composite <= 50 else 'MED'
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(pos_size, 4),
                confidence=confidence,
                signal_params={
                    'composite_score': round(composite, 1),
                    'cohort': cohort,
                    'strategy': 'earnings_quality_factor',
                },
            ))
        return signals
