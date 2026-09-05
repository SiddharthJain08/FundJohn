"""Live per-ticker options aux, version 2 (spec 2026-09-04 A.7) — the same
strategies.options_surface functions the backtest panel is built from.

build() returns {ticker: {…}} with every key the strategies read. The engine
serves it when OPENCLAW_OPTIONS_SURFACE=1 and otherwise logs a one-line
shadow comparison against the legacy dict.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from strategies.options_surface import (OPTIONS_FEATURES_VERSION, features_for_day,
                                        prepare_chain, rv_series_from_closes, series_features)

logger = logging.getLogger(__name__)
FLAG = 'OPENCLAW_OPTIONS_SURFACE'
HISTORY_DAYS = 400
_HIST_COLS = ['ticker', 'date', 'iv30', 'pc_ratio']


def enabled() -> bool:
    return os.environ.get(FLAG, '0') == '1'


def load_history(master_dir: Path, tickers, before: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Surface-master rows per ticker with date < before (last HISTORY_DAYS calendar days),
    plus the row dated `before` itself when the master already holds it."""
    path = Path(os.environ.get('OPENCLAW_OPTIONS_SURFACE_PATH') or (Path(master_dir) / 'options_surface.parquet'))
    if not path.exists():
        logger.warning('[options_surface] master %s missing — iv_rank/histories unavailable this run', path)
        return {}
    floor = (before - pd.Timedelta(days=HISTORY_DAYS)).strftime('%Y-%m-%d')
    flt = (pc.field('date') >= pc.scalar(pd.Timestamp(floor).date())) & (pc.field('date') <= pc.scalar(before.date())) \
        & pc.field('ticker').isin(list(tickers))
    try:
        df = pq.read_table(path, columns=_HIST_COLS, filters=flt).to_pandas()
    except Exception:  # date stored as string in some vintages
        flt = (pc.field('date') >= pc.scalar(floor)) & (pc.field('date') <= pc.scalar(before.strftime('%Y-%m-%d'))) \
            & pc.field('ticker').isin(list(tickers))
        df = pq.read_table(path, columns=_HIST_COLS, filters=flt).to_pandas()
    df['date'] = pd.to_datetime(df['date']).dt.normalize()
    return {t: g.sort_values('date') for t, g in df.groupby('ticker')}


def build(opts: pd.DataFrame, universe, today, master_dir, px_window: pd.DataFrame,
          earnings_upcoming: pd.DataFrame | None = None) -> dict[str, dict]:
    today = pd.Timestamp(today).normalize()
    uni = set(universe)
    if opts is None or len(opts) == 0:
        return {}
    opts = opts[opts['ticker'].astype(str).isin(uni)]
    tickers = sorted(opts['ticker'].astype(str).unique())
    history = load_history(master_dir, tickers, today)
    px = px_window.copy() if px_window is not None else pd.DataFrame(columns=['ticker', 'date', 'close'])
    px['date'] = pd.to_datetime(px['date']).dt.normalize()
    px['ticker'] = px['ticker'].astype(str)
    closes_by = {t: g.set_index('date')['close'].sort_index() for t, g in px.groupby('ticker')}
    try:
        from strategies.options_oi import oi_features_for_ticker      # Part B; optional until task 13 lands
    except ImportError:
        oi_features_for_ticker = None
    out: dict[str, dict] = {}
    for ticker, grp in opts.groupby(opts['ticker'].astype(str)):
        chain_date = pd.to_datetime(grp['date']).max().normalize()
        chain = grp[pd.to_datetime(grp['date']).dt.normalize() == chain_date]
        closes = closes_by.get(ticker)
        spot = None
        if closes is not None and len(closes):
            upto = closes[closes.index <= chain_date]
            spot = float(upto.iloc[-1]) if len(upto) else None
        hist = history.get(ticker)
        master_today = None
        if hist is not None and (hist['date'] == chain_date).any():
            master_today = hist[hist['date'] == chain_date].iloc[-1]
            hist = hist[hist['date'] < chain_date]
        row = features_for_day(prepare_chain(chain, chain_date), spot, chain_date)
        if master_today is not None:                       # precedence: the official record wins
            row['iv30'] = float(master_today['iv30']) if master_today['iv30'] == master_today['iv30'] else row['iv30']
            row['near_iv'] = row['iv30']
            if master_today['pc_ratio'] == master_today['pc_ratio']:
                row['pc_ratio'] = float(master_today['pc_ratio'])
        rv = rv_series_from_closes(closes) if closes is not None else pd.Series(dtype=float)
        row.update(series_features({'date': chain_date, 'iv30': row['iv30'], 'pc_ratio': row['pc_ratio']},
                                   hist if hist is not None else pd.DataFrame(columns=_HIST_COLS[1:]), rv))
        if oi_features_for_ticker is not None:
            row.update(oi_features_for_ticker(ticker, chain_date, master_dir) or {})
        else:
            row.update({'gex': None, 'iv_centroid_delta': None, 'surface_premium': None, 'contracts_liquid': None,
                        'open_interest_by_strike': {}, 'max_pain': None, 'pcr_oi': None, 'oi_session': None})
        row['earnings_dte'] = None
        if earnings_upcoming is not None and len(earnings_upcoming):
            e = earnings_upcoming[earnings_upcoming['ticker'] == ticker]
            if not e.empty:
                row['earnings_dte'] = int((pd.to_datetime(e['date']).min() - today).days)
        row['surface_date'] = chain_date.date().isoformat()
        out[ticker] = row
    return out


def shadow_summary(old: dict, new: dict) -> str:
    common = [t for t in new if t in old]
    ratios = [old[t]['iv30'] / new[t]['iv30'] for t in common
              if old[t].get('iv30') and new[t].get('iv30')]
    nonnull = sum(1 for t in new if new[t].get('iv_rank') is not None)
    med = float(np.median(ratios)) if ratios else float('nan')
    p90 = float(np.percentile(ratios, 90)) if ratios else float('nan')
    pct = round(100.0 * nonnull / len(new)) if new else 0
    return (f'[options_surface] shadow n={len(new)} iv30 old/new median={med:.3f} p90={p90:.3f} '
            f'iv_rank_nonnull={pct}% version={OPTIONS_FEATURES_VERSION}')
