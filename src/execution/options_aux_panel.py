"""Live per-ticker options aux served from the SURFACE MASTER (the pre-aggregated
panel) instead of a raw ``options_eod`` window read.

Spec: docs/specs/2026-10-27-options-aux-panel-source-spec.md.

Why: the engine docstring has owed this since 2026-07-29 — the live path
re-aggregated 14 days of raw contract rows (16M rows touched in the October
month file; 4.3–5.2 GB peak, rc=137 at the 4500M step cap on 2026-10-27)
to derive per-ticker fields the nightly surface master already holds, row
for row, from the SAME ``strategies.options_surface`` functions. Serving the
master row costs one filtered read of a 30 MB parquet.

Source selection (read at call time; both default OFF):
  OPENCLAW_OPTIONS_SURFACE=1          v2 dict is load-bearing (existing flag, gated
                                      by scripts/options_surface_flip_after_shadow.sh)
  OPENCLAW_OPTIONS_SURFACE_SOURCE     'raw' (default) → v2 builds from the raw window
                                      'panel'         → v2 keys built here, from the master

With source=panel:
  * flag ON  → the engine serves this dict and never opens the raw window.
  * flag OFF → the engine still serves the legacy dict (raw read unchanged) but
    the `[options_surface] shadow` line compares legacy against THIS build —
    cheap enough to run on the 15:00 ET compute, which the raw v2 shadow
    skips (OPENCLAW_SIGNALS_SKIP_SHADOW_OPTIONS, M2 2026-09-28).

Row provenance per ticker (mirrors options_aux_v2.build exactly):
  * today's tier-1 raw overlay present (OPENCLAW_INTRADAY_AUX=1, fresh, rows for
    the ticker) → ``features_for_day(prepare_chain(overlay rows, today), spot,
    today)`` — the same call v2 makes on the spliced overlay rows; a master row
    dated today (none at 15:00 ET) takes precedence for iv30/pc_ratio as in v2.
  * otherwise → the latest master row dated <= today IS the per-day feature
    row (it was built by the same function from the same EOD rows), with
    ``near_iv = iv30`` as v2 sets under master precedence.
  Then ``series_features`` over the ticker's prior master rows + realized vol
  from the price window, CBOE OI features, earnings_dte, surface_date,
  spot_date — unchanged from v2.
"""
from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from execution.options_aux_v2 import HISTORY_DAYS, _HIST_COLS
from strategies.options_surface import (features_for_day, prepare_chain, rv_series_from_closes,
                                        series_features)

logger = logging.getLogger(__name__)
SOURCE_ENV = 'OPENCLAW_OPTIONS_SURFACE_SOURCE'
# Columns a master row contributes to the per-ticker dict. Everything the
# builder wrote except the key/bookkeeping columns.
_ROW_SKIP = {'ticker', 'date', 'built_at'}


def panel_source_enabled() -> bool:
    return os.environ.get(SOURCE_ENV, 'raw').strip().lower() == 'panel'


def master_path(master_dir) -> Path:
    return Path(os.environ.get('OPENCLAW_OPTIONS_SURFACE_PATH')
                or (Path(master_dir) / 'options_surface.parquet'))


def load_master_rows(master_dir, tickers, today: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """All surface-master columns per ticker for date in
    [today - HISTORY_DAYS, today], sorted by date. {} when the master is absent."""
    path = master_path(master_dir)
    if not path.exists():
        logger.warning('[options_panel] surface master %s missing — nothing to serve', path)
        return {}
    floor = (today - pd.Timedelta(days=HISTORY_DAYS))
    tick = list(tickers)
    schema = pq.read_schema(path)
    date_is_str = schema.field('date').type == 'string' or str(schema.field('date').type).startswith('large_string')
    if date_is_str:
        flt = ((pc.field('date') >= pc.scalar(floor.strftime('%Y-%m-%d')))
               & (pc.field('date') <= pc.scalar(today.strftime('%Y-%m-%d'))))
    else:
        flt = ((pc.field('date') >= pc.scalar(floor.date()))
               & (pc.field('date') <= pc.scalar(today.date())))
    if tick:
        flt = flt & pc.field('ticker').isin(tick)
    df = pq.read_table(path, filters=flt).to_pandas()
    if df.empty:
        return {}
    df['date'] = pd.to_datetime(df['date']).dt.normalize()
    if 'iv30_source' not in df.columns:
        df['iv30_source'] = None
    return {t: g.sort_values('date') for t, g in df.groupby('ticker')}


def load_today_overlay(today: pd.Timestamp, universe, max_age_h: float | None = None):
    """Today's tier-1 raw options overlay (same age guard the engine applies to
    the raw path), restricted to `universe`; None when absent/stale/disabled."""
    if os.environ.get('OPENCLAW_INTRADAY_AUX', '0') != '1':
        return None
    try:
        from ingestion.intraday_options import load_overlay, overlay_path
    except Exception as exc:  # noqa: BLE001
        logger.warning('[options_panel] intraday overlay unavailable (%s)', exc)
        return None
    if max_age_h is None:
        max_age_h = float(os.environ.get('OPENCLAW_INTRADAY_AUX_MAX_AGE_H', '6'))
    path = overlay_path(today, 'options_raw')
    if not path.exists():
        return None
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    if age_h > max_age_h:
        logger.warning('[options_panel] intraday options overlay is %.1fh old (max %.1fh) — ignored',
                       age_h, max_age_h)
        return None
    overlay = load_overlay(today, category='options_raw')
    if overlay is None or overlay.empty:
        return None
    overlay = overlay[overlay['ticker'].astype(str).isin(set(universe))].copy()
    if overlay.empty:
        return None
    overlay['date'] = pd.to_datetime(overlay['date'], errors='coerce')
    overlay['expiry'] = pd.to_datetime(overlay['expiry'], errors='coerce')
    return overlay


def _row_from_master(master_row: pd.Series) -> dict:
    out = {}
    for k, v in master_row.items():
        if k in _ROW_SKIP:
            continue
        if isinstance(v, float) and math.isnan(v):
            v = None
        elif v is pd.NaT:
            v = None
        elif hasattr(v, 'item') and not isinstance(v, (str, bytes)):
            try:
                v = v.item()
            except Exception:  # noqa: BLE001
                pass
        out[k] = v
    # v2 serves the master's iv30 as near_iv under master precedence.
    out['near_iv'] = out.get('iv30')
    # the master's `spot` is the per-day feature row's last_price (features_for_day emits both)
    if 'last_price' not in out:
        out['last_price'] = out.get('spot')
    return out


def build(universe, today, master_dir, px_window: pd.DataFrame | None,
          earnings_upcoming: pd.DataFrame | None = None,
          overlay: pd.DataFrame | None = None) -> dict[str, dict]:
    """{ticker: {…v2 keys…}} from the surface master (+ today's overlay)."""
    today = pd.Timestamp(today).normalize()
    uni = sorted(set(str(t) for t in universe))
    master = load_master_rows(master_dir, uni, today)
    over_by: dict[str, pd.DataFrame] = {}
    if overlay is not None and len(overlay):
        over_by = {t: g for t, g in overlay.groupby(overlay['ticker'].astype(str))}
    px = px_window.copy() if px_window is not None else pd.DataFrame(columns=['ticker', 'date', 'close'])
    if len(px):
        px['date'] = pd.to_datetime(px['date']).dt.normalize()
        px['ticker'] = px['ticker'].astype(str)
    closes_by = {t: g.set_index('date')['close'].sort_index() for t, g in px.groupby('ticker')} if len(px) else {}
    try:
        from strategies.options_oi import oi_features_for_ticker
    except ImportError:
        oi_features_for_ticker = None
    out: dict[str, dict] = {}
    n_overlay = n_master = 0
    for ticker in uni:
        hist = master.get(ticker)
        chain = over_by.get(ticker)
        if chain is None and (hist is None or hist.empty):
            continue
        closes = closes_by.get(ticker)
        if chain is not None:
            chain_date = today
        else:
            upto = hist[hist['date'] <= today]
            if upto.empty:
                continue
            chain_date = pd.Timestamp(upto['date'].iloc[-1])
        spot = spot_date = None
        if closes is not None and len(closes):
            upto_px = closes[closes.index <= chain_date]
            if len(upto_px):
                spot = float(upto_px.iloc[-1])
                spot_date = pd.Timestamp(upto_px.index[-1]).date().isoformat()
        master_today = None
        prior = hist
        if hist is not None and not hist.empty:
            if (hist['date'] == chain_date).any():
                master_today = hist[hist['date'] == chain_date].iloc[-1]
            prior = hist[hist['date'] < chain_date]
        if chain is not None:
            n_overlay += 1
            row = features_for_day(prepare_chain(chain, chain_date), spot, chain_date)
            if master_today is not None:                   # precedence: the official record wins (v2)
                if master_today['iv30'] == master_today['iv30']:
                    row['iv30'] = float(master_today['iv30'])
                    m_src = master_today.get('iv30_source')
                    row['iv30_source'] = m_src if isinstance(m_src, str) else None
                row['near_iv'] = row['iv30']
                if master_today['pc_ratio'] == master_today['pc_ratio']:
                    row['pc_ratio'] = float(master_today['pc_ratio'])
        else:
            n_master += 1
            row = _row_from_master(master_today)
            if spot is not None:
                row['last_price'] = spot
        rv = rv_series_from_closes(closes) if closes is not None else pd.Series(dtype=float)
        hist_cols = [c for c in _HIST_COLS[1:] if prior is not None and c in prior.columns]
        row.update(series_features({'date': chain_date, 'iv30': row.get('iv30'), 'pc_ratio': row.get('pc_ratio')},
                                   prior[hist_cols] if prior is not None and not prior.empty
                                   else pd.DataFrame(columns=_HIST_COLS[1:]), rv))
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
    logger.info('[options_panel] built %d tickers (overlay=%d master=%d, master rows for %d tickers)',
                len(out), n_overlay, n_master, len(master))
    return out
