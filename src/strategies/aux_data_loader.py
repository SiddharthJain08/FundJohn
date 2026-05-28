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
import bisect
import logging
import os
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
AGG_PATH = ROOT / 'data' / 'master' / 'options_aggregates_enriched.parquet'
EARNINGS_PATH = ROOT / 'data' / 'master' / 'earnings.parquet'
VOL_INDICES_PATH = ROOT / 'data' / 'master' / 'vol_indices.parquet'
INSIDER_PATH = ROOT / 'data' / 'master' / 'insider.parquet'

log = logging.getLogger(__name__)

_AGG_DF: Optional[pd.DataFrame] = None
_EARNINGS_DF: Optional[pd.DataFrame] = None
_VOL_INDICES_DF: Optional[pd.DataFrame] = None


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


# How far back to include insider transactions in a backtest slice.
# S12_insider uses lookback_days=20 trading days * 1.5 calendar buffer = 30 days.
# We supply a 45-day window so strategies with larger lookbacks are still covered.
INSIDER_SLICE_DAYS = 45

# Module-level caches for insider index structures.
# _INSIDER_DATE_INDEX: sorted list of date strings that have transactions.
# _INSIDER_BY_DATE:    {date_str: {ticker: [txn_dicts]}} — one dict per calendar date.
_INSIDER_DATE_INDEX: Optional[list] = None
_INSIDER_BY_DATE: Optional[dict] = None


def _build_insider_index() -> None:
    """Load insider.parquet and build a binary-searchable date index.

    This replaces per-call pandas filtering with a single parse pass +
    O(log n) date lookups. Building takes ~0.3s; a full 3700-bar backtest
    sweep of all slices takes <1s total (vs ~40s with per-bar filtering).
    """
    global _INSIDER_DATE_INDEX, _INSIDER_BY_DATE
    if _INSIDER_DATE_INDEX is not None:
        return
    if not INSIDER_PATH.exists():
        log.warning('aux_data_loader: %s missing — insider_txns will be empty', INSIDER_PATH)
        _INSIDER_DATE_INDEX = []
        _INSIDER_BY_DATE = {}
        return
    try:
        ins = pd.read_parquet(INSIDER_PATH)
        # Use to_dict('records') for vectorized loading — 50x faster than iterrows().
        ins['_dp_str'] = pd.to_datetime(ins['date'], errors='coerce').dt.strftime('%Y-%m-%d')
        ins = ins.dropna(subset=['_dp_str'])
        records = ins[['ticker', '_dp_str', 'date', 'transaction_type',
                       'insider_name', 'net_value', 'shares']].to_dict('records')
        # Group by calendar date of transaction for binary-search index.
        by_date: dict = defaultdict(lambda: defaultdict(list))
        for r in records:
            ds = r['_dp_str']
            # Store transactionDate as pd.Timestamp rather than str so downstream
            # strategies that call `pd.to_datetime(txn['transactionDate'])` get a
            # near-zero-cost conversion (Timestamp→Timestamp = 0.3µs vs str = 220µs).
            # This eliminates a major backtest bottleneck when 344 tickers each have
            # ~30 transactions to filter per bar.
            try:
                txn_ts = pd.Timestamp(ds)
            except Exception:
                txn_ts = ds  # fallback to string if parsing fails
            by_date[ds][str(r.get('ticker', ''))].append({
                'transactionDate': txn_ts,
                'transactionType': str(r.get('transaction_type', '') or ''),
                'reportingName':   str(r.get('insider_name', '') or ''),
                'value':           float(r.get('net_value', 0) or 0),
                'shares':          float(r.get('shares', 0) or 0),
            })
        _INSIDER_DATE_INDEX = sorted(by_date.keys())
        _INSIDER_BY_DATE = {d: dict(tickers) for d, tickers in by_date.items()}
        log.info('aux_data_loader: insider index built date_count=%d tickers=%d rows=%d',
                 len(_INSIDER_DATE_INDEX),
                 sum(len(v) for v in _INSIDER_BY_DATE.values()),
                 len(ins))
    except Exception as exc:
        log.warning('aux_data_loader: failed to build insider index: %s', exc)
        _INSIDER_DATE_INDEX = []
        _INSIDER_BY_DATE = {}


@lru_cache(maxsize=None)
def _insider_slice(date_str: str) -> dict:
    """Return insider_txns for the INSIDER_SLICE_DAYS window ending on date_str.

    Uses a binary-searchable date index so the entire 3700-bar backtest
    window can be served in <1s total (vs ~40s with per-bar pandas filtering).

    Returns {ticker: [{transactionDate, transactionType, reportingName, value, shares}]}
    matching the shape engine.py's load_aux_data() produces.

    Result is cached indefinitely (maxsize=None) to eliminate redundant
    recomputation across the backtest simulation loop.
    """
    _build_insider_index()
    if not _INSIDER_DATE_INDEX:
        return {}
    ts = pd.to_datetime(date_str)
    cutoff_str = str((ts - pd.Timedelta(days=INSIDER_SLICE_DAYS)).date())
    lo = bisect.bisect_left(_INSIDER_DATE_INDEX, cutoff_str)
    hi = bisect.bisect_right(_INSIDER_DATE_INDEX, date_str)
    if lo >= hi:
        return {}
    merged: dict = defaultdict(list)
    for i in range(lo, hi):
        d = _INSIDER_DATE_INDEX[i]
        for ticker, txns in _INSIDER_BY_DATE[d].items():
            merged[ticker].extend(txns)
    return dict(merged)


def _load_vol_indices() -> pd.DataFrame:
    global _VOL_INDICES_DF
    if _VOL_INDICES_DF is not None:
        return _VOL_INDICES_DF
    if not VOL_INDICES_PATH.exists():
        _VOL_INDICES_DF = pd.DataFrame()
        return _VOL_INDICES_DF
    df = pd.read_parquet(VOL_INDICES_PATH)
    df['date'] = pd.to_datetime(df['date'])
    _VOL_INDICES_DF = df.sort_values('date').reset_index(drop=True)
    return _VOL_INDICES_DF


def _vol_indices_slice(date_str: str) -> dict:
    """Return {vix_close, vvix_close, vix9d_close} for a given date.
    Falls back to most recent prior date when the requested date isn't a
    market session (weekends, holidays). Returns {} if the parquet is empty
    or the date is before the earliest available row."""
    df = _load_vol_indices()
    if df.empty:
        return {}
    ts = pd.to_datetime(date_str)
    prior = df[df['date'] <= ts]
    if prior.empty:
        return {}
    row = prior.iloc[-1]
    out = {}
    for col in ('vix_close', 'vvix_close', 'vix9d_close'):
        if col in df.columns:
            v = row.get(col)
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                out[col] = float(v)
    return out


# Cooldown lookback for recent stop-outs (calendar days). The strategy's
# `cooldown_after_stop_days` is in trading days (~10), so use a generous
# calendar buffer to cover weekends/holidays. This is the OUTER envelope —
# the strategy's own ``cooldown_after_stop_days`` is the binding comparison.
RECENT_STOP_OUTS_LOOKBACK_DAYS = 21


def _recent_stop_outs(
    date_str: str,
    strategy_id: str | None = None,
    run_stop_history: dict | None = None,
) -> dict:
    """Return per-ticker last-stop-out date scoped to the CURRENT backtest run.

    The previous implementation queried ``strategy_backtest_trades`` directly,
    which read PRIOR runs' trades and suppressed the same ticker in the next
    run (cross-run contamination — breaks reproducibility). Per-bar trades
    from the current run are not in the DB during simulation (run_backtest
    writes the run atomically at the end), so the DB path could never have
    given correct semantics either.

    The fix: the simulation loop owns ``run_stop_history`` (built up bar-by-bar
    from ``simulate_trade``'s exit_info) and passes it in. This function
    becomes a pure filter, with no DB or hidden state.

    When ``run_stop_history`` is not provided, returns ``{}`` — the cooldown
    is a no-op, which matches live trading where stop history flows through
    a different path (engine.load_aux_data, not this loader).
    """
    if not run_stop_history:
        return {}
    try:
        as_of = pd.to_datetime(date_str)
    except Exception:
        return {}
    cutoff = as_of - pd.Timedelta(days=RECENT_STOP_OUTS_LOOKBACK_DAYS)
    out: dict = {}
    for ticker, dt in run_stop_history.items():
        try:
            dt_ts = pd.to_datetime(dt)
        except Exception:
            continue
        # Strictly past (no look-ahead) and within the outer envelope.
        if dt_ts >= as_of:
            continue
        if dt_ts < cutoff:
            continue
        prev = out.get(ticker)
        if prev is None or dt_ts > prev:
            out[ticker] = dt_ts
    return out


def load_aux_data(
    date: str | pd.Timestamp,
    strategy_id: str | None = None,
    run_stop_history: dict | None = None,
) -> dict:
    """Return aux_data dict for a given trading date.

    date: 'YYYY-MM-DD' or pandas Timestamp.
    strategy_id: optional — used to gate inclusion of recent_stop_outs. When
        present, recent_stop_outs is computed from ``run_stop_history`` (if
        provided by the caller — typically the backtest simulation loop).
    run_stop_history: optional dict {ticker: pd.Timestamp} representing
        within-run stop-out exits. Filtered to the lookback envelope and
        strictly-past dates relative to ``date`` before being injected into
        aux_data['recent_stop_outs'].

    Returns: {
        'options':      {ticker: {...fields...}},
        'vol_indices':  {vix_close, vvix_close, vix9d_close},
        'insider_txns': {ticker: [{transactionDate, transactionType,
                                   reportingName, value, shares}]},
        'recent_stop_outs': {ticker: pd.Timestamp},  # when strategy_id given
    }
    insider_txns is filtered to a INSIDER_SLICE_DAYS-day rolling window ending on
    `date` so per-bar calls in backtest simulation don't carry the full history.
    This matches the effective content engine.py serves in live trading (same-day
    data fetch naturally contains only recent filings). Strategies still apply
    their own lookback window filter as they do in production.
    """
    date_str = str(date)[:10]
    out = {
        'options':      _day_slice(date_str),
        'vol_indices':  _vol_indices_slice(date_str),
        'insider_txns': _insider_slice(date_str),
    }
    if strategy_id:
        out['recent_stop_outs'] = _recent_stop_outs(
            date_str, strategy_id, run_stop_history=run_stop_history
        )
    return out


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
