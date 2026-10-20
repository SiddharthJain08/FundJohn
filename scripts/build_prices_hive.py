#!/usr/bin/env python3
# scripts/build_prices_hive.py
"""Build the hive-partitioned prices copy (prices/year=YYYY/part-0.parquet) from the
single authoritative prices.parquet (stream A, A7). The single file is never modified.

Usage: python3 scripts/build_prices_hive.py [--apply] [--years 2024,2025] [--src P]
       [--out-dir D] [--spill-dir D] [--memory-limit 1GB]
Exit: 0 ok, 2 count mismatch.
Run `build --apply` BEFORE setting OPENCLAW_PRICES_HIVE_READ=1; parity FAILs until built.
Each year's verify+replace holds the same per-year lock as the dual-write.
RUNBOOK: holds the prices.parquet lock up to 600 s while collect's append_dedup times out after 30 s — run only outside 19:00–20:30Z.
Readers flip separately: set OPENCLAW_PRICES_HIVE_READ=1 (OPENCLAW_PRICES_HIVE only gates the dual-write).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / 'src'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import duckdb  # noqa: E402

from src.data.parquet_store import _file_lock, prices_hive_lock_base  # noqa: E402

DEFAULT_SRC = ROOT / 'data' / 'master' / 'prices.parquet'
DEFAULT_OUT = ROOT / 'data' / 'master' / 'prices'
DEFAULT_SPILL = Path('/root/openclaw/data/.duckdb_spill')


def _q(p) -> str:
    return str(p).replace("'", "''")


def build(src: Path, out_dir: Path, *, years: list[int] | None, apply: bool,
          spill_dir: Path, memory_limit: str = '1GB', threads: int = 1,
          row_group_size: int = 122880) -> dict:
    src, out_dir = Path(src), Path(out_dir)
    res = {'years': [], 'rows_src': 0, 'rows_hive': 0, 'applied': False}
    Path(spill_dir).mkdir(parents=True, exist_ok=True)
    s = _q(src)
    with _file_lock(src, timeout=600.0):
        con = duckdb.connect(':memory:')
        try:
            con.execute(f"SET memory_limit='{memory_limit}'")
            con.execute(f"SET temp_directory='{_q(spill_dir)}'")
            con.execute(f"SET threads={int(threads)}")
            con.execute('SET preserve_insertion_order=true')
            avail = [int(r[0]) for r in con.execute(
                f"SELECT DISTINCT year(CAST(date AS DATE)) FROM read_parquet('{s}', "
                f"hive_partitioning=false) ORDER BY 1").fetchall()]
            todo = [y for y in avail if years is None or y in years]
            for y in todo:
                where = (f"date >= '{y}-01-01' AND date < '{y + 1}-01-01'")
                body = (f"SELECT *, CAST(date AS DATE) AS date_d FROM read_parquet('{s}', "
                        f"hive_partitioning=false) WHERE {where}")
                n_src = con.execute(f"SELECT count(*) FROM ({body})").fetchone()[0]
                res['years'].append(y)
                res['rows_src'] += n_src
                if not apply:
                    res['rows_hive'] += n_src
                    continue
                part = out_dir / f'year={y}' / 'part-0.parquet'
                part.parent.mkdir(parents=True, exist_ok=True)
                tmp = Path(str(part) + '.tmp')
                with _file_lock(prices_hive_lock_base(y, out_dir), timeout=600.0):
                    tmp.unlink(missing_ok=True)   # under the lock: a concurrent dual-write owns .tmp
                    try:
                        con.execute(f"COPY ({body} ORDER BY date, ticker) TO '{_q(tmp)}' "
                                    f"(FORMAT PARQUET, COMPRESSION 'snappy', "
                                    f"ROW_GROUP_SIZE {int(row_group_size)})")
                        n_out = con.execute(
                            f"SELECT count(*) FROM read_parquet('{_q(tmp)}', "
                            f"hive_partitioning=false)").fetchone()[0]
                        if n_out != n_src:
                            raise RuntimeError(f'year {y}: rows {n_out} != src {n_src}')
                        os.replace(tmp, part)
                    finally:
                        tmp.unlink(missing_ok=True)
                res['rows_hive'] += n_out
            res['applied'] = bool(apply)
        finally:
            con.close()
    print(f"[build_prices_hive] years={','.join(map(str, res['years'])) or '-'} "
          f"rows_src={res['rows_src']} rows_hive={res['rows_hive']} applied={res['applied']}")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--years', default=None)
    ap.add_argument('--src', default=str(DEFAULT_SRC))
    ap.add_argument('--out-dir', default=str(DEFAULT_OUT))
    ap.add_argument('--spill-dir', default=str(DEFAULT_SPILL))
    ap.add_argument('--memory-limit', default='1GB')
    a = ap.parse_args(argv)
    yrs = [int(x) for x in a.years.split(',')] if a.years else None
    try:
        r = build(Path(a.src), Path(a.out_dir), years=yrs, apply=a.apply,
                  spill_dir=Path(a.spill_dir), memory_limit=a.memory_limit)
    except RuntimeError as e:
        print(f'[build_prices_hive] {e}', file=sys.stderr)
        return 2
    return 0 if r['rows_src'] == r['rows_hive'] else 2


if __name__ == '__main__':
    sys.exit(main())
