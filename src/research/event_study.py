"""Event-study markout (QuestDB HORIZON JOIN model) on the NYSE trading calendar.

``markout(events, prices_wide, offsets)`` ASOF-joins each event onto the session
table, then reads closes at session offsets around that anchor.

Anchor (offset 0) = the last trading session <= ``event_date`` (DuckDB ASOF JOIN).
A Saturday event anchors to Friday. An after-close event dated on a session
anchors to that session, so +1 is the first full session after the release.
``ret(k) = close[s0+k] / close[s0] - 1`` (cumulative from the anchor close;
offset 0 is identically 0). A missing price at the anchor or the target excludes
that observation; duplicate (ticker, anchor) pairs count once; tickers absent
from the panel are excluded, and ``n`` counts only priced observations.

CLI (the ``=`` form is required for negative offsets: argparse treats a bare
``-2:3`` as a flag)::

    python -m research.event_study --master earnings --since 2025-01-01 --offsets=-2:3
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from lib import trading_calendar as tc

_MEM = "SET memory_limit='1GB'"


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute(_MEM)
    con.execute('SET threads=1')
    return con


def _markout_sql(con, events_view: str, prices_view: str, sessions_view: str, offsets) -> pd.DataFrame:
    """events_view(ticker, event_date DATE); prices_view(ticker, date DATE, close);
    sessions_view(d DATE) ascending. Returns [offset, mean_ret, median_ret, n]."""
    offs = [int(o) for o in offsets]
    con.register('_es_offsets', pd.DataFrame({'ord': range(len(offs)), 'k': offs}))
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW _es_sess AS
        SELECT d, row_number() OVER (ORDER BY d) - 1 AS i FROM {sessions_view}""")
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW _es_anchor AS
        SELECT DISTINCT e.ticker, s.i AS ai, s.d AS ad
        FROM (SELECT ticker, CAST(event_date AS DATE) AS ed FROM {events_view}) e
        ASOF JOIN _es_sess s ON e.ed >= s.d
        WHERE e.ed <= (SELECT max(d) FROM _es_sess)""")
    dropped = con.execute(f"""
        SELECT count(*) FROM (SELECT DISTINCT ticker, CAST(event_date AS DATE) AS ed FROM {events_view})
        WHERE ed > (SELECT max(d) FROM _es_sess)""").fetchone()[0]
    df = con.execute(f"""
        WITH obs AS (
            SELECT o.k, pt.close / pa.close - 1 AS ret
            FROM _es_anchor a
            CROSS JOIN _es_offsets o
            JOIN _es_sess st ON st.i = a.ai + o.k
            JOIN {prices_view} pa ON pa.ticker = a.ticker AND pa.date = a.ad
            JOIN {prices_view} pt ON pt.ticker = a.ticker AND pt.date = st.d
            WHERE pa.close IS NOT NULL AND pt.close IS NOT NULL AND pa.close <> 0
        )
        SELECT o.ord, o.k AS "offset", avg(obs.ret) AS mean_ret,
               median(obs.ret) AS median_ret, count(obs.ret) AS n
        FROM _es_offsets o LEFT JOIN obs ON obs.k = o.k
        GROUP BY o.ord, o.k ORDER BY o.ord""").df()
    con.unregister('_es_offsets')
    out = df[['offset', 'mean_ret', 'median_ret', 'n']].copy()
    out['n'] = out['n'].astype('int64')
    out = out.reset_index(drop=True)
    out.attrs['dropped_after_panel'] = int(dropped)
    if dropped:
        print(f'[event_study] dropped_after_panel={dropped}', file=sys.stderr)
    return out


def markout(events: pd.DataFrame, prices_wide: pd.DataFrame, offsets=range(-5, 21)) -> pd.DataFrame:
    """Mean/median cumulative return by session offset around each event."""
    cols = ['offset', 'mean_ret', 'median_ret', 'n']
    offs = list(offsets)
    empty = pd.DataFrame({'offset': offs, 'mean_ret': float('nan'),
                          'median_ret': float('nan'), 'n': 0})[cols]
    if len(events) == 0 or prices_wide.empty:
        return empty
    ev = pd.DataFrame({'ticker': events['ticker'].astype(str).astype(object),
                       'event_date': pd.to_datetime(events['event_date']).dt.normalize()})
    ev = ev[ev['ticker'].isin(set(map(str, prices_wide.columns)))].drop_duplicates()
    if ev.empty:
        return empty
    px = prices_wide.copy()
    px.index = pd.to_datetime(px.index).normalize()
    long = (px[sorted(ev['ticker'].unique())].rename_axis('date').reset_index()
            .melt(id_vars='date', var_name='ticker', value_name='close').dropna(subset=['close']))
    long['ticker'] = long['ticker'].astype(str).astype(object)   # duckdb 1.1 lacks pandas 3 `str`
    sess = pd.DataFrame({'d': pd.to_datetime(tc.sessions(px.index.min(), px.index.max()))})
    con = _connect()
    try:
        con.register('_es_ev', ev)
        con.register('_es_px', long)
        con.register('_es_sessions', sess)
        return _markout_sql(con, '_es_ev', '_es_px', '_es_sessions', offs)
    finally:
        con.close()


def _parse_offsets(s: str) -> range:
    lo, hi = s.split(':')
    return range(int(lo), int(hi) + 1)


def _q(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


_EARNINGS_COLS = ('date', 'report_date', 'earnings_date')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='research.event_study')
    ap.add_argument('--master', required=True, choices=['earnings', 'insider'])
    ap.add_argument('--since', required=True)
    ap.add_argument('--offsets', default='-5:20', help="lo:hi inclusive; use --offsets=-2:3")
    ap.add_argument('--events-path')
    ap.add_argument('--prices-path')
    ap.add_argument('--calendar-path')
    a = ap.parse_args(argv)
    since = pd.Timestamp(a.since).date().isoformat()
    offs = _parse_offsets(a.offsets)

    from data import parquet_store
    ev_path = a.events_path or str(parquet_store.MASTER_DIR / f'{a.master}.parquet')
    px_path = a.prices_path or str(parquet_store.PRICES_PATH)
    if a.calendar_path:
        import os
        os.environ[tc.MASTER_PATH_ENV] = a.calendar_path
        tc.clear_cache()

    con = _connect()
    try:
        cols = [r[0] for r in con.execute('DESCRIBE SELECT * FROM read_parquet(?)', [ev_path]).fetchall()]
        if a.master == 'insider':
            dcol = 'date'
        else:
            dcol = next((c for c in _EARNINGS_COLS if c in cols), None)
        if dcol is None or dcol not in cols:
            print(f'[event_study] no event date column in {ev_path} (cols={cols})', file=sys.stderr)
            return 2
        con.execute(f"""CREATE TEMP VIEW _cli_events AS
            SELECT CAST(ticker AS VARCHAR) AS ticker, CAST("{dcol}" AS DATE) AS event_date
            FROM read_parquet({_q(ev_path)}) WHERE CAST("{dcol}" AS DATE) >= DATE {_q(since)}""")
        n_events = con.execute('SELECT count(*) FROM _cli_events').fetchone()[0]
        con.execute(f"""CREATE TEMP VIEW _cli_prices AS
            SELECT CAST(ticker AS VARCHAR) AS ticker, CAST(date AS DATE) AS date, max(close) AS close
            FROM read_parquet({_q(px_path)})
            WHERE ticker IN (SELECT DISTINCT ticker FROM _cli_events)
              AND CAST(date AS DATE) >= DATE {_q(since)} - INTERVAL 40 DAY
            GROUP BY 1, 2""")
        lo, hi = con.execute('SELECT min(date), max(date) FROM _cli_prices').fetchone()
        sess = pd.DataFrame({'d': pd.to_datetime(tc.sessions(lo, hi) if lo is not None else [])})
        con.register('_cli_sessions', sess)
        res = _markout_sql(con, '_cli_events', '_cli_prices', '_cli_sessions', offs)
    finally:
        con.close()
    print(f'[event_study] master={a.master} events={n_events} offsets={a.offsets}', file=sys.stderr)
    rows = [{'offset': int(r.offset),
             'mean_ret': None if pd.isna(r.mean_ret) else float(r.mean_ret),
             'median_ret': None if pd.isna(r.median_ret) else float(r.median_ret),
             'n': int(r.n)} for r in res.itertuples()]
    print(json.dumps({'master': a.master, 'since': since, 'events': int(n_events), 'rows': rows}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
