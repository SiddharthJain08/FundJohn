#!/usr/bin/env python3
# scripts/prices_hive_parity.py
"""Drift detector: hive-partitioned prices vs the authoritative single file (stream A, A7).
Exit 0 PASS, 1 FAIL."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / 'src'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import duckdb  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

DEFAULT_SRC = ROOT / 'data' / 'master' / 'prices.parquet'
DEFAULT_HIVE = ROOT / 'data' / 'master' / 'prices'


def _q(p) -> str:
    return str(p).replace("'", "''")


def parity(src: Path, hive_dir: Path, *, sample: int = 2000, seed: int = 0,
           memory_limit: str = '1GB') -> dict:
    src, hive_dir = Path(src), Path(hive_dir)
    parts = sorted(hive_dir.glob('*/*.parquet'))
    out = {'rows_file': pq.read_metadata(src).num_rows,
           'rows_hive': sum(pq.read_metadata(p).num_rows for p in parts),
           'dup_keys_hive': -1, 'ticker_maxdate_mismatch': -1,
           'sample_n': 0, 'sample_mismatch': -1, 'verdict': 'FAIL'}
    if parts:
        s, h = _q(src), _q(hive_dir / '*' / '*.parquet')
        hive = f"read_parquet('{h}', hive_partitioning=true)"
        single = f"read_parquet('{s}', hive_partitioning=false)"
        con = duckdb.connect(':memory:')
        try:
            con.execute(f"SET memory_limit='{memory_limit}'")
            con.execute('SET threads=1')
            out['dup_keys_hive'] = con.execute(
                f"SELECT count(*) - count(DISTINCT (ticker, date)) FROM {hive}").fetchone()[0]
            out['ticker_maxdate_mismatch'] = con.execute(
                f"SELECT count(*) FROM (SELECT ticker, max(date) d FROM {single} GROUP BY 1) a "
                f"FULL OUTER JOIN (SELECT ticker, max(date) d FROM {hive} GROUP BY 1) b "
                f"USING (ticker) WHERE a.d IS DISTINCT FROM b.d").fetchone()[0]
            out['sample_n'], out['sample_mismatch'] = con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE h.ticker IS NULL OR "
                f"f.close IS DISTINCT FROM h.close) FROM "
                f"(SELECT ticker, date, close FROM {single} "
                f"USING SAMPLE reservoir({int(sample)} ROWS) REPEATABLE ({int(seed)})) f "
                f"LEFT JOIN {hive} h ON f.ticker = h.ticker AND f.date = h.date").fetchone()
        finally:
            con.close()
    ok = (out['rows_file'] == out['rows_hive'] and out['dup_keys_hive'] == 0
          and out['ticker_maxdate_mismatch'] == 0 and out['sample_mismatch'] == 0)
    out['verdict'] = 'PASS' if ok else 'FAIL'
    print(f"[prices_hive_parity] rows_file={out['rows_file']} rows_hive={out['rows_hive']} "
          f"dup_keys_hive={out['dup_keys_hive']} "
          f"ticker_maxdate_mismatch={out['ticker_maxdate_mismatch']} "
          f"sample_n={out['sample_n']} sample_mismatch={out['sample_mismatch']} "
          f"verdict={out['verdict']}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--src', default=str(DEFAULT_SRC))
    ap.add_argument('--hive-dir', default=str(DEFAULT_HIVE))
    ap.add_argument('--sample', type=int, default=2000)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--memory-limit', default='1GB')
    a = ap.parse_args(argv)
    r = parity(Path(a.src), Path(a.hive_dir), sample=a.sample, seed=a.seed,
               memory_limit=a.memory_limit)
    return 0 if r['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
