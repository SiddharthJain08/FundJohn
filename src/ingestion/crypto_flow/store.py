"""Persistence for the crypto flow engine: master parquets + Postgres writers.

Master parquets are append-only (CLAUDE.md invariant). Window readers use
duckdb predicates, never a whole-file pandas read.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
from datetime import date as _date
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from src.data import parquet_store
from src.ingestion.crypto_flow.models import ProviderResult, WhaleEvent

EVENTS_PATH = Path('data/master/crypto_whale_events.parquet')
FLOWS_PATH = Path('data/master/crypto_flows_1h.parquet')
ONCHAIN_DAILY_PATH = Path('data/master/crypto_onchain_daily.parquet')
EVENT_KEY = ('source', 'chain', 'tx_hash', 'out_index')
FLOW_KEY = ('ticker', 'ts_utc')
ONCHAIN_KEY = ('ticker', 'date', 'metric')

_EVENT_COLS = [f.name for f in dataclasses.fields(WhaleEvent)]
_FLOW_COLS = ['ticker', 'ts_utc']


def events_to_frame(events: list[WhaleEvent]) -> pd.DataFrame:
    df = pd.DataFrame([dataclasses.asdict(e) for e in events], columns=_EVENT_COLS)
    df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True).astype('datetime64[ns, UTC]')
    df['amount'] = df['amount'].astype('float64')
    df['amount_usd'] = df['amount_usd'].astype('float64')
    df['out_index'] = df['out_index'].astype('int64')
    return df


def write_events(events: list[WhaleEvent], *, path: Path = EVENTS_PATH) -> int:
    """Insert-only append; returns NEW rows written (approximate under concurrent writers; the parquet is the truth)."""
    if not events:
        return 0
    path = Path(path)
    before = parquet_store.row_count(path)
    after = parquet_store.append_insert_only(path, events_to_frame(events), EVENT_KEY)
    return after - before


def write_flows(rows: pd.DataFrame, *, path: Path = FLOWS_PATH) -> int:
    """Upsert on (ticker, ts_utc); returns total rows."""
    return parquet_store.append_dedup(path, rows, FLOW_KEY, mode='replace')


def write_onchain_daily(rows: pd.DataFrame, *, path: Path = ONCHAIN_DAILY_PATH) -> int:
    """Upsert on (ticker, date, metric); returns total rows."""
    return parquet_store.append_dedup(path, rows, ONCHAIN_KEY, mode='replace')


def _read_window(path: Path, since: datetime, tickers: list[str] | None,
                 cols: list[str]) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=cols)
    sql = 'SELECT * FROM read_parquet(?) WHERE ts_utc >= ?'
    params: list = [str(path), pd.Timestamp(since).tz_convert('UTC') if pd.Timestamp(since).tzinfo
                    else pd.Timestamp(since, tz='UTC')]
    if tickers is not None:
        if not tickers:
            return pd.DataFrame(columns=cols)
        sql += ' AND ticker IN (%s)' % ','.join('?' * len(tickers))
        params += list(tickers)
    sql += ' ORDER BY ts_utc'
    con = duckdb.connect()
    try:
        return con.execute(sql, params).df()
    finally:
        con.close()


def read_events_window(since: datetime, *, path: Path = EVENTS_PATH,
                       tickers: list[str] | None = None) -> pd.DataFrame:
    return _read_window(path, since, tickers, _EVENT_COLS)


def read_flows_window(since: datetime, *, path: Path = FLOWS_PATH,
                      tickers: list[str] | None = None) -> pd.DataFrame:
    return _read_window(path, since, tickers, _FLOW_COLS)


def _sanitize(obj):
    """Replace NaN/+-inf with None recursively (Postgres jsonb rejects them)."""
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if obj is pd.NaT:
        return None
    if isinstance(obj, (datetime, _date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def write_snapshot(conn, ts_utc: datetime, per_ticker: dict[str, dict]) -> int:
    n = 0
    try:
        with conn.cursor() as cur:
            for ticker, features in per_ticker.items():
                payload = json.dumps(_sanitize(features), allow_nan=False)
                cur.execute(
                    'INSERT INTO crypto_flow_snapshot (ticker, ts_utc, features) '
                    'VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING',
                    (ticker, ts_utc, payload))
                n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def write_provider_health(conn, ts_utc: datetime, results: list[ProviderResult]) -> int:
    n = 0
    try:
        with conn.cursor() as cur:
            for r in results:
                cur.execute(
                    'INSERT INTO crypto_flow_provider_health '
                    '(provider, ts_utc, ok, rows, latency_ms, err, skipped) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (r.provider, ts_utc, r.ok, r.rows, r.latency_ms, r.err, r.skipped))
                n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def write_events_live(conn, events: list[WhaleEvent]) -> int:
    """Upsert into the 7-day cache, then prune rows older than 7 days."""
    n = 0
    try:
        with conn.cursor() as cur:
            for e in events:
                cur.execute(
                    'INSERT INTO crypto_whale_events_live '
                    '(event_key, ts_utc, source, chain, symbol, ticker, amount, amount_usd, '
                    'from_owner, to_owner, direction, tx_hash) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
                    'ON CONFLICT (event_key) DO UPDATE SET ts_utc=EXCLUDED.ts_utc, '
                    'amount=EXCLUDED.amount, amount_usd=EXCLUDED.amount_usd, '
                    'from_owner=EXCLUDED.from_owner, to_owner=EXCLUDED.to_owner, '
                    'direction=EXCLUDED.direction',
                    ('|'.join(str(x) for x in e.key()), e.ts_utc, e.source, e.chain, e.symbol,
                     e.ticker, e.amount, e.amount_usd, e.from_owner, e.to_owner, e.direction,
                     e.tx_hash))
                n += 1
            cur.execute("DELETE FROM crypto_whale_events_live WHERE ts_utc < NOW() - INTERVAL '7 days'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n


def get_conn():
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        raise RuntimeError('POSTGRES_URI not set')
    import psycopg2
    return psycopg2.connect(uri)
