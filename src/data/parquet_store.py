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
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
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

def _atomic_write(path: Path, df: pd.DataFrame, *, write_page_index: bool = True,
                  row_group_size: int | None = 131072) -> None:
    """Write to path.tmp then os.replace → atomic on POSIX.

    Writes a page index and bounded row groups (131072 rows) so date-sorted
    files prune on row-group min/max. Container-only: data is unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    # Use pyarrow directly to preserve types and compression consistent with existing files.
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, tmp, compression='snappy',
                   write_page_index=write_page_index,
                   row_group_size=row_group_size)
    os.replace(tmp, path)


def _merge_threads() -> int:
    """DuckDB merge threads: env OPENCLAW_PARQUET_MERGE_THREADS, default 1
    (order-preserving). Non-integer or < 1 falls back to 1."""
    raw = os.environ.get('OPENCLAW_PARQUET_MERGE_THREADS', '1')
    try:
        n = int(raw)
    except ValueError:
        n = 1
    if n < 1:
        n = 1
    print(f"[parquet_store] merge threads={n}", file=sys.stderr, flush=True)
    return n


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
        # reindex+concat. Output order = existing rows in file order, then the
        # (same-date) batch -- but ONLY with a single DuckDB thread: COPY at
        # default threads does not preserve insertion order (measured: widest row
        # group 17->34 days over 15 appends), so the merge connection pins threads
        # (OPENCLAW_PARQUET_MERGE_THREADS, default 1). Row-group date pruning then
        # holds once the master is re-sorted by (date, ticker) (A1 script), since
        # daily appends are date-monotone. Ticker-ordering (ORDER BY key_cols) is
        # still avoided: it makes the file ticker-contiguous and widens every row
        # group's date range.
        tmp = Path(str(path) + '.tmp')
        existing_lit = str(path).replace("'", "''")
        out_lit = str(tmp).replace("'", "''")
        keymatch = ' AND '.join(f'e.{c} IS NOT DISTINCT FROM n.{c}' for c in key_cols)
        con = duckdb.connect()
        try:
            con.execute(f"SET memory_limit='{_merge_mem_limit()}'")
            con.execute(f"SET temp_directory='{_merge_spill_dir()}'")
            con.execute(f"SET threads = {_merge_threads()}")
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


# ── Path-or-directory source (per-month options layout) ─────────────────────

_MONTH_GLOB = '[0-9][0-9][0-9][0-9]-[0-9][0-9].parquet'


def _dir_files(path: Path) -> List[Path]:
    return sorted(path.glob(_MONTH_GLOB))


def _has_data(path: Path) -> bool:
    """True when path is a file, or a directory holding at least one month file."""
    if path.is_dir():
        return bool(_dir_files(path))
    return path.exists()


def _sql_source(path: Path) -> str:
    """DuckDB table-function source for a parquet file or a per-month directory."""
    path = Path(path)
    if path.is_dir():
        files = ", ".join("'" + str(f).replace("'", "''") + "'" for f in _dir_files(path))
        return f"read_parquet([{files}], union_by_name=true, hive_partitioning=false)"
    lit = str(path).replace("'", "''")
    return f"read_parquet('{lit}', hive_partitioning=false)"


# ── Read helpers ─────────────────────────────────────────────────────────────

def read_latest_per_ticker(
    path: Path | str,
    cols: Optional[Sequence[str]] = None,
    ticker_col: str = 'ticker',
    date_col: str = 'date',
) -> pd.DataFrame:
    """Return one row per ticker — the row with max(date_col)."""
    path = Path(path)
    if not _has_data(path):
        return pd.DataFrame()
    q_cols = ', '.join(cols) if cols else '*'
    sql = f"""
        SELECT {q_cols} FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY {ticker_col} ORDER BY {date_col} DESC) AS _rn
            FROM {_sql_source(path)}
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
    if not _has_data(path):
        return pd.DataFrame()
    q_cols = ', '.join(cols) if cols else '*'
    sql = f"SELECT {q_cols} FROM {_sql_source(path)}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return duckdb.sql(sql).df()


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_WINDOW_STATS_COLS = ['ticker', 'last_close', 'last_date', 'adv_usd', 'n_rows']


def _window_prep(path, cutoff, upto, need_volume):
    """Validate dates (ISO only: they are interpolated as SQL literals) and the
    footer schema. Returns (quoted_path, where_clause, volume_select) or None
    when the file is missing."""
    for name, v, optional in (('cutoff', cutoff, False), ('upto', upto, True)):
        if v is None and optional:
            continue
        if not isinstance(v, str) or not _ISO_DATE_RE.fullmatch(v):
            raise ValueError(f"{name} must be an ISO date YYYY-MM-DD string, got {v!r}")
    path = Path(path)
    if not path.exists():
        return None
    has_vol = "volume" in pq.read_schema(path).names
    if need_volume and not has_vol:
        raise KeyError("volume")
    vol = "volume" if has_vol else "CAST(NULL AS DOUBLE) AS volume"
    where = f"date >= '{cutoff}'"
    if upto is not None:
        where += f" AND date <= '{upto}'"
    return str(path).replace("'", "''"), where, vol


_TAIL_ROWS_COLS = ['ticker', 'date', 'close', 'volume', 'n_rows']


def ticker_window_tail_rows(
    path: Path | str,
    *,
    cutoff: str,
    upto: Optional[str] = None,
    last_n: int = 20,
) -> pd.DataFrame:
    """
    The <= last_n most recent (ticker, date, close, volume) rows per ticker in
    [cutoff, upto], plus per-ticker n_rows (window count(*)). Returns raw rows so
    callers can run pandas math (identical summation order to a full read).
    Empty frame if the file is missing; KeyError('volume') if it has no volume.
    """
    prep = _window_prep(path, cutoff, upto, True)
    if prep is None:
        return pd.DataFrame(columns=_TAIL_ROWS_COLS)
    qpath, where, vol = prep
    last_n = int(last_n)
    sql = f"""
        WITH w AS (SELECT ticker, date, close, {vol}
                   FROM read_parquet('{qpath}', hive_partitioning=false)
                   WHERE {where}),
             r AS (SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn,
                          count(*) OVER (PARTITION BY ticker) AS n_rows FROM w)
        SELECT ticker, date, close, volume, n_rows FROM r WHERE rn <= {last_n}
        ORDER BY ticker, date
    """
    return duckdb.sql(sql).df()[_TAIL_ROWS_COLS]


def ticker_window_stats(
    path: Path | str,
    *,
    cutoff: str,
    upto: Optional[str] = None,
    last_n: int = 20,
    need_volume: bool = True,
) -> pd.DataFrame:
    """
    Per-ticker windowed aggregate over a prices parquet (DuckDB predicate pushdown).

    One row per ticker with any row in [cutoff, upto]: last non-NaN close, max date,
    n_rows (count(*) in the window, NaN rows included) and adv_usd = mean of
    close*volume over the ticker's last `last_n` window rows (rows counted NaN
    included, NaN products skipped, mirroring pandas tail(n).mean()).
    adv_usd equals legacy tail(last_n) only when n_rows >= last_n.
    Empty frame with the columns above if the file is missing.
    need_volume=True raises KeyError('volume') if the file has no volume column;
    need_volume=False (last_close-only callers) yields NULL adv_usd instead.
    """
    prep = _window_prep(path, cutoff, upto, need_volume)
    if prep is None:
        return pd.DataFrame(columns=_WINDOW_STATS_COLS)
    qpath, where, vol = prep
    last_n = int(last_n)
    sql = f"""
        WITH w AS (SELECT ticker, date, close, {vol}
                   FROM read_parquet('{qpath}', hive_partitioning=false)
                   WHERE {where}),
             r AS (SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn FROM w)
        SELECT ticker,
               arg_max(close, date) FILTER (WHERE close IS NOT NULL AND NOT isnan(close)) AS last_close,
               max(date) AS last_date,
               count(*) AS n_rows,
               avg(close*volume) FILTER (WHERE rn <= {last_n} AND close*volume IS NOT NULL
                                           AND NOT isnan(close*volume)) AS adv_usd
        FROM r GROUP BY ticker
    """
    return duckdb.sql(sql).df()[_WINDOW_STATS_COLS]


def max_date(path: Path | str, date_col: str = 'date') -> Optional[str]:
    """Return ISO date string of the latest row, or None if empty."""
    path = Path(path)
    if not _has_data(path):
        return None
    result = duckdb.sql(f"SELECT MAX({date_col}) AS d FROM {_sql_source(path)}").fetchone()
    if result is None or result[0] is None:
        return None
    d = result[0]
    return d.isoformat() if hasattr(d, 'isoformat') else str(d)


def row_count(path: Path | str) -> int:
    path = Path(path)
    if path.is_dir():
        return sum(pq.read_metadata(f).num_rows for f in _dir_files(path))
    if not path.exists():
        return 0
    return pq.read_metadata(path).num_rows


# ── Named writers (called from Node via parquet_reader.py CLI) ───────────────

PRICES_PATH    = MASTER_DIR / 'prices.parquet'
OPTIONS_PATH   = MASTER_DIR / 'options_eod.parquet'
OPTIONS_DIR    = MASTER_DIR / 'options_eod'
FUNDAMENTALS_PATH = MASTER_DIR / 'financials.parquet'
INSIDER_PATH   = MASTER_DIR / 'insider.parquet'
MACRO_PATH     = MASTER_DIR / 'macro.parquet'

PRICES_KEYS       = ['ticker', 'date']
OPTIONS_KEYS      = ['ticker', 'date', 'expiry', 'strike', 'option_type']
FUNDAMENTALS_KEYS = ['ticker', 'period']
INSIDER_KEYS      = ['ticker', 'filing_date', 'insider_name', 'transaction_type', 'shares']
MACRO_KEYS        = ['date', 'series']
CALENDAR_PATH     = MASTER_DIR / 'trading_calendar.parquet'
CALENDAR_KEYS     = ['date']
SURFACE_PATH      = MASTER_DIR / 'options_surface.parquet'
SURFACE_KEYS      = ['ticker', 'date']


def write_prices(rows: Iterable[dict]) -> int:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return row_count(PRICES_PATH)
    return append_dedup(PRICES_PATH, df, PRICES_KEYS, mode='replace')


def options_monthly_enabled() -> bool:
    """Per-month options layout flag (read at call time)."""
    return os.environ.get('OPENCLAW_OPTIONS_MONTHLY') == '1'


def options_path() -> Path:
    return OPTIONS_DIR if options_monthly_enabled() else OPTIONS_PATH


def options_month_files(root: Path | None = None) -> List[Path]:
    return _dir_files(Path(root) if root is not None else OPTIONS_DIR)


def options_dataset() -> ds.Dataset:
    """The options store as a pyarrow dataset: the single file, or (flag on) the
    month files with a permissively unified schema (all-null month vs double)."""
    if not options_monthly_enabled():
        return ds.dataset(str(OPTIONS_PATH), format='parquet')
    files = options_month_files()
    if not files:
        raise FileNotFoundError(f'no options month files under {OPTIONS_DIR}')
    schema = pa.unify_schemas([pq.read_schema(f) for f in files],
                              promote_options='permissive')
    return ds.dataset([str(f) for f in files], schema=schema, format='parquet')


def options_read_table(columns: Sequence[str], filters=None,
                       read_dictionary: Sequence[str] | None = None) -> pa.Table:
    """Read options columns; filters = DNF list or pc.Expression."""
    columns = list(columns)
    rd = list(read_dictionary) if read_dictionary else None
    if not options_monthly_enabled():
        return pq.read_table(OPTIONS_PATH, columns=columns, filters=filters,
                             read_dictionary=rd)
    if isinstance(filters, list):
        filters = pq.filters_to_expression(filters)
    dataset = options_dataset()
    if rd:
        fmt = ds.ParquetFileFormat(read_options=ds.ParquetReadOptions(dictionary_columns=rd))
        dataset = ds.dataset(list(dataset.files), schema=dataset.schema, format=fmt)
    return dataset.to_table(columns=columns, filter=filters)


def _options_log(mode: str, months: List[str], rows_in: int, total: int) -> None:
    line = (f"[options_store] mode={mode} months={','.join(months) or '-'} "
            f"rows_in={rows_in} total={total}")
    print(line, file=sys.stderr, flush=True)


def append_options(df: pd.DataFrame, key_cols: Sequence[str] = OPTIONS_KEYS,
                   mode: str = 'replace') -> int:
    """Upsert options rows. Flag off: one append_dedup on the single file.
    Flag on: split by month of `date`, append_dedup into each month's file."""
    key_cols = list(key_cols)
    if not options_monthly_enabled():
        total = append_dedup(OPTIONS_PATH, df, key_cols, mode)
        _options_log('file', [], len(df), total)
        return total
    months: List[str] = []
    if not df.empty:
        # Same month rule as split_options_master (substr(CAST(date AS VARCHAR),1,7)).
        keys = df['date'].astype(str).str[:7]
        bad = int((~keys.str.fullmatch(r'\d{4}-\d{2}') | df['date'].isna()).sum())
        if bad:
            raise ValueError(f'[options_store] {bad} rows with NULL/unparseable date')
        # NOTE: a multi-month batch is not atomic across months; a retry is
        # idempotent in replace mode (each month is its own upsert).
        for m, part in df.groupby(keys, sort=True):
            append_dedup(OPTIONS_DIR / f'{m}.parquet', part.reset_index(drop=True),
                         key_cols, mode)
            months.append(str(m))
    total = row_count(OPTIONS_DIR)
    _options_log('monthly', months, len(df), total)
    return total


def write_options(rows: Iterable[dict]) -> int:
    df = pd.DataFrame(list(rows))
    if df.empty:
        return row_count(options_path())
    return append_options(df)


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
