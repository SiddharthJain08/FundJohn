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
MACRO_PATH = ROOT / 'data' / 'master' / 'macro.parquet'
FINANCIALS_PATH = ROOT / 'data' / 'master' / 'financials.parquet'
INSIDER_PATH = ROOT / 'data' / 'master' / 'insider.parquet'
SENTIMENT_PATH = ROOT / 'data' / 'master' / 'sentiment.parquet'

log = logging.getLogger(__name__)

_AGG_DF: Optional[pd.DataFrame] = None
_EARNINGS_DF: Optional[pd.DataFrame] = None
_VOL_INDICES_DF: Optional[pd.DataFrame] = None
_MACRO_SERIES: Optional[dict] = None
_FIN_DF: Optional[pd.DataFrame] = None
_SENT_DF = None

# camelCase aliases MUST mirror engine.py load_aux_data exactly — strategies
# read the FMP-style names (S10_quality_value, S_bankruptcy_risk_anomaly).
_FIN_CAMEL_ALIASES = {
    'returnOnEquity':           'roe',
    'returnOnInvestedCapital':  'roic',
    'grossProfitMargin':        'gross_margin',
    'debtEquityRatio':          'debt_equity_ratio',
    'enterpriseValueMultiple':  'ev_ebitda',
    'priceToFreeCashFlowsRatio': 'p_fcf_ratio',
    'totalAssets':              'total_assets',
    'totalLiabilities':         'total_liabilities',
    'retainedEarnings':         'retained_earnings',
    'workingCapital':           'working_capital',
    'operatingIncome':          'operating_income',
    'marketCap':                'market_cap',
}
SENTIMENT_FIELDS = ['news_count_24h', 'news_mean_score', 'news_finbert_pos',
                    'news_finbert_neu', 'news_finbert_neg']
# News sentiment decays fast; do not serve a score older than this (avoids stale-data signals).
SENTIMENT_MAX_AGE_DAYS = 7


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
INSIDER_LONG_DAYS = 450   # 15 months — Stage 2 classifier window for S15

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
        # Project the parquet columns we surface to strategies. role +
        # shares_owned_after are required by S15's Stage 3 conviction filter
        # (C-suite role check + personal-stake calculation); their absence
        # silently blocks every S15 signal on real data.
        projected_cols = ['ticker', '_dp_str', 'date', 'transaction_type',
                          'insider_name', 'net_value', 'shares']
        for opt_col in ('role', 'shares_owned_after'):
            if opt_col in ins.columns:
                projected_cols.append(opt_col)
        records = ins[projected_cols].to_dict('records')
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
            role_raw = r.get('role')
            after_raw = r.get('shares_owned_after')
            txn = {
                'transactionDate': txn_ts,
                'transactionType': str(r.get('transaction_type', '') or ''),
                'reportingName':   str(r.get('insider_name', '') or ''),
                'value':           float(r.get('net_value', 0) or 0),
                'shares':          float(r.get('shares', 0) or 0),
                'role':            str(role_raw) if role_raw is not None and not pd.isna(role_raw) else None,
                'sharesOwnedAfter': float(after_raw) if after_raw is not None and not pd.isna(after_raw) else None,
            }
            by_date[ds][str(r.get('ticker', ''))].append(txn)
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


@lru_cache(maxsize=None)
def _insider_long_slice(date_str: str) -> dict:
    """Return insider history for the INSIDER_LONG_DAYS window ending on date_str.

    Mirrors _insider_slice's structure but with a 15-month window for S15's
    opportunistic-vs-routine classifier. Returns the same per-txn dict shape:
    {ticker: [{transactionDate, transactionType, reportingName, value,
              shares, sharesOwnedAfter, pricePerShare}]}.

    Cached indefinitely (lru_cache maxsize=None) — same pattern as
    _insider_slice. Caller is expected to slice further by sub-window
    (e.g. t-15 to t-3 months) inside the strategy.
    """
    _build_insider_index()
    if not _INSIDER_DATE_INDEX:
        return {}
    ts = pd.to_datetime(date_str)
    cutoff_str = str((ts - pd.Timedelta(days=INSIDER_LONG_DAYS)).date())
    lo = bisect.bisect_left(_INSIDER_DATE_INDEX, cutoff_str)
    hi = bisect.bisect_right(_INSIDER_DATE_INDEX, date_str)
    if lo >= hi:
        return {}
    merged: dict = defaultdict(list)
    for d in _INSIDER_DATE_INDEX[lo:hi]:
        for ticker, txns in _INSIDER_BY_DATE.get(d, {}).items():
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


def _load_macro_series() -> dict:
    """{series_name: pd.Series(DatetimeIndex → value)} — full history, cached.

    Mirrors engine.py's live aux['macro'] construction exactly (macro.parquet
    long format date/series/value → per-series sorted Series). Strategies like
    S_tr_01_vvix_early_warning consume aux_data['macro']['VVIX'] as a SERIES
    (63-day z-window); the scalar vol_indices day-slice above cannot serve
    that — which made every macro-series strategy backtest-blind until
    2026-07-03.
    """
    global _MACRO_SERIES
    if _MACRO_SERIES is not None:
        return _MACRO_SERIES
    if not MACRO_PATH.exists():
        log.warning('aux_data_loader: %s missing — macro series unavailable', MACRO_PATH)
        _MACRO_SERIES = {}
        return _MACRO_SERIES
    try:
        mac = pd.read_parquet(MACRO_PATH)
        if mac.empty or not {'date', 'series', 'value'}.issubset(mac.columns):
            log.warning('aux_data_loader: macro.parquet empty/malformed — unavailable')
            _MACRO_SERIES = {}
            return _MACRO_SERIES
        mac['date'] = pd.to_datetime(mac['date'])
        _MACRO_SERIES = {
            name: grp.set_index('date')['value'].sort_index().dropna()
            for name, grp in mac.groupby('series')
        }
        log.info('aux_data_loader: macro loaded series=%s', sorted(_MACRO_SERIES))
    except Exception as e:  # noqa: BLE001 — aux is best-effort, never fatal
        log.warning('aux_data_loader: macro load failed (%s) — unavailable', e)
        _MACRO_SERIES = {}
    return _MACRO_SERIES


def _macro_slice(date_str: str) -> dict:
    """Point-in-time macro: each series truncated to rows <= date (no
    look-ahead). Empty dict when macro.parquet is absent (fail-open —
    strategies already guard on missing macro)."""
    full = _load_macro_series()
    if not full:
        return {}
    ts = pd.to_datetime(date_str)
    out = {}
    for name, s in full.items():
        cut = s.loc[:ts]
        if len(cut):
            out[name] = cut
    return out


def _load_financials() -> pd.DataFrame:
    global _FIN_DF
    if _FIN_DF is not None:
        return _FIN_DF
    if not FINANCIALS_PATH.exists():
        log.warning('aux_data_loader: %s missing — financials unavailable', FINANCIALS_PATH)
        _FIN_DF = pd.DataFrame()
        return _FIN_DF
    try:
        df = pd.read_parquet(FINANCIALS_PATH)
        df['date'] = pd.to_datetime(df['date'])
        _FIN_DF = df.sort_values('date')
        log.info('aux_data_loader: financials loaded rows=%d tickers=%d',
                 len(df), df['ticker'].nunique())
    except Exception as e:  # noqa: BLE001 — aux is best-effort, never fatal
        log.warning('aux_data_loader: financials load failed (%s)', e)
        _FIN_DF = pd.DataFrame()
    return _FIN_DF


def _financials_slice(date_str: str) -> dict:
    """Point-in-time financials: latest reported row per ticker with
    period date <= as-of date, shaped {ticker: {field: value, ...camelCase
    aliases}} exactly like engine.py's live aux['financials'].

    Coverage note: financials.parquet holds a rolling ~4-6 quarters
    (collector limit=4/quarterly), so deep-history backtest bars simply see
    no financials (strategies already guard) — recent-window bars get the
    real as-of snapshot. Added 2026-07-03 so financials strategies (S10,
    S_bankruptcy) are no longer structurally backtest-blind.
    """
    df = _load_financials()
    if df.empty:
        return {}
    ts = pd.to_datetime(date_str)
    asof = df[df['date'] <= ts]
    if asof.empty:
        return {}
    latest = asof.groupby('ticker').last()
    out: dict = {}
    for ticker, row in latest.iterrows():
        d = {
            k: (float(v) if pd.notna(v) else None)
            for k, v in row.items()
            if k not in ('date', 'period') and not isinstance(v, str)
        }
        for camel, snake in _FIN_CAMEL_ALIASES.items():
            d[camel] = d.get(snake)
        out[ticker] = d
    return out


def _load_sentiment_panel():
    global _SENT_DF
    if _SENT_DF is not None:
        return _SENT_DF
    if not SENTIMENT_PATH.exists():
        _SENT_DF = pd.DataFrame(); return _SENT_DF
    df = pd.read_parquet(SENTIMENT_PATH)
    df['date'] = pd.to_datetime(df['date'])
    _SENT_DF = df
    return df


def _sentiment_day_slice(date_str):
    panel = _load_sentiment_panel()
    if panel.empty:
        return {}
    ts = pd.to_datetime(date_str)
    day = panel[panel['date'] <= ts]          # point-in-time: never future
    if day.empty:
        return {}
    latest = day.sort_values('date').drop_duplicates('ticker', keep='last')
    # Staleness cap: news sentiment decays fast — a score older than SENTIMENT_MAX_AGE_DAYS
    # is noise, not signal. Without this, a ticker that stops making news would drive a
    # live LONG/SHORT off a months-old score indefinitely (silent-stale-data failure mode).
    latest = latest[(ts - latest['date']).dt.days <= SENTIMENT_MAX_AGE_DAYS]
    if latest.empty:
        return {}
    out = {}
    for row in latest.itertuples(index=False):
        d = {f: getattr(row, f) for f in SENTIMENT_FIELDS
             if hasattr(row, f) and getattr(row, f) is not None
             and not (isinstance(getattr(row, f), float) and pd.isna(getattr(row, f)))}
        if d:
            out[row.ticker] = d
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
        'options':              {ticker: {...fields...}},
        'vol_indices':          {vix_close, vvix_close, vix9d_close},
        'macro':                {series_name: pd.Series(date → value)},
                                # point-in-time (rows <= date); mirrors the
                                # live engine.py aux['macro'] format exactly
                                # (VIX / VIX3M / VIX9D / VVIX from macro.parquet)
        'financials':           {ticker: {field: value, +camelCase aliases}},
                                # point-in-time latest report per ticker
                                # (period date <= as-of); mirrors engine.py's
                                # live aux['financials'] incl. aliases
        'insider_txns':         {ticker: [{transactionDate, transactionType,
                                           reportingName, value, shares}]},
        'insider_history_long': {ticker: [{transactionDate, transactionType,
                                           reportingName, value, shares}]},
                                # 15-month (INSIDER_LONG_DAYS=450d) rolling window
                                # for S15_insider_opportunistic_short Stage 2
                                # classifier (t-15 to t-3 month pattern check).
                                # Caller slices further by sub-window as needed.
        'recent_stop_outs': {ticker: pd.Timestamp},  # when strategy_id given
    }
    insider_txns is filtered to a INSIDER_SLICE_DAYS-day rolling window ending on
    `date` so per-bar calls in backtest simulation don't carry the full history.
    insider_history_long uses a INSIDER_LONG_DAYS-day (15-month) window for S15's
    opportunistic-vs-routine classifier; it is purely additive and does not alter
    insider_txns. Both match the effective content engine.py serves in live trading
    (same-day data fetch naturally contains only recent filings). Strategies still
    apply their own lookback window filter as they do in production.
    """
    date_str = str(date)[:10]
    out = {
        'options':              _day_slice(date_str),
        'vol_indices':          _vol_indices_slice(date_str),
        'macro':                _macro_slice(date_str),
        'financials':           _financials_slice(date_str),
        'insider_txns':         _insider_slice(date_str),
        'insider_history_long': _insider_long_slice(date_str),
        'sentiment':            _sentiment_day_slice(date_str),
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
