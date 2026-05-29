from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

from src.strategies.universe_default import no_otc as universe_filter

__all__ = ['EarningsDistractionPEAD']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_earnings_distraction_pead'

# Lookback windows
_DISTRACTION_LOOKBACK_DAYS = 252   # trailing year for median distraction threshold
_SUE_QUARTERS              = 8     # quarters of EPS surprise history for std
_ANNOUNCE_WINDOW_DAYS      = 5     # trading days back to scan for recent announcements
_TOP_PCTL                  = 0.80  # top quintile threshold
_BOT_PCTL                  = 0.20  # bottom quintile threshold
_BASE_SIZE                 = 0.012 # 1.2% per name; ~20 longs+20 shorts ≈ 48% gross


class EarningsDistractionPEAD(BaseStrategy):
    """PEAD amplified on high-distraction days: LONG/SHORT top/bottom-quintile SUE × distraction."""

    id          = STRATEGY_ID
    name        = 'EarningsDistractionPEAD'
    description = (
        'On high-distraction earnings days (many concurrent announcements) investors '
        'underreact to individual SUE, creating stronger PEAD. '
        'LONG top-quintile, SHORT bottom-quintile surprise × distraction. '
        'Hirshleifer, Lim & Teoh (2009).'
    )
    tier         = 2
    min_lookback = 504

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

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

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        aux_data = aux_data or {}
        earnings = aux_data.get('earnings')

        if earnings is None or earnings.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        tickers_set = set(universe) & set(prices.columns)

        # ── 1. Normalise earnings columns ──────────────────────────────────
        df = earnings.copy()
        df.columns = [c.lower().strip() for c in df.columns]

        ticker_col = next((c for c in df.columns if c in ('ticker', 'symbol')), None)
        date_col   = next((c for c in df.columns if 'date' in c or 'period' in c), None)
        actual_col = next((c for c in df.columns if 'actual' in c), None)
        est_col    = next((c for c in df.columns if 'estimate' in c or 'consensus' in c or 'expected' in c), None)

        if any(c is None for c in (ticker_col, date_col, actual_col, est_col)):
            print('[debug] signals=0 (missing earnings columns)', file=sys.stderr)
            return []

        df = df[[ticker_col, date_col, actual_col, est_col]].copy()
        df.columns = ['ticker', 'report_date', 'actual', 'estimate']
        df = df.dropna(subset=['ticker', 'report_date', 'actual', 'estimate'])
        df['ticker'] = df['ticker'].astype(str)
        df['report_date'] = pd.to_datetime(df['report_date'], errors='coerce')
        df = df.dropna(subset=['report_date'])
        df['actual']   = pd.to_numeric(df['actual'],   errors='coerce')
        df['estimate'] = pd.to_numeric(df['estimate'], errors='coerce')
        df = df.dropna(subset=['actual', 'estimate'])
        df['surprise'] = df['actual'] - df['estimate']
        df = df.sort_values('report_date')

        if df.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        today = prices.index[-1] if hasattr(prices.index[-1], 'date') else pd.Timestamp(str(prices.index[-1]))

        # ── 2. Distraction count per calendar date ──────────────────────────
        # Count distinct tickers announcing per date (universe-filtered for liquidity)
        ann_dates = df[df['ticker'].isin(tickers_set)].copy()
        if ann_dates.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ann_dates['date_only'] = ann_dates['report_date'].dt.normalize()
        daily_count = ann_dates.groupby('date_only')['ticker'].nunique().rename('distraction_count')

        # Trailing year: median distraction to classify high-distraction dates
        cutoff_past = today - pd.Timedelta(days=_DISTRACTION_LOOKBACK_DAYS)
        trailing_counts = daily_count[(daily_count.index >= cutoff_past) & (daily_count.index <= today)]
        if trailing_counts.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        median_distraction = float(trailing_counts.median())
        high_distraction_dates = set(trailing_counts[trailing_counts > median_distraction].index)

        # ── 3. Recent announcements within _ANNOUNCE_WINDOW_DAYS ───────────
        # Use calendar days heuristic (5 trading days ≈ 7 calendar days)
        window_start = today - pd.Timedelta(days=_ANNOUNCE_WINDOW_DAYS * 2)
        recent = ann_dates[
            (ann_dates['date_only'] >= window_start) &
            (ann_dates['date_only'] <= today) &
            (ann_dates['date_only'].isin(high_distraction_dates))
        ].copy()

        if recent.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── 4. SUE: standardised unexpected earnings (8-quarter window) ────
        def _sue(grp: pd.DataFrame) -> float:
            """Latest surprise / trailing std(surprise, 8q)."""
            grp = grp.sort_values('report_date')
            surprises = grp['surprise'].values
            if len(surprises) < 2:
                return float('nan')
            # Use up to last _SUE_QUARTERS quarters
            tail = surprises[-_SUE_QUARTERS:]
            std = float(np.std(tail, ddof=1))
            if std == 0 or np.isnan(std):
                return float('nan')
            return float(tail[-1]) / std

        # Compute SUE for all tickers with any earnings history
        sue_map = {}
        for tkr, grp in df[df['ticker'].isin(tickers_set)].groupby('ticker'):
            val = _sue(grp)
            if not np.isnan(val):
                sue_map[tkr] = val

        # Only keep tickers that announced recently in high-distraction window
        recent_tickers = set(recent['ticker'].unique())
        scored = {t: sue_map[t] for t in recent_tickers if t in sue_map}

        if len(scored) < 10:
            print(f'[debug] signals=0 (only {len(scored)} scored tickers)', file=sys.stderr)
            return []

        score_series = pd.Series(scored)
        top_cut = score_series.quantile(_TOP_PCTL)
        bot_cut = score_series.quantile(_BOT_PCTL)

        longs  = score_series[score_series >= top_cut].index.tolist()
        shorts = score_series[score_series <= bot_cut].index.tolist()

        # ── 5. Build signals ───────────────────────────────────────────────
        close = prices[list(tickers_set)].ffill()
        latest = close.iloc[-1]
        signals: List[Signal] = []

        for ticker in longs[: self.MAX_SIGNALS // 2]:
            if ticker not in latest.index:
                continue
            cp = float(latest[ticker])
            if np.isnan(cp) or cp <= 0:
                continue
            st = self.compute_stops_and_targets(
                close[ticker].dropna(), 'LONG', cp, regime_state=regime_state
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = float(cp),
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = float(round(_BASE_SIZE * scale, 4)),
                confidence        = 'HIGH',
                signal_params     = {
                    'sue':              float(round(score_series[ticker], 4)),
                    'distraction_date': str(recent[recent['ticker'] == ticker]['date_only'].iloc[-1].date()),
                },
            ))

        for ticker in shorts[: self.MAX_SIGNALS // 2]:
            if ticker not in latest.index:
                continue
            cp = float(latest[ticker])
            if np.isnan(cp) or cp <= 0:
                continue
            st = self.compute_stops_and_targets(
                close[ticker].dropna(), 'SHORT', cp, regime_state=regime_state
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = float(cp),
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = float(round(_BASE_SIZE * scale, 4)),
                confidence        = 'HIGH',
                signal_params     = {
                    'sue':              float(round(score_series[ticker], 4)),
                    'distraction_date': str(recent[recent['ticker'] == ticker]['date_only'].iloc[-1].date()),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[: self.MAX_SIGNALS]
