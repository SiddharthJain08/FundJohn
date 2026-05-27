#!/usr/bin/env python3
"""run_sod_refresh.py — start-of-day opening-price ingest for the DASHBOARD ONLY.

Fetches today's opening price (dailyBar.o) per universe ticker (~9:35am ET) and
writes a dashboard cache JSON. This is NOT a signal input — the signal engine
uses prior-day EOD plus the ~3pm close[t] proxy. It NEVER writes data/master/*.
Gated on by the close-exec cron (OPENCLAW_CLOSE_EXEC_LIVE=1).
"""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from ingestion.close_proxy_snapshot import fetch_open_prices  # noqa: E402

OUT_DIR = ROOT / 'output' / 'dashboard'


def _universe() -> list[str]:
    import pyarrow.parquet as pq
    p = ROOT / 'data' / 'master' / 'prices.parquet'
    if not p.exists():
        return []
    return sorted(pq.read_table(p, columns=['ticker']).to_pandas()['ticker'].unique().tolist())


def run(_universe_fn=None, _fetch=None, _out_dir=None) -> Path:
    out_dir = _out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    universe = (_universe_fn or _universe)()
    opens = (_fetch or fetch_open_prices)(universe)
    today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
    fpath = out_dir / f'sod_prices_{today}.json'
    fpath.write_text(json.dumps({'date': today, 'opens': opens, 'count': len(opens)}, indent=2))
    print(f'[sod_refresh] wrote {len(opens)} opening prices -> {fpath}')
    return fpath


if __name__ == '__main__':
    run()
