"""
parquet_store.py — canonical read/write layer for OpenClaw master parquets.

Replaces the DB-staging-then-sync pattern (sync_master_parquets.py) with
direct parquet writes from the collector. Used by:
  - src/pipeline/store.js (via parquet_store.js → parquet_reader.py CLI)
  - src/ingestion/fetch_vol_indices.py (direct Python import)
  - src/channels/api/server.js (via parquet_store.js)
  - src/execution/engine.py (reads via pandas/pyarrow, no change needed)

Concurrency: per-file fcntl.flock on {path}.lock sidecar serializes writers.
Atomicity: write to {path}.tmp + os.replace ensures readers never see partial state.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent.parent
MASTER_DIR = ROOT / 'data' / 'master'


# ── Locking ──────────────────────────────────────────────────────────────────

@contextmanager
def _file_lock(path: Path, timeout: float = 30.0):
    """Advisory exclusive lock on {path}.lock. Raises TimeoutError on contention."""
    lock_path = Path(str(path) + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, 'w')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f'parquet lock contended >{timeout}s: {path}')
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


# ── Write helpers ────────────────────────────────────────────────────────────

def _atomic_write(path: Path, df: pd.DataFrame) -> None:
    """Write to path.tmp then os.replace → atomic on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    # Use pyarrow directly to preserve types and compression consistent with existing files.
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, tmp, compression='snappy')
    os.replace(tmp, path)


def _merge_mem_limit() -> str:
    """DuckDB memory cap for the streaming merge (spills to disk beyond it)."""
    return os.environ.get('OPENCLAW_PARQUET_MERGE_MEM_LIMIT', '1GB')


def _merge_spill_dir() -> str:
    """Disk-spill directory for the streaming merge. Setting temp_directory on the
    in-memory connection is what lets DuckDB spill instead of OOMing under the cap."""
    return os.environ.get('OPENCLAW_PARQUET_SPILL_DIR', tempfile.gettempdir())


def append_dedup(
    path: Path | str,
    new_df: pd.DataFrame,
    key_cols: Sequence[str],
    mode: str = 'replace',
) -> int:
    """
    Append new_df to the parquet at path, deduplicating on key_cols.

    mode='replace'    — new row wins on key conflict (UPSERT semantics).
    mode='keep-first' — existing row wins on key conflict (INSERT IF NOT EXISTS).

    Returns the total row count after the write.

    Memory-bounded: the existing master parquet is scanned from disk by DuckDB
    (bounded by OPENCLAW_PARQUET_MERGE_MEM_LIMIT, spilling to disk) rather than
    loaded whole into pandas. This is the fix for the 2026-06-22/23 EOD OOM, where
    a `pd.read_parquet` of the 5.9M-row options_eod.parquet ballooned to ~4GB and
    was OOM-killed (rc=137). Only the small incoming batch (new_df) lives in pandas.
    """
    path = Path(path)
    if new_df.empty:
        if path.exists():
            return pq.read_metadata(path).num_rows
        return 0

    keep = 'last' if mode == 'replace' else 'first'
    key_cols = list(key_cols)
    # Pre-dedup the (small) incoming batch with the same keep semantics and input
    # order pandas would use. Existing master rows are already key-unique (every
    # write goes through this function), so after this the only remaining conflicts
    # are existing-vs-batch, resolved by `mode` in the merge below.
    new_df = new_df.drop_duplicates(subset=key_cols, keep=keep)

    with _file_lock(path):
        if not path.exists():
            _atomic_write(path, new_df)
            return len(new_df)

        # NULL-safe key match (mirrors pandas treating NaN==NaN as a duplicate);
        # UNION ALL BY NAME reproduces the column-union-with-NULL-fill of the old
        # reindex+concat. Insertion order is preserved (DuckDB default): existing rows
        # stream out in their current file order, then the (same-date) batch appends —
        # identical physical locality to the old pandas concat, so the row-group
        # min/max date pruning the sliced pyarrow reads rely on is unchanged. (An
        # ORDER BY key_cols was measured to make the file ticker-contiguous, which
        # widened every row group's date range and broke date pruning — avoided.)
        tmp = Path(str(path) + '.tmp')
        existing_lit = str(path).replace("'", "''")
        out_lit = str(tmp).replace("'", "''")
        keymatch = ' AND '.join(f'e.{c} IS NOT DISTINCT FROM n.{c}' for c in key_cols)
        con = duckdb.connect()
        try:
            con.execute(f"SET memory_limit='{_merge_mem_limit()}'")
            con.execute(f"SET temp_directory='{_merge_spill_dir()}'")
            # Register the batch as an Arrow table (reliable dtype mapping; the bare
            # pandas replacement scan can choke on object columns) — same pyarrow
            # conversion the master file itself was written through.
            con.register('n', pa.Table.from_pandas(new_df, preserve_index=False))
            if mode == 'replace':
                # batch wins: existing rows with no batch-key match, then all batch rows.
                merge = (
                    f"SELECT e.* FROM read_parquet('{existing_lit}') e "
                    f"WHERE NOT EXISTS (SELECT 1 FROM n WHERE {keymatch}) "
                    f"UNION ALL BY NAME SELECT * FROM n"
                )
            else:  # keep-first: existing wins on conflict.
                merge = (
                    f"SELECT * FROM read_parquet('{existing_lit}') "
                    f"UNION ALL BY NAME "
                    f"SELECT n.* FROM n WHERE NOT EXISTS "
                    f"(SELECT 1 FROM read_parquet('{existing_lit}') e WHERE {keymatch})"
                )
            cnt = con.execute(
                f"COPY ({merge}) TO '{out_lit}' (FORMAT PARQUET, COMPRESSION 'snappy')"
            ).fetchone()[0]
        finally:
            con.close()
        os.replace(tmp, path)
        return int(cnt)


def append_insert_only(path: Path | str, new_df: pd.DataFrame, key_cols: Sequence[str]) -> int:
    """ON CONFLICT DO NOTHING — only inserts rows whose key tuple is not already present."""
    return append_dedup(path, new_df, key_cols, mode='keep-first')


# ── Read helpers ─────────────────────────────────────────────────────────────

def read_latest_per_ticker(
    path: Path | str,
    cols: Optional[Sequence[str]] = None,
    ticker_col: str = 'ticker',
    date_col: str = 'date',
) -> pd.DataFrame:
    """Return one row per ticker — the row with max(date_col)."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    q_cols = ', '.join(cols) if cols else '*'
    sql = f"""
        SELECT {q_cols} FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY {ticker_col} ORDER BY {date_col} DESC) AS _rn
            FROM read_parquet('{path}')
        ) WHERE _rn = 1
    """
    return duckdb.sql(sql).df().drop(columns=['_rn'], errors='ignore')


def read_filtered(
    path: Path | str,
    where: Optional[str] = None,
    cols: Optional[Sequence[str]] = None,
    order_by: Optional[str] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """
    DuckDB predicate-pushdown read. `where` is a SQL WHERE clause without the keyword.
    Example: read_filtered('prices.parquet', where="ticker='AAPL' AND date >= '2026-01-01'")
    """
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    q_cols = ', '.join(cols) if cols else '*'
    sql = f"SELECT {q_cols} FROM read_parquet('{path}')"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return duckdb.sql(sql).df()


def max_date(path: Path | str, date_col: str = 'date') -> Optional[str]:
    """Return ISO date string of the latest row, or None if empty."""
    path = Path(path)
    if not path.exists():
        return None
    result = duckdb.sql(f"SELECT MAX({date_col}) AS d FROM read_parquet('{path}')").fetchone()
    if result is None or result[0] is None:
        return None
    d = result[0]
    return d.isoformat() if hasattr(d, 'isoformat') else str(d)


def row_count(path: Path | str) -> int:
    path = Path(path)
    if not path.exists():
        return 0
    return pq.read_metadata(path).num_rows


# ── Named writers (called from Node via parquet_reader.py CLI) ───────────────

PRICES_PATH    = MASTER_DIR / 'prices.parquet'
OPTIONS_PATH   = MASTER_DIR / 'options_eod.parquet'
FUNDAMENTALS_PATH = MASTER_DIR / 'financials.parquet'
INSIDER_PATH   = MASTER_DIR / 'insider.parquet'
MACRO_PATH     = MASTER_DIR / 'macro.parquet'

PRICES_KEYS       = ['ticker', 'date']
OPTIONS_KEYS      = ['ticker', 'date', 'expiry', 'strike', 'option_type']
FUNDAMENTALS_KEYS = ['ticker', 'period']
INSIDER_KEYS      = ['ticker', 'filing_date', 'insider_name', 'transaction_type', 'shares']
MACRO_KEYS        = ['date', 'series']


def write_prices(rows: Iterable[dict]) -> int:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return row_count(PRICES_PATH)
    return append_dedup(PRICES_PATH, df, PRICES_KEYS, mode='replace')


def write_options(rows: Iterable[dict]) -> int:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return row_count(OPTIONS_PATH)
    return append_dedup(OPTIONS_PATH, df, OPTIONS_KEYS, mode='replace')


def write_fundamentals(rows: Iterable[dict]) -> int:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return row_count(FUNDAMENTALS_PATH)
    return append_dedup(FUNDAMENTALS_PATH, df, FUNDAMENTALS_KEYS, mode='replace')


def write_insider(rows: Iterable[dict]) -> int:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return row_count(INSIDER_PATH)
    return append_insert_only(INSIDER_PATH, df, INSIDER_KEYS)


def write_macro(rows: Iterable[dict]) -> int:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return row_count(MACRO_PATH)
    return append_dedup(MACRO_PATH, df, MACRO_KEYS, mode='replace')
