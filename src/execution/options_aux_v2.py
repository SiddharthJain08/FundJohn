"""Live per-ticker options aux, version 2 (spec 2026-09-04 A.7) — the same
strategies.options_surface functions the backtest panel is built from.

build() returns {ticker: {…}} with every key the strategies read. The engine
serves it when OPENCLAW_OPTIONS_SURFACE=1 and otherwise logs a one-line
shadow comparison against the legacy dict.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from strategies.options_surface import (OPTIONS_FEATURES_VERSION, features_for_day,
                                        prepare_chain, rv_series_from_closes, series_features)

logger = logging.getLogger(__name__)
FLAG = 'OPENCLAW_OPTIONS_SURFACE'
BUDGET_ENV = 'OPENCLAW_OPTIONS_SURFACE_BUDGET_S'
DEFAULT_BUDGET_S = 240.0
HISTORY_DAYS = 400
_HIST_COLS = ['ticker', 'date', 'iv30', 'pc_ratio', 'iv30_source']


def enabled() -> bool:
    return os.environ.get(FLAG, '0') == '1'


def budget_seconds() -> float:
    """Per-run wall-clock budget for build() (spec 2026-09-04 A.7 fix wave).
    In shadow mode a slow surface compute is pure cost — the engine serves the
    legacy dict either way — so the loop stops at the budget and returns what
    it has. With the flag ON the dict is load-bearing, so the budget only
    warns."""
    try:
        return float(os.environ.get(BUDGET_ENV, DEFAULT_BUDGET_S))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_S


def load_history(master_dir: Path, tickers, before: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Surface-master rows per ticker with date < before (last HISTORY_DAYS calendar days),
    plus the row dated `before` itself when the master already holds it.

    `iv30_source` (amendment 2026-09-06 §H) is read when the master carries it; an
    older master vintage without the column still loads (schema-checked first),
    with `iv30_source` filled `None` for every row so the precedence block in
    `build()` always finds the key."""
    path = Path(os.environ.get('OPENCLAW_OPTIONS_SURFACE_PATH') or (Path(master_dir) / 'options_surface.parquet'))
    if not path.exists():
        logger.warning('[options_surface] master %s missing — iv_rank/histories unavailable this run', path)
        return {}
    try:
        schema_cols = set(pq.read_schema(path).names)
    except Exception:  # unreadable schema — fall back to requesting everything and let the read below fail loud
        schema_cols = set(_HIST_COLS)
    cols = [c for c in _HIST_COLS if c in schema_cols]
    floor = (before - pd.Timedelta(days=HISTORY_DAYS)).strftime('%Y-%m-%d')
    flt = (pc.field('date') >= pc.scalar(pd.Timestamp(floor).date())) & (pc.field('date') <= pc.scalar(before.date())) \
        & pc.field('ticker').isin(list(tickers))
    try:
        df = pq.read_table(path, columns=cols, filters=flt).to_pandas()
    except Exception:  # date stored as string in some vintages
        flt = (pc.field('date') >= pc.scalar(floor)) & (pc.field('date') <= pc.scalar(before.strftime('%Y-%m-%d'))) \
            & pc.field('ticker').isin(list(tickers))
        df = pq.read_table(path, columns=cols, filters=flt).to_pandas()
    if 'iv30_source' not in df.columns:
        df['iv30_source'] = None
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
    t0 = time.monotonic()
    budget, live, over_warned = budget_seconds(), enabled(), False
    # `tickers` is already the distinct ticker list, so the budget message can
    # report k/n without materialising every groupby sub-frame (which would
    # hold a second copy of the whole chain — a memory regression in a fix
    # whose whole point is cost).
    n_total = len(tickers)
    for ticker, grp in opts.groupby(opts['ticker'].astype(str)):
        chain_date = pd.to_datetime(grp['date']).max().normalize()
        chain = grp[pd.to_datetime(grp['date']).dt.normalize() == chain_date]
        closes = closes_by.get(ticker)
        spot = spot_date = None
        if closes is not None and len(closes):
            # LAST KNOWN close at or before the chain date — under the intraday
            # overlay that is T−1 while the chain is dated today. spot_date is
            # reported so the shadow line can count the stale ones.
            upto = closes[closes.index <= chain_date]
            if len(upto):
                spot = float(upto.iloc[-1])
                spot_date = pd.Timestamp(upto.index[-1]).date().isoformat()
        hist = history.get(ticker)
        master_today = None
        if hist is not None and (hist['date'] == chain_date).any():
            master_today = hist[hist['date'] == chain_date].iloc[-1]
            hist = hist[hist['date'] < chain_date]
        row = features_for_day(prepare_chain(chain, chain_date), spot, chain_date)
        if master_today is not None:                       # precedence: the official record wins
            if master_today['iv30'] == master_today['iv30']:
                row['iv30'] = float(master_today['iv30'])
                # the master's iv30 wins, so its provenance must win too — never
                # leave the fresh live fit's iv30_source beside a master value.
                m_src = master_today.get('iv30_source')
                row['iv30_source'] = m_src if isinstance(m_src, str) else None
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
        row['spot_date'] = spot_date
        out[ticker] = row
        if time.monotonic() - t0 > budget:
            if not live:
                logger.warning('[options_surface] budget %.0fs exceeded after %d/%d tickers — partial shadow',
                               budget, len(out), n_total)
                break
            if not over_warned:
                logger.warning('[options_surface] budget %.0fs exceeded after %d/%d tickers — flag is ON, '
                               'running to completion', budget, len(out), n_total)
                over_warned = True
    return out


def _pct_of_new(new: dict, predicate) -> int:
    """Share of the v2 dict's tickers satisfying `predicate`, 0-100. The
    denominator is always len(new) — every percentage on the shadow line is
    read against the same base."""
    return round(100.0 * sum(1 for t in new if predicate(new[t])) / len(new)) if new else 0


def _pct_of_subset(new: dict, subset, predicate) -> int:
    """Share of the tickers satisfying `subset` that also satisfy `predicate`,
    0-100. Used where the spec defines a coverage ratio over a sub-population
    (v3 coverage is measured on tickers with ≥ 2 fitted expiries, spec §C.3 —
    the same denominator the rollout verify uses). Empty subset ⇒ 0, never a
    division by zero and never a fabricated 100 %."""
    n = sum(1 for t in new if subset(new[t]))
    if not n:
        return 0
    return round(100.0 * sum(1 for t in new if subset(new[t]) and predicate(new[t])) / n)


def shadow_summary(old: dict, new: dict, seconds=None) -> str:
    """One-line old-vs-new comparison (spec 2026-09-04 A.7).

    `rv20_nonnull` / `vrp_nonnull` catch the F1 regression class — the live
    path losing rv_20 (and therefore vrp/vrp_zscore) whenever prices.parquet
    lags the chain date. `spot_stale` is the share of tickers priced off a
    close OLDER than the surface date (normal at the 15:00 ET compute, since
    the day's close does not exist yet). `mfiv_nonnull`/`rn_nonnull` are the
    v3 coverage — spec 2026-09-06 §C.3 expects ≥ 90 % of tickers with ≥ 2
    fitted expiries, and that sub-population IS their denominator (final
    review I2), matching the rollout verify. `iv30_src smile=…% band=…%`
    splits the served `iv30` by provenance over the tickers that have one
    (spec §H.5); the two need not sum to 100 — a master row without the
    column serves `iv30` with `iv30_source = None`. `dur` is the build's
    wall clock, or `n/a` when the caller did not time it."""
    common = [t for t in new if t in old]
    ratios = [old[t]['iv30'] / new[t]['iv30'] for t in common
              if old[t].get('iv30') and new[t].get('iv30')]
    med = float(np.median(ratios)) if ratios else float('nan')
    p90 = float(np.percentile(ratios, 90)) if ratios else float('nan')
    pct = _pct_of_new(new, lambda r: r.get('iv_rank') is not None)
    rv_pct = _pct_of_new(new, lambda r: r.get('rv_20') is not None)
    vrp_pct = _pct_of_new(new, lambda r: r.get('vrp') is not None)
    fit2 = lambda r: (r.get('n_expiries_fit') or 0) >= 2      # noqa: E731 — spec §C.3 denominator
    mf_pct = _pct_of_subset(new, fit2, lambda r: r.get('mfiv_30d') is not None)
    rn_pct = _pct_of_subset(new, fit2, lambda r: r.get('rn_skew_30d') is not None)
    has_iv30 = lambda r: r.get('iv30') is not None            # noqa: E731 — §H.5 split denominator
    smile_pct = _pct_of_subset(new, has_iv30, lambda r: r.get('iv30_source') == 'smile')
    band_pct = _pct_of_subset(new, has_iv30, lambda r: r.get('iv30_source') == 'atm_band')
    stale_pct = _pct_of_new(new, lambda r: r.get('spot_date') is not None
                            and r.get('surface_date') is not None
                            and r['spot_date'] < r['surface_date'])
    dur = 'n/a' if seconds is None else f'{float(seconds):.0f}s'
    return (f'[options_surface] shadow n={len(new)} iv30 old/new median={med:.3f} p90={p90:.3f} '
            f'iv_rank_nonnull={pct}% rv20_nonnull={rv_pct}% vrp_nonnull={vrp_pct}% '
            f'mfiv_nonnull={mf_pct}% rn_nonnull={rn_pct}% '
            f'iv30_src smile={smile_pct}% band={band_pct}% '
            f'spot_stale={stale_pct}% dur={dur} version={OPTIONS_FEATURES_VERSION}')
