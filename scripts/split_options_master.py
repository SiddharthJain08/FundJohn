#!/usr/bin/env python3
"""split_options_master.py — one-off split of options_eod.parquet into the
per-month layout data/master/options_eod/<YYYY-MM>.parquet (A6).

DuckDB streams the source under memory_limit (spilling to --spill-dir); each
month is COPYed to <m>.parquet.tmp, its row count verified against the source,
then os.replace'd. The source is never modified or deleted (append-only
invariant). `--months 2026-10` re-splits only that month (pre-flip refresh).
`--verify` writes nothing: per-month src_rows vs dir_rows, exit 2 on mismatch.
Controller-run; do not run against the 5 GB master from tests.

WARNING: split() holds the SOURCE lock for the whole multi-month scan, and the
source is rescanned once per month (O(months x size)). Run outside the 16:15 ET
collect/flush window and never while a backfill runs. --apply refuses (exit 2)
when OPENCLAW_OPTIONS_MONTHLY=1 (post-flip the month files are the master) or
when an existing month file already has more rows than the source (never shrink).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from src.data.parquet_store import (  # noqa: E402
    MASTER_DIR, _file_lock, options_monthly_enabled)

DEFAULT_SPILL = '/root/openclaw/data/.duckdb_spill'
_MONTH = "substr(CAST(date AS VARCHAR),1,7)"


def _lit(p) -> str:
    return str(p).replace("'", "''")


def _connect(spill_dir: Path, memory_limit: str, threads: int):
    spill_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET temp_directory='{_lit(spill_dir)}'")
    con.execute(f"SET threads={int(threads)}")
    return con


def _src_counts(con, src: Path, months) -> dict:
    rows = con.execute(
        f"SELECT {_MONTH} AS m, count(*) FROM read_parquet('{_lit(src)}', "
        f"hive_partitioning=false) GROUP BY 1 ORDER BY 1").fetchall()
    if any(m is None or len(m) != 7 for m, _ in rows):
        raise SystemExit('[split_options] source has rows with NULL/unparseable date; '
                         'refusing (fix the source first)')
    out = {m: int(n) for m, n in rows}
    if months:
        out = {m: n for m, n in out.items() if m in months}
    return out


def _dir_counts(out_dir: Path, months) -> dict:
    out = {}
    for f in sorted(out_dir.glob('[0-9][0-9][0-9][0-9]-[0-9][0-9].parquet')):
        if not months or f.stem in months:
            out[f.stem] = pq.read_metadata(f).num_rows
    return out


def _report(months, rows_src, rows_dir, mism, applied) -> dict:
    res = {'months': months, 'rows_src': rows_src, 'rows_dir': rows_dir,
           'mismatches': mism, 'applied': applied}
    print(f"[split_options] months={','.join(months) or '-'} rows_src={rows_src} "
          f"rows_dir={rows_dir} mismatches={len(mism)} applied={applied}", flush=True)
    return res


def split(src: Path, out_dir: Path, *, months: list[str] | None, apply: bool,
          spill_dir: Path, memory_limit: str = '1GB', threads: int = 1) -> dict:
    src, out_dir, spill_dir = Path(src), Path(out_dir), Path(spill_dir)
    want = set(months) if months else None
    print('[split_options] WARN holding source lock for the whole scan: run outside '
          'the 16:15 ET collect window, never during a backfill', file=sys.stderr, flush=True)
    if apply and options_monthly_enabled():
        raise SystemExit('[split_options] refusing --apply: OPENCLAW_OPTIONS_MONTHLY=1')
    with _file_lock(src):
        con = _connect(spill_dir, memory_limit, threads)
        try:
            counts = _src_counts(con, src, want)
            if want:
                missing = want - set(counts)
                if missing:
                    raise SystemExit(f"months not in source: {sorted(missing)}")
            mism = []
            if apply:
                out_dir.mkdir(parents=True, exist_ok=True)
                for m, n in counts.items():
                    ex = out_dir / f'{m}.parquet'
                    if ex.exists() and pq.read_metadata(ex).num_rows > n:
                        raise SystemExit(f'[split_options] refusing to shrink {ex.name}: '
                                         f'{pq.read_metadata(ex).num_rows} rows > source {n}')
                for m, n in counts.items():
                    tmp = out_dir / f'{m}.parquet.tmp'
                    con.execute(
                        f"COPY (SELECT * FROM read_parquet('{_lit(src)}', "
                        f"hive_partitioning=false) WHERE {_MONTH}='{m}') "
                        f"TO '{_lit(tmp)}' (FORMAT PARQUET, COMPRESSION 'snappy')")
                    got = pq.read_metadata(tmp).num_rows
                    if got != n:
                        tmp.unlink()
                        mism.append((m, n, got))
                        continue
                    with _file_lock(out_dir / f'{m}.parquet'):
                        os.replace(tmp, out_dir / f'{m}.parquet')
        finally:
            con.close()
    dir_counts = _dir_counts(out_dir, want) if out_dir.exists() else {}
    if not apply:
        mism = [(m, counts.get(m, 0), dir_counts.get(m, 0))
                for m in sorted(set(counts) | set(dir_counts))
                if counts.get(m, 0) != dir_counts.get(m, 0)]
    res = _report(sorted(counts), sum(counts.values()), sum(dir_counts.values()),
                  mism, bool(apply))
    res['per_month'] = {m: (counts.get(m, 0), dir_counts.get(m, 0))
                        for m in sorted(set(counts) | set(dir_counts))}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--src', type=Path, default=MASTER_DIR / 'options_eod.parquet')
    ap.add_argument('--out-dir', type=Path, default=MASTER_DIR / 'options_eod')
    ap.add_argument('--months', nargs='*', default=None)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--spill-dir', type=Path, default=Path(DEFAULT_SPILL))
    ap.add_argument('--memory-limit', default='1GB')
    ap.add_argument('--threads', type=int, default=1)
    a = ap.parse_args(argv)
    res = split(a.src, a.out_dir, months=a.months, apply=a.apply and not a.verify,
                spill_dir=a.spill_dir, memory_limit=a.memory_limit, threads=a.threads)
    for m, (s, d) in res['per_month'].items():
        print(f"  {m} src_rows={s} dir_rows={d}{'' if s == d else '  MISMATCH'}")
    if res['mismatches']:
        raise SystemExit(2)
    return 0


if __name__ == '__main__':
    sys.exit(main())
