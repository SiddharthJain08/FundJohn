"""
S_ast_earnings_announcements_combined_with_stock_repurchases
Source: https://quantpedia.com/strategies/earnings-announcements-combined-with-stock-repurchases/

Long stocks that (a) have earnings ~10 calendar days ahead AND (b) announced a
buyback (≥5% of shares) in the 30-to-15 days window before that earnings date.

Clean-room port from QuantConnect blueprint. Buyback feed approximated via
insider P-Purchase transactions (no corporate-buyback announcement feed in
standard pipeline). Falls back to earnings-only at lower confidence.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from pathlib import Path
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import no_adr as universe_filter

__all__ = ['AstEarningsAnnouncementsCombinedWithStockRepurchases']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_ast_earnings_announcements_combined_with_stock_repurchases'

_ROOT        = Path(__file__).resolve().parent.parent.parent.parent
_MAX_POS     = 40
_ENTRY_DAYS  = 10   # calendar days before earnings
_BB_START    = 20   # t-20 ≈ earnings-30d (buyback window open)
_BB_END      = 5    # t-5  ≈ earnings-15d (buyback window close)
_WEIGHT      = 1.0 / _MAX_POS

_EARN_DF:    pd.DataFrame | None = None
_INSIDER_DF: pd.DataFrame | None = None


def _load_earn() -> pd.DataFrame:
    global _EARN_DF
    if _EARN_DF is not None:
        return _EARN_DF
    p = _ROOT / 'data' / 'master' / 'earnings.parquet'
    if not p.exists():
        _EARN_DF = pd.DataFrame(columns=['ticker', 'date'])
        return _EARN_DF
    df = pd.read_parquet(p, columns=['ticker', 'date'])
    df['date'] = pd.to_datetime(df['date'], utc=False, errors='coerce')
    _EARN_DF = df.dropna(subset=['date']).copy()
    return _EARN_DF


def _load_insider() -> pd.DataFrame:
    global _INSIDER_DF
    if _INSIDER_DF is not None:
        return _INSIDER_DF
    p = _ROOT / 'data' / 'master' / 'insider.parquet'
    if not p.exists():
        _INSIDER_DF = pd.DataFrame(columns=['ticker', 'transaction_date'])
        return _INSIDER_DF
    df = pd.read_parquet(
        p, columns=['ticker', 'transaction_date', 'transaction_type'])
    df['transaction_date'] = pd.to_datetime(
        df['transaction_date'], errors='coerce')
    # P-Purchase = insider open-market buy; best available proxy for buyback intent
    _INSIDER_DF = (df[df['transaction_type'] == 'P-Purchase']
                   .dropna(subset=['transaction_date'])
                   .copy())
    return _INSIDER_DF


class AstEarningsAnnouncementsCombinedWithStockRepurchases(BaseStrategy):
    """Long upcoming-earnings stocks with prior buyback/insider-buy signal.

    Source: https://quantpedia.com/strategies/earnings-announcements-combined-with-stock-repurchases/
    """

    id                = STRATEGY_ID
    name              = 'AstEarningsAnnouncementsCombinedWithStockRepurchases'
    description       = ('Long stocks with earnings in ~10 days whose insiders '
                         'bought shares in the 30-to-15 day pre-announcement window.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 20
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

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

        today     = pd.Timestamp(prices.index[-1]).normalize()
        univ_set  = set(universe) & set(prices.columns)
        earn_df   = _load_earn()
        insider_df = _load_insider()

        if earn_df.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # target earnings window: today+7 to today+14 calendar days (~10d midpoint)
        target_lo = today + pd.Timedelta(days=7)
        target_hi = today + pd.Timedelta(days=14)

        upcoming = earn_df[
            (earn_df['date'] >= target_lo) &
            (earn_df['date'] <= target_hi) &
            (earn_df['ticker'].isin(univ_set))
        ].copy()

        if upcoming.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # buyback window: [today-20, today-5] calendar days
        bb_lo = today - pd.Timedelta(days=_BB_START)
        bb_hi = today - pd.Timedelta(days=_BB_END)

        # tickers with insider purchases in the buyback window (proxy)
        has_buyback: set[str] = set()
        if not insider_df.empty:
            bb_mask = (
                (insider_df['transaction_date'] >= bb_lo) &
                (insider_df['transaction_date'] <= bb_hi) &
                (insider_df['ticker'].isin(univ_set))
            )
            has_buyback = set(insider_df.loc[bb_mask, 'ticker'])

        # market-cap filter: drop bottom 25% by most-recent close price as proxy
        # (real market-cap data via financials is optional — use price as fallback)
        fin = (aux_data or {}).get('financials', {})
        mcaps: dict[str, float] = {}
        for t in upcoming['ticker']:
            f  = fin.get(t, {})
            mc = f.get('market_cap') or f.get('marketCap')
            if mc:
                mcaps[t] = float(mc)
            elif t in prices.columns:
                ts = prices[t].dropna()
                mcaps[t] = float(ts.iloc[-1]) if not ts.empty else 0.0

        if mcaps:
            cap_thresh = np.quantile(list(mcaps.values()), 0.25)
            qualifying_tickers = [t for t in upcoming['ticker']
                                  if mcaps.get(t, 0) >= cap_thresh]
        else:
            qualifying_tickers = list(upcoming['ticker'])

        # prefer tickers with buyback signal; fall back to all qualifying
        bb_tickers   = [t for t in qualifying_tickers if t in has_buyback]
        all_tickers  = bb_tickers if bb_tickers else qualifying_tickers

        scale  = self.position_scale(regime_state)
        signals: List[Signal] = []

        for t in all_tickers[:_MAX_POS]:
            if t not in prices.columns:
                continue
            ts = prices[t].dropna()
            if len(ts) < 5:
                continue
            price = float(ts.iloc[-1])
            if price <= 0:
                continue

            pct  = round(_WEIGHT * scale, 4)
            conf = 'HIGH' if t in has_buyback else 'MED'
            st   = self.compute_stops_and_targets(
                ts, 'LONG', price, regime_state=regime_state)

            # earnings_date for signal metadata
            earn_date = earn_df.loc[
                earn_df['ticker'] == t, 'date'].iloc[0]

            signals.append(Signal(
                ticker            = t,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = pct,
                confidence        = conf,
                signal_params     = {
                    'strategy':     STRATEGY_ID,
                    'earnings_date': str(earn_date.date()),
                    'has_buyback':  t in has_buyback,
                    'max_hold_days': 25,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
