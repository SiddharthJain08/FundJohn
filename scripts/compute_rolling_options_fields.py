#!/usr/bin/env python3
"""
Builds `data/master/options_aggregates_enriched.parquet` — the ONLY options
source the backtest's `aux_data_loader._day_slice` reads — from the
`strategies.options_surface` master (`data/master/options_surface.parquet`)
plus `prices.parquet` closes.

    iv_rank       : 252-session percentile of iv30 per ticker (0..100, None below 20 obs)
    rv_20         : 20-session log-return vol (annualized) from prices.parquet
    vrp           : iv30 - rv_20
    vrp_zscore    : zscore of vrp over trailing 60 sessions per ticker
    iv_rank_history, hv20_history, vrp_history, pc_ratio_history : trailing-20 lists
    Legacy aliases (LEGACY_ALIASES): iv_front=iv30, iv_back=iv90,
      otm_put_iv=iv_25d_put_30d, otm_call_iv=iv_25d_call_30d, skew=skew_25d_30d,
      skew_20d=skew_25d_30d, put_call_vol_ratio=pc_ratio, near_iv=iv30, far_iv=iv90
    unusual_flow  : int(pc_ratio > 1.5)

All feature math (iv_rank/vrp/vrp_zscore/history lists) lives in
strategies.options_surface.series_frame — the single implementation shared
with the live per-ticker call (engine.load_aux_data).
"""
from __future__ import annotations
import logging, os, sys, time
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from strategies.options_surface import SCALAR_KEYS, SERIES_KEYS, series_frame, rv_series_from_closes  # noqa: E402

log = logging.getLogger('compute_rolling_options_fields')

SURFACE_PATH = ROOT / 'data' / 'master' / 'options_surface.parquet'
OUT_PATH = ROOT / 'data' / 'master' / 'options_aggregates_enriched.parquet'
PRICES_PATH = ROOT / 'data' / 'master' / 'prices.parquet'

LEGACY_ALIASES = {'iv_front': 'iv30', 'iv_back': 'iv90', 'otm_put_iv': 'iv_25d_put_30d',
                  'otm_call_iv': 'iv_25d_call_30d', 'skew': 'skew_25d_30d', 'skew_20d': 'skew_25d_30d',
                  'put_call_vol_ratio': 'pc_ratio', 'near_iv': 'iv30', 'far_iv': 'iv90'}


def load_surface() -> pd.DataFrame:
    if not SURFACE_PATH.exists():
        raise SystemExit(f'No surface master at {SURFACE_PATH} — run scripts/build_options_surface.py first')
    df = pd.read_parquet(SURFACE_PATH)
    df['date'] = pd.to_datetime(df['date'])
    return df.sort_values(['ticker', 'date']).reset_index(drop=True)


def load_closes(tickers: set, floor: pd.Timestamp) -> pd.DataFrame:
    tbl = pq.read_table(PRICES_PATH, columns=['ticker', 'date', 'close'], read_dictionary=['ticker', 'date'],
                        filters=(pc.field('date') >= pc.scalar(floor.strftime('%Y-%m-%d'))))
    px = tbl.to_pandas(); del tbl
    px['ticker'] = px['ticker'].astype(str)
    px = px[px['ticker'].isin(tickers)]
    px['date'] = pd.to_datetime(px['date'])
    return px[['ticker', 'date', 'close']]


def build_panel(surface: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """Enriched backtest panel: surface scalars + series features + legacy aliases."""
    surf = surface.copy()
    surf['date'] = pd.to_datetime(surf['date'])
    closes = closes.copy(); closes['date'] = pd.to_datetime(closes['date'])
    rv_parts = []
    for ticker, g in closes.sort_values(['ticker', 'date']).groupby('ticker'):
        rv_parts.append(pd.DataFrame({'ticker': g['ticker'].values, 'date': g['date'].values,
                                      'rv_20': rv_series_from_closes(g.set_index('date')['close']).values}))
    rv = pd.concat(rv_parts, ignore_index=True) if rv_parts else pd.DataFrame(columns=['ticker', 'date', 'rv_20'])
    surf = surf.merge(rv, on=['ticker', 'date'], how='left')
    parts = []
    for _, g in surf.groupby('ticker', sort=True):
        parts.append(series_frame(g))
    out = pd.concat(parts, ignore_index=True)
    # A real surface master (build_options_surface.py::build_rows) always writes
    # every SCALAR_KEYS column, so a gap here is a regression signal, not an
    # expected state — default to None (never fabricate) but warn once per call.
    # Part-B CBOE-OI columns (contracts_liquid, gex, ...) are deliberately NOT in
    # SCALAR_KEYS — they stay silent below, since their absence is the current,
    # expected pre-task-13 state, not a defect.
    missing_scalar = [k for k in SCALAR_KEYS if k not in out.columns]
    for k in missing_scalar:
        out[k] = None
    missing_alias_srcs = sorted({src for src in LEGACY_ALIASES.values() if src in missing_scalar})
    missing_other_scalar_cols = sorted(set(missing_scalar) - set(missing_alias_srcs))
    for alias, src in LEGACY_ALIASES.items():
        out[alias] = out[src]
    out['unusual_flow'] = (pd.to_numeric(out['pc_ratio'], errors='coerce') > 1.5).astype(int)
    for c in ('contracts_liquid', 'gex', 'iv_centroid_delta', 'surface_premium', 'max_pain', 'pcr_oi', 'oi_session'):
        if c not in out.columns:
            out[c] = None
    if missing_alias_srcs or missing_other_scalar_cols:
        log.warning('build_panel: surface frame lacks SCALAR_KEYS column(s) %s (legacy-alias sources, defaulted '
                    'to None) and %s (other scalar columns, defaulted to None) — a real surface master carries '
                    'every SCALAR_KEYS column; investigate the builder', missing_alias_srcs, missing_other_scalar_cols)
    return out


def main():
    t0 = time.time()
    df = load_surface()
    print(f'surface rows: {len(df):,} tickers: {df["ticker"].nunique():,} dates: {df["date"].nunique()}')
    closes = load_closes(set(df['ticker'].unique()), df['date'].min() - pd.Timedelta(days=90))
    panel = build_panel(df, closes)
    tmp = OUT_PATH.with_suffix('.parquet.tmp')
    panel.to_parquet(tmp, index=False)
    os.replace(tmp, OUT_PATH)
    print(f'wrote {OUT_PATH} rows={len(panel):,} in {time.time() - t0:.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
