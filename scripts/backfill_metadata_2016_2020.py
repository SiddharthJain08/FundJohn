#!/usr/bin/env python3
"""Historical ticker_metadata_snapshots backfill 2016–2020 — ladder campaign W2.

Extends the monthly snapshot history from its 2021-01-31 floor back to
2016-01-31 so point-in-time tier membership (sp500 / tier_r1000 / tier_r3000 /
tier_liquid) resolves for pre-2021 trades — the blocker for shrinking the
full-universe fleet re-backtest down the universe ladder
(docs/runbooks/2026-07-21-universe-ladder-campaign-plan.md §5 W2).

Semantics are IDENTICAL to the canonical builder
(src/pipeline/backfillers/universe_metadata.build_month_snapshot +
src/pipeline/market_cap_lookup.build_market_cap_lookup), whose helpers are
reused directly for membership / alpaca PIT status / ranking / validation.
Only the per-month heavy lifting is replaced by ONE vectorized pass over the
master parquets (the canonical path re-reads all of prices.parquet twice per
month — 120 full reads for this window would thrash the 8GB box):

  * adv_usd_20d  — mean of the last <=20 dollar-volume bars on/before the
                   month-end, >=5 bars required, NO staleness bound
                   (== _adv_usd_20d_batch: tail(20), count>=5).
  * market_cap   — latest EDGAR shares_outstanding row <= month-end ×
                   latest close <= month-end no older than 10 days
                   (== build_market_cap_lookup, PRICE_STALENESS_DAYS=10).
  * in_sp500     — data/sp500_historical_membership_v1.csv point-in-time.
  * in_r1000/3000— top-1000/3000 by market_cap among tradable+active rows
                   (rank_in_r1000_r3000; same pool-limited approximation as
                   the 2021+ snapshots).
  * alpaca PIT   — tickers with listed_date (fallback first_seen_at) after
                   the snapshot are dropped; ~99.3% of pre-2021 TRADED
                   tickers have alpaca rows, so no synthesis is needed.

Rows are append-only (ON CONFLICT DO NOTHING) — existing snapshots are never
touched. Audited per month in backfill_audit.

Usage:
  python3 scripts/backfill_metadata_2016_2020.py [--years 2016,...,2020]
      [--dry-run] [--source-tag backfill_hist_2016_2020_v1]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline.backfillers.universe_metadata import (  # noqa: E402
    SCHEMA_COLUMNS, _alpaca_status_batch, _sp500_membership_on,
    rank_in_r1000_r3000,
)
from scripts.backfill_universe_5y import (  # noqa: E402
    _audit_start, _audit_finish, _enumerate_month_ends, _validate_metadata,
)

PRICES = ROOT / 'data' / 'master' / 'prices.parquet'
SHARES = ROOT / 'data' / 'master' / 'shares_outstanding.parquet'
PRICE_STALENESS_DAYS = 10   # mirrors market_cap_lookup
DEFAULT_TAG = 'backfill_hist_2016_2020_v1'


def month_grids(month_ends: list[date]) -> tuple[pd.DataFrame, ...]:
    """One vectorized pass over prices + shares → per-(month_end, ticker)
    frames: adv20, close, close_date, shares. Each is a DataFrame indexed by
    month-end Timestamp with one column per ticker, forward-filled so a
    month with no bars carries the last known value (matching the canonical
    per-month "latest row on/before" queries)."""
    end_iso = month_ends[-1].isoformat()

    px = pd.read_parquet(PRICES, columns=['ticker', 'date', 'close', 'volume'])
    px = px[px['date'] <= end_iso]
    px = px.sort_values(['ticker', 'date'], ignore_index=True)
    dv = px['close'].astype(float) * px['volume'].astype(float)
    px['adv20'] = (dv.groupby(px['ticker'], sort=False)
                     .rolling(20, min_periods=5).mean()
                     .reset_index(level=0, drop=True))
    px['date_d'] = pd.to_datetime(px['date'])
    px['month'] = px['date_d'].dt.to_period('M')

    last = px.groupby(['ticker', 'month'], sort=False, observed=True).tail(1)
    grid_idx = pd.PeriodIndex([pd.Period(m, 'M') for m in month_ends])

    def _pivot(col):
        p = last.pivot(index='month', columns='ticker', values=col)
        return p.reindex(p.index.union(grid_idx)).sort_index().ffill().reindex(grid_idx)

    adv_g, close_g, cdate_g = _pivot('adv20'), _pivot('close'), _pivot('date_d')

    sh = pd.read_parquet(SHARES, columns=['ticker', 'asof_date', 'shares'])
    sh = sh[sh['asof_date'] <= end_iso].copy()
    sh['month'] = pd.to_datetime(sh['asof_date']).dt.to_period('M')
    sh = sh.sort_values(['ticker', 'asof_date'])
    sh_last = sh.groupby(['ticker', 'month'], sort=False).tail(1)
    shp = sh_last.pivot(index='month', columns='ticker', values='shares')
    shares_g = (shp.reindex(shp.index.union(grid_idx)).sort_index()
                   .ffill().reindex(grid_idx))
    return adv_g, close_g, cdate_g, shares_g


def snapshot_maps(snap: date, adv_g, close_g, cdate_g, shares_g):
    """Extract {ticker: value} maps for one month-end, applying the 10-day
    close-staleness bound for market_cap (ADV has no bound — parity)."""
    p = pd.Period(snap, 'M')
    adv = adv_g.loc[p].dropna()
    close = close_g.loc[p]
    cdate = cdate_g.loc[p]
    fresh = cdate >= pd.Timestamp(snap - timedelta(days=PRICE_STALENESS_DAYS))
    close = close[fresh.fillna(False)].dropna()
    sh_row = shares_g.loc[p].dropna() if p in shares_g.index else pd.Series(dtype=float)
    both = close.index.intersection(sh_row.index)
    caps = {t: float(close[t]) * float(sh_row[t]) for t in both}
    return {t: float(v) for t, v in adv.items()}, caps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', default='2016,2017,2018,2019,2020')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--source-tag', default=DEFAULT_TAG)
    args = ap.parse_args()
    years = sorted({int(y) for y in args.years.split(',') if y.strip()})
    month_ends = _enumerate_month_ends(years)

    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    print(f'[hist-metadata] months={len(month_ends)} '
          f'({month_ends[0]}..{month_ends[-1]}) tag={args.source_tag} '
          f'dry_run={args.dry_run}')

    print('[hist-metadata] building month grids (single parquet pass)…')
    adv_g, close_g, cdate_g, shares_g = month_grids(month_ends)
    universe = sorted(adv_g.columns)
    print(f'[hist-metadata] grid tickers={len(universe)}')

    promoted = quarantined = 0
    for snap in month_ends:
        chunk_key = f'{snap.isoformat()}:metadata'
        adv_map, cap_map = snapshot_maps(snap, adv_g, close_g, cdate_g, shares_g)
        alpaca = _alpaca_status_batch(universe, snap, pg)
        sp500 = _sp500_membership_on(snap)
        present = [s for s in universe if s in alpaca]

        rows = []
        for sym in present:
            a = alpaca[sym]
            rows.append({
                'snapshot_date': snap, 'symbol': sym,
                'asset_class': a.get('asset_class') or 'us_equity',
                'exchange': a.get('exchange'),
                'status': a.get('status') or 'active',
                'tradable': bool(a.get('tradable')),
                'shortable': bool(a.get('shortable')),
                'fractionable': bool(a.get('fractionable')),
                'easy_to_borrow': bool(a.get('easy_to_borrow')),
                'market_cap': cap_map.get(sym),
                'adv_usd_20d': adv_map.get(sym),
                'sector': None, 'industry': None,
                'options_eligible': False,
                'in_sp500': sym in sp500,
                'in_r1000': False, 'in_r3000': False,
                'listed_date': None, 'delisted_date': None,
            })
        df = pd.DataFrame(rows, columns=SCHEMA_COLUMNS)
        if not df.empty:
            r1000, r3000 = rank_in_r1000_r3000(df)
            df['in_r1000'] = df['symbol'].isin(r1000)
            df['in_r3000'] = df['symbol'].isin(r3000)

        audit_id = _audit_start(pg, 'metadata', chunk_key, args.source_tag)
        ok, err = _validate_metadata(df, snap)
        if not ok:
            print(f'  [{chunk_key}] QUARANTINED: {err}')
            _audit_finish(pg, audit_id, status='quarantined', err=err)
            quarantined += 1
            continue
        if args.dry_run:
            n_caps = int(df['market_cap'].notna().sum())
            print(f'  [dry-run] {chunk_key}: rows={len(df)} sp500='
                  f'{int(df.in_sp500.sum())} caps={n_caps} '
                  f'r1000={int(df.in_r1000.sum())} r3000={int(df.in_r3000.sum())}')
            _audit_finish(pg, audit_id, status='validated', rows=len(df))
            continue

        data = [(r.snapshot_date, r.symbol, r.asset_class, r.exchange, r.status,
                 bool(r.tradable), bool(r.shortable), bool(r.fractionable),
                 bool(r.easy_to_borrow), r.market_cap, r.adv_usd_20d,
                 r.sector, r.industry, bool(r.options_eligible),
                 bool(r.in_sp500), bool(r.in_r1000), bool(r.in_r3000),
                 r.listed_date, r.delisted_date, args.source_tag)
                for r in df.itertuples(index=False)]
        try:
            with pg.cursor() as cur:
                execute_values(cur, """
                    INSERT INTO ticker_metadata_snapshots (
                        snapshot_date, symbol, asset_class, exchange, status,
                        tradable, shortable, fractionable, easy_to_borrow,
                        market_cap, adv_usd_20d, sector, industry,
                        options_eligible, in_sp500, in_r1000, in_r3000,
                        listed_date, delisted_date, source_tag)
                    VALUES %s ON CONFLICT (snapshot_date, symbol) DO NOTHING""",
                    data, page_size=1000)
            pg.commit()
        except Exception as e:
            pg.rollback()
            _audit_finish(pg, audit_id, status='failed', err=str(e)[:500])
            print(f'  [{chunk_key}] FAILED: {e}')
            continue
        _audit_finish(pg, audit_id, status='promoted', rows=len(df))
        print(f'  [{chunk_key}] promoted rows={len(df)} '
              f'sp500={int(df.in_sp500.sum())} r3000={int(df.in_r3000.sum())}')
        promoted += 1

    print(f'[hist-metadata] DONE promoted={promoted} quarantined={quarantined}')
    pg.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
