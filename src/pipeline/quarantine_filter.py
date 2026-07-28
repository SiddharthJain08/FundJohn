"""Read-time filter for master-parquet consumers.

The append-only invariant means bad rows cannot be DELETEd. This module
loads data_quarantine rows, caches them, and lets consumers drop affected
(symbol, date) pairs at read time.

Also exposes a CLI for JS callers (collector.js) to bootstrap the
quarantine set at startup.

Design notes
------------
- Cache key is the ``master_table`` string only; the cached value is a
  set of ``(symbol, date_str)`` tuples loaded from
  ``data_quarantine WHERE master_table = %s AND superseded_at IS NULL``.
- TTL is 5 minutes (SP-2 Phase B design §3.1). The cache is *additive
  only* across requests with respect to fresh DB inserts — consumers
  that need the quarantine set to reflect a brand-new insert immediately
  must call ``invalidate_cache()``.
- The DB ``symbol`` column is opaque text; it can hold equity tickers
  (prices.parquet) or OCC contract symbols (options_eod.parquet).
- DataFrames coming from different master parquets use different key
  column names. Empirically (verified via Task 1) every master parquet
  on the codebase uses ``ticker``, but several in-memory frames inside
  signal / sentiment pipelines still use ``symbol``. We accept either
  and raise an informative error if the frame has neither.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Tuple

import pandas as pd
import psycopg2

# Module-level state. All access is guarded by ``_LOCK``.
_CACHE: dict[str, tuple[float, set[Tuple[str, str]]]] = {}
_LOCK = threading.Lock()
_TTL = 300  # 5 minutes per SP-2 Phase B design §3.1

# Column candidates checked, in priority order. Both are treated
# symmetrically — first hit wins, but the error message names both.
_KEY_COL_CANDIDATES = ("symbol", "ticker")


def _load(master_table: str) -> set[Tuple[str, str]]:
    """Fetch the unsuperseded quarantine set for ``master_table`` from PG.

    Returns a set of ``(symbol, ISO-date-str)`` tuples. The date is
    cast to text in SQL so comparisons against DataFrame columns
    (which are typically pre-stringified) are direct.
    """
    pg = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, affected_date::TEXT
                FROM data_quarantine
                WHERE master_table = %s AND superseded_at IS NULL
                """,
                (master_table,),
            )
            return {(s, d) for s, d in cur.fetchall()}
    finally:
        pg.close()


def _cached(master_table: str) -> set[Tuple[str, str]]:
    """Return the cached quarantine set for ``master_table``, reloading
    if missing or older than ``_TTL``.

    The DB load happens *inside* ``_LOCK`` so concurrent callers see
    exactly one load per cache miss. Moving the load outside the lock
    (the obvious-looking refactor) breaks the thread-safety contract.
    """
    with _LOCK:
        entry = _CACHE.get(master_table)
        if entry is not None and (time.time() - entry[0]) < _TTL:
            return entry[1]
        rows = _load(master_table)
        _CACHE[master_table] = (time.time(), rows)
        return rows


def _pick_key_column(df: pd.DataFrame) -> str:
    """Return whichever of ``symbol`` / ``ticker`` is present on ``df``.

    Raises ``ValueError`` with both candidates and the actual columns
    listed when neither is present. Per SP-2 Phase B task: the filter
    must be loud about an unfilterable frame, not silently pass it
    through (the plan's stub had the opposite behavior — see task
    docstring for the rationale).
    """
    for cand in _KEY_COL_CANDIDATES:
        if cand in df.columns:
            return cand
    raise ValueError(
        "quarantine_filter: DataFrame missing required key column "
        f"(expected one of {list(_KEY_COL_CANDIDATES)}); "
        f"got columns={df.columns.tolist()}"
    )


def filter_quarantined(df: pd.DataFrame, master_table: str) -> pd.DataFrame:
    """Drop rows whose ``(<keycol>, date)`` is in ``data_quarantine``.

    Parameters
    ----------
    df
        Input frame. Must have a ``date`` column AND either a
        ``symbol`` or ``ticker`` column (raises if neither).
    master_table
        The ``master_table`` value used in ``data_quarantine`` (e.g.
        ``'prices.parquet'``).

    Returns
    -------
    pd.DataFrame
        Frame with unsuperseded quarantined ``(symbol, date)`` rows
        dropped. Index is reset.
    """
    bad = _cached(master_table)
    if not bad:
        # Hot path: most invocations return here. Keep allocation-free.
        return df
    if df.empty:
        return df
    key_col = _pick_key_column(df)
    if "date" not in df.columns:
        raise ValueError(
            "quarantine_filter: DataFrame missing 'date' column; "
            f"got columns={df.columns.tolist()}"
        )
    # Categorical fast path (prices.parquet panel load: 18.8M rows, both key
    # columns categorical from the dictionary read). The generic path below
    # materialises two 18.8M-element python string lists PLUS 18.8M tuples —
    # a ~3.3GB transient measured at 4.3GB whole-process peak on the 8GB box
    # (the 07-16 "1.15GB whole-load peak" was measured while data_quarantine
    # was still empty, i.e. before this branch was ever reached). Matching on
    # category CODES instead is byte-identical (categories ARE the astype(str)
    # values) at ~200MB transient.
    if (isinstance(df[key_col].dtype, pd.CategoricalDtype)
            and isinstance(df["date"].dtype, pd.CategoricalDtype)):
        import numpy as np
        kmap = {str(c): i for i, c in enumerate(df[key_col].cat.categories)}
        dmap = {str(c): i for i, c in enumerate(df["date"].cat.categories)}
        n_dates = len(dmap)
        bad_combined = np.array(
            sorted(kmap[s] * n_dates + dmap[d]
                   for s, d in bad if s in kmap and d in dmap),
            dtype="int64",
        )
        if bad_combined.size == 0:
            return df
        combined = (df[key_col].cat.codes.to_numpy().astype("int64") * n_dates
                    + df["date"].cat.codes.to_numpy())
        keep = ~np.isin(combined, bad_combined)
        if keep.all():
            return df
        return df.loc[keep].reset_index(drop=True)
    # Stringify both sides so the comparison is total.
    keys = list(
        zip(df[key_col].astype(str).tolist(), df["date"].astype(str).tolist())
    )
    mask = [k not in bad for k in keys]
    return df.loc[mask].reset_index(drop=True)


def invalidate_cache(master_table: str | None = None) -> None:
    """Clear the cache for one ``master_table`` (or all if ``None``).

    Consumers that have just INSERTed a new quarantine row and want
    subsequent ``filter_quarantined`` calls to honor it MUST call
    ``invalidate_cache(master_table)`` first; otherwise they may see
    stale data for up to ``_TTL`` seconds.
    """
    with _LOCK:
        if master_table is None:
            _CACHE.clear()
        else:
            _CACHE.pop(master_table, None)


# ---------------------------------------------------------------------------
# CLI — for Node callers (collector.js) that need to bootstrap the
# quarantine set at startup. See SP-2 Phase B plan Task 5 step 4.
# ---------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        prog="python3 -m src.pipeline.quarantine_filter",
        description=(
            "Emit the unsuperseded data_quarantine set for one "
            "master_table. Used by collector.js to load the in-process "
            "quarantine map at startup."
        ),
    )
    ap.add_argument("--master-table", required=True,
                    help="e.g. prices.parquet")
    ap.add_argument("--json", action="store_true",
                    help="emit a JSON array of [symbol, date] pairs "
                         "(default: TSV)")
    args = ap.parse_args(argv)

    rows = sorted(_cached(args.master_table))
    if args.json:
        sys.stdout.write(json.dumps([list(r) for r in rows]))
        sys.stdout.write("\n")
    else:
        for s, d in rows:
            sys.stdout.write(f"{s}\t{d}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(_main())
