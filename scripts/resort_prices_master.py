#!/usr/bin/env python3
# scripts/resort_prices_master.py
"""One-off: rewrite data/master/prices.parquet sorted by (date, ticker) so
row-group min/max statistics prune on date (stream A, A1).

Container-only change: rows/columns/values are byte-identical in content; the
script proves it (schema, count, per-column hash sums, whole-row hash sum)
before swapping. Safe by construction:
  * holds the master lock for the whole run (parquet_store._file_lock)
  * DuckDB sort is memory-capped and spills to --spill-dir (default is on disk,
    NOT tmpfs /tmp); one DuckDB COPY ... ORDER BY writes the output
  * the output carries no page index (DuckDB COPY does not write one; the
    row-group layout check never depended on it)
  * --apply: hardlink backup, then atomic os.replace -> master never absent
  * tmp is removed on any failure; the backup is never deleted here

Usage:
  python3 scripts/resort_prices_master.py [--dry-run | --apply] [--path P]
      [--spill-dir D] [--memory-limit 2GB] [--row-group-size 122880] [--threads 1]
Exit: 0 verified, 2 on any mismatch (master untouched), 3 dry-run layout FAIL.
RUNBOOK: --apply holds the prices.parquet lock up to 600 s while collect's append_dedup times out after 30 s — run only outside 19:00–20:30Z.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / 'src'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import duckdb  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from src.data.parquet_layout import layout_verdict, rowgroup_span_report  # noqa: E402
from src.data.parquet_store import _file_lock  # noqa: E402

DEFAULT_PATH = ROOT / 'data' / 'master' / 'prices.parquet'
DEFAULT_SPILL = Path('/root/openclaw/data/.duckdb_spill')


def _q(p) -> str:
    return str(p).replace("'", "''")


def _hashes(con, src) -> dict:
    """Order-independent fingerprint of a parquet file's content."""
    s = _q(src)
    schema = [tuple(r[:2]) for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s}', hive_partitioning=false)").fetchall()]
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{s}', hive_partitioning=false)").fetchone()[0]
    colhash = list(con.execute(
        f"SELECT sum(hash(columns(*))::HUGEINT) FROM read_parquet('{s}', hive_partitioning=false)").fetchone())
    rowhash = con.execute(
        f"SELECT sum(hash(t)::HUGEINT) FROM read_parquet('{s}', hive_partitioning=false) t").fetchone()[0]
    return {'schema': schema, 'rows': rows, 'colhash': colhash, 'rowhash': rowhash}


def _write_sorted(con, src: Path, tmp: Path, row_group_size: int) -> None:
    # One DuckDB COPY: the sort is memory-capped and spills (a pyarrow streaming
    # writer could not sort the 19.5 M-row master within the cgroup cap).
    con.execute(
        f"COPY (SELECT * FROM read_parquet('{_q(src)}', hive_partitioning=false) "
        f"ORDER BY date, ticker) TO '{_q(tmp)}' "
        f"(FORMAT PARQUET, COMPRESSION snappy, ROW_GROUP_SIZE {int(row_group_size)})")


def resort(path: Path, *, apply: bool, spill_dir: Path, memory_limit: str = '2GB',
           row_group_size: int = 122880, threads: int = 1, lock_timeout: float = 600.0,
           today: date | None = None) -> dict:
    path = Path(path)
    tmp = Path(str(path) + '.resort.tmp')
    today = today or date.today()
    backup = path.with_name(f"{path.name}.pre-resort-{today:%Y%m%d}")
    out = {'rows_old': 0, 'rows_new': 0, 'colhash_equal': False, 'rowhash_equal': False,
           'schema_equal': False, 'arrow_schema_equal': False, 'report_new': {}, 'verdict_new': 'FAIL', 'backup': None,
           'applied': False}
    with _file_lock(path, timeout=lock_timeout):
        print(f"[resort_prices] lock acquired at {datetime.now().isoformat(timespec='seconds')}")
        if apply and backup.exists():
            raise FileExistsError(f'backup already exists: {backup}')
        Path(spill_dir).mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(':memory:')
        try:
            con.execute(f"SET memory_limit='{memory_limit}'")
            con.execute(f"SET temp_directory='{_q(spill_dir)}'")
            con.execute(f"SET threads={int(threads)}")
            con.execute('SET preserve_insertion_order=true')
            tmp.unlink(missing_ok=True)
            _write_sorted(con, path, tmp, row_group_size)
            old, new = _hashes(con, path), _hashes(con, tmp)
            out['rows_old'], out['rows_new'] = old['rows'], new['rows']
            out['schema_equal'] = old['schema'] == new['schema'] and old['rows'] == new['rows']
            out['arrow_schema_equal'] = pq.read_schema(path).equals(pq.read_schema(tmp))
            out['colhash_equal'] = old['colhash'] == new['colhash']
            out['rowhash_equal'] = old['rowhash'] == new['rowhash']
            out['report_new'] = rowgroup_span_report(tmp)
            out['verdict_new'], detail = layout_verdict(out['report_new'])
            ok = out['schema_equal'] and out['colhash_equal'] and out['rowhash_equal']
            if apply and ok:
                os.link(path, backup)
                os.replace(tmp, path)
                out['applied'], out['backup'] = True, str(backup)
        finally:
            con.close()
            tmp.unlink(missing_ok=True)   # no-op after a successful replace
    r = out['report_new']
    print(f"[resort_prices] rows_old={out['rows_old']} rows={out['rows_new']} "
          f"schema_eq={out['schema_equal']} arrow_schema_eq={out['arrow_schema_equal']} "
          f"colhash_eq={out['colhash_equal']} rowhash_eq={out['rowhash_equal']} "
          f"rg={r.get('n_row_groups')} frac_tight={r.get('frac_tight', 0):.3f} "
          f"monotone={r.get('monotone')} page_index=False "
          f"verdict={out['verdict_new']} applied={out['applied']} backup={out['backup']}")
    if out['verdict_new'] == 'FAIL':
        print('[resort_prices] WARN layout verdict FAIL - rerun with a smaller --row-group-size')
    if out['applied']:
        print(f"remove {backup} after 2026-10-27 if prices_rowgroup_span is PASS "
              f"and the 15:00 ET compute is clean")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true', help='default')
    g.add_argument('--apply', action='store_true')
    ap.add_argument('--path', default=str(DEFAULT_PATH))
    ap.add_argument('--spill-dir', default=str(DEFAULT_SPILL))
    ap.add_argument('--memory-limit', default='2GB')
    ap.add_argument('--row-group-size', type=int, default=122880)
    ap.add_argument('--threads', type=int, default=1)
    a = ap.parse_args(argv)
    r = resort(Path(a.path), apply=a.apply, spill_dir=Path(a.spill_dir),
               memory_limit=a.memory_limit, row_group_size=a.row_group_size, threads=a.threads)
    if not (r['schema_equal'] and r['colhash_equal'] and r['rowhash_equal']):
        print('[resort_prices] VERIFICATION MISMATCH - master untouched', file=sys.stderr)
        raise SystemExit(2)
    if r['verdict_new'] == 'FAIL' and not a.apply:
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
