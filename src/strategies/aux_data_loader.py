"""
aux_data loader — builds the per-date `aux_data` dict that HV-series options
strategies consume in their `generate_signals(prices, regime, universe, aux_data)`.

Shape it produces (matches strategy expectations):

    {
        'options': {
            'AAPL': {
                'iv_rank': 62.3, 'iv30': 0.32, 'near_iv': 0.32, 'far_iv': 0.36,
                'iv_spread': 0.04, 'ts_ratio': 1.125, 'skew_20d': 0.058,
                'vrp': 0.08, 'vrp_zscore': 1.2,
                'pc_ratio': 0.33, 'unusual_flow': 0,
                'rv_20': 0.24, 'last_price': 270.23,
                'earnings_dte': 14,     # if available
            }, ...
        },
    }

Usage from auto_backtest.py:

    from strategies.aux_data_loader import load_aux_data
    aux = load_aux_data('2024-06-17')   # panel-backed, lazy-cached
    signals = strategy.generate_signals(prices, regime, universe, aux_data=aux)
"""
from __future__ import annotations
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
AGG_PATH = ROOT / 'data' / 'master' / 'options_aggregates_enriched.parquet'
EARNINGS_PATH = ROOT / 'data' / 'master' / 'earnings.parquet'

log = logging.getLogger(__name__)

_AGG_DF: Optional[pd.DataFrame] = None
_EARNINGS_DF: Optional[pd.DataFrame] = None


def _load_panel() -> pd.DataFrame:
    """Load enriched aggregates once into module-level cache."""
    global _AGG_DF
    if _AGG_DF is not None:
        return _AGG_DF
    if not AGG_PATH.exists():
        log.warning('aux_data_loader: %s missing — returning empty panel', AGG_PATH)
        _AGG_DF = pd.DataFrame()
        return _AGG_DF
    df = pd.read_parquet(AGG_PATH)
    df['date'] = pd.to_datetime(df['date'])
    _AGG_DF = df
    log.info('aux_data_loader: panel loaded rows=%d tickers=%d dates=%d',
             len(df), df['ticker'].nunique(), df['date'].nunique())
    return df


def _load_earnings() -> pd.DataFrame:
    global _EARNINGS_DF
    if _EARNINGS_DF is not None:
        return _EARNINGS_DF
    if not EARNINGS_PATH.exists():
        _EARNINGS_DF = pd.DataFrame()
        return _EARNINGS_DF
    e = pd.read_parquet(EARNINGS_PATH)
    # Expected schema: ticker, date (report date) — may have extras
    for c in ('date', 'report_date', 'earnings_date'):
        if c in e.columns:
            e['date'] = pd.to_datetime(e[c], errors='coerce')
            break
    _EARNINGS_DF = e[['ticker', 'date']].dropna() if 'date' in e.columns else pd.DataFrame()
    return _EARNINGS_DF


FIELDS = [
    'iv_front', 'iv_back', 'term_slope', 'otm_put_iv', 'otm_call_iv', 'skew',
    'put_call_vol_ratio', 'contracts_liquid', 'spot',
    'rv_20', 'vrp', 'iv_rank', 'vrp_zscore',
    'pc_ratio', 'iv_spread', 'ts_ratio', 'near_iv', 'far_iv', 'iv30',
    'unusual_flow',
    # Greeks + surface metrics (added in v2 backfill)
    'gamma_atm', 'theta_atm', 'gex',
    'iv_centroid_delta', 'surface_premium',
    # Rolling history lists (added in v2 enrichment)
    'iv_rank_history', 'hv20_history', 'vrp_history',
    'volume',
]

# Any `earnings_dte` beyond this gets suppressed — the earnings parquet
# is sparse; distant dates are usually stale/placeholder rather than real.
EARNINGS_DTE_MAX = 90


@lru_cache(maxsize=512)
def _day_slice(date_str: str) -> dict[str, dict]:
    """Return {ticker: {field: value, ...}} for a single date. Cached."""
    panel = _load_panel()
    if panel.empty:
        return {}
    ts = pd.to_datetime(date_str)
    day = panel[panel['date'] == ts]
    if day.empty:
        # Fall back to the most recent prior date (stale-but-best-available)
        prior = panel[panel['date'] <= ts]
        if prior.empty:
            return {}
        last_ts = prior['date'].max()
        day = panel[panel['date'] == last_ts]

    earn = _load_earnings()
    earn_map: dict[str, float] = {}
    if not earn.empty:
        future = earn[earn['date'] > ts]
        nearest = (future.sort_values('date').drop_duplicates('ticker'))
        nearest['dte'] = (nearest['date'] - ts).dt.days
        earn_map = dict(zip(nearest['ticker'], nearest['dte']))

    out: dict[str, dict] = {}
    for row in day.itertuples(index=False):
        sid: dict = {}
        for f in FIELDS:
            if hasattr(row, f):
                v = getattr(row, f)
                if v is not None and not (isinstance(v, float) and pd.isna(v)):
                    # Map to the aliases strategies use: skew_20d ← skew
                    sid[f] = v
        if hasattr(row, 'skew') and row.skew is not None and not pd.isna(row.skew):
            sid['skew_20d'] = row.skew
        if hasattr(row, 'spot') and row.spot is not None and not pd.isna(row.spot):
            sid['last_price'] = row.spot
        dte = earn_map.get(row.ticker)
        if dte is not None and dte <= EARNINGS_DTE_MAX:
            sid['earnings_dte'] = int(dte)
        out[row.ticker] = sid
    return out


def load_aux_data(date: str | pd.Timestamp) -> dict:
    """Return aux_data dict for a given trading date.

    date: 'YYYY-MM-DD' or pandas Timestamp.
    Returns: {'options': {ticker: {...fields...}}}.
    """
    date_str = str(date)[:10]
    return {'options': _day_slice(date_str)}


def available_dates() -> list[str]:
    panel = _load_panel()
    if panel.empty:
        return []
    return sorted(str(d.date()) for d in panel['date'].unique())


if __name__ == '__main__':
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else None
    dates = available_dates()
    print(f'Available dates: {len(dates)}  first={dates[0] if dates else "?"}  last={dates[-1] if dates else "?"}')
    if date is None and dates:
        date = dates[-1]
    if date:
        aux = load_aux_data(date)
        opts = aux.get('options', {})
        print(f'{date}: {len(opts)} tickers')
        for t in list(opts.keys())[:3]:
            print(f'  {t}: {opts[t]}')
