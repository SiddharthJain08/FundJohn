from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['PriceEarningsMomentumDrift']


class PriceEarningsMomentumDrift(BaseStrategy):
    """Composite price+earnings momentum: ranks 6m price momentum + SUE; longs top decile, shorts bottom."""

    id          = 'S_price_earnings_momentum_drift'
    name        = 'PriceEarningsMomentumDrift'
    description = (
        'Stocks with high past price returns and positive earnings surprises '
        'continue to drift upward due to gradual market underreaction to both signals.'
    )
    tier        = 2
    min_lookback = 504

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    # Monthly rebalance: ~21 trading days
    _PRICE_MOM_LOOKBACK = 126   # ~6 months
    _SKIP_LAST = 21             # skip most-recent month (t-1m skip standard)
    _TOP_PCT  = 0.10
    _BOT_PCT  = 0.10
    _BASE_SIZE = 0.015          # 1.5% per name; ~15 longs + 15 shorts at top decile

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

        scale = self.position_scale(regime_state)
        aux_data = aux_data or {}

        # ── 1. Price momentum: cumulative return t-6m to t-1m ──────────────
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        close = prices[tickers].ffill()
        if len(close) < self._PRICE_MOM_LOOKBACK + self._SKIP_LAST + 5:
            print('[debug] signals=0', file=sys.stderr)
            return []

        end_idx   = len(close) - self._SKIP_LAST - 1   # t-1m
        start_idx = end_idx - self._PRICE_MOM_LOOKBACK

        if start_idx < 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        p_start = close.iloc[start_idx]
        p_end   = close.iloc[end_idx]
        price_mom = (p_end / p_start.replace(0, np.nan) - 1.0).dropna()

        # ── 2. SUE: standardised unexpected earnings ────────────────────────
        earnings = aux_data.get('earnings')
        sue_series = pd.Series(dtype=float)

        if earnings is not None and not earnings.empty:
            try:
                sue_series = self._compute_sue(earnings, tickers)
            except Exception:
                sue_series = pd.Series(dtype=float)

        # ── 3. Composite rank ───────────────────────────────────────────────
        common = price_mom.index
        if len(sue_series) > 0:
            common = common.intersection(sue_series.index)

        if len(common) < 20:
            # Fall back to price-only momentum
            score = price_mom.rank(pct=True)
        else:
            pm_rank  = price_mom.loc[common].rank(pct=True)
            sue_rank = sue_series.loc[common].rank(pct=True)
            score    = pm_rank + sue_rank

        n = len(score)
        top_cut = score.quantile(1 - self._TOP_PCT)
        bot_cut = score.quantile(self._BOT_PCT)

        longs  = score[score >= top_cut].index.tolist()
        shorts = score[score <= bot_cut].index.tolist()

        # ── 4. Build signals ────────────────────────────────────────────────
        signals: List[Signal] = []
        latest_prices = close.iloc[-1]

        for ticker in longs[:self.MAX_SIGNALS // 2]:
            cp = float(latest_prices.get(ticker, np.nan))
            if np.isnan(cp) or cp <= 0:
                continue
            st = self.compute_stops_and_targets(
                close[ticker].dropna(), 'LONG', cp, regime_state=regime_state
            )
            confidence = 'HIGH' if (len(sue_series) > 0 and ticker in sue_series.index) else 'MED'
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = float(cp),
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = float(round(self._BASE_SIZE * scale, 4)),
                confidence        = confidence,
                signal_params     = {
                    'composite_score': float(score.get(ticker, 0)),
                    'price_mom_6m':    float(price_mom.get(ticker, 0)),
                },
            ))

        for ticker in shorts[:self.MAX_SIGNALS // 2]:
            cp = float(latest_prices.get(ticker, np.nan))
            if np.isnan(cp) or cp <= 0:
                continue
            st = self.compute_stops_and_targets(
                close[ticker].dropna(), 'SHORT', cp, regime_state=regime_state
            )
            confidence = 'HIGH' if (len(sue_series) > 0 and ticker in sue_series.index) else 'MED'
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = float(cp),
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = float(round(self._BASE_SIZE * scale, 4)),
                confidence        = confidence,
                signal_params     = {
                    'composite_score': float(score.get(ticker, 0)),
                    'price_mom_6m':    float(price_mom.get(ticker, 0)),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    # ── helpers ────────────────────────────────────────────────────────────
    def _compute_sue(self, earnings: pd.DataFrame, tickers: list) -> pd.Series:
        """
        Compute Standardised Unexpected Earnings (SUE) per ticker.
        Expects earnings with columns: ticker, eps_actual, eps_estimate, report_date.
        Uses the most-recent report per ticker; std over trailing 4 quarters.
        Returns pd.Series indexed by ticker.
        """
        df = earnings.copy()
        # Normalise column names defensively
        df.columns = [c.lower().strip() for c in df.columns]

        ticker_col  = next((c for c in df.columns if 'ticker' in c or 'symbol' in c), None)
        actual_col  = next((c for c in df.columns if 'actual' in c), None)
        est_col     = next((c for c in df.columns if 'estimate' in c or 'consensus' in c or 'expected' in c), None)

        if ticker_col is None or actual_col is None or est_col is None:
            return pd.Series(dtype=float)

        df = df[[ticker_col, actual_col, est_col]].dropna()
        df.columns = ['ticker', 'actual', 'estimate']
        df['surprise'] = df['actual'] - df['estimate']

        sue_vals = {}
        for tkr, grp in df.groupby('ticker'):
            if tkr not in tickers:
                continue
            surprises = grp['surprise'].values
            if len(surprises) < 2:
                continue
            std = float(np.std(surprises, ddof=1))
            if std == 0:
                continue
            sue_vals[tkr] = float(surprises[-1]) / std

        return pd.Series(sue_vals)
