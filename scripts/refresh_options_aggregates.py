#!/usr/bin/env python3
"""Daily forward-fill of the options aggregates panel (wired 2026-08-07).

History: the aggregates collector was retired and the panel froze at
2026-04-22; a one-off manual repair on 2026-07-29 rebuilt April→July-28 and
then froze again because NOTHING scheduled the builders. The backtest's only
options source (aux_data_loader) silently served the frozen last slice to
every later bar until the staleness guard landed (1acb7eb). This runner is the
missing schedule.

Runs both stages, serially, with the same bounded-read builders:
  1. build_options_surface.py --start <T-7> --end <T-1>
     (incremental per-session append to the options_surface master via
      strategies.options_surface.series_frame/features_for_day; rebuilding
      the trailing week daily is idempotent and self-heals short gaps —
      ~1 chunk, a few minutes)
  2. compute_rolling_options_fields.py
     (mandatory FULL rebuild — the 252-day iv_rank window and the rolling
      history lists need full panel history; ~5-10 min, proven to fit RAM)

Invoked by cron-schedule.js daily 06:05 ET Mon-Fri (detached spawn, logged to
logs/options_aggregates_refresh_<date>.log). Manual: python3 scripts/refresh_options_aggregates.py
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(argv: list[str]) -> None:
    print(f"[refresh_options_aggregates] {' '.join(argv)}", flush=True)
    r = subprocess.run([sys.executable, *argv], cwd=ROOT)
    if r.returncode != 0:
        print(f"[refresh_options_aggregates] FAILED rc={r.returncode}: {argv[0]}",
              file=sys.stderr, flush=True)
        sys.exit(r.returncode)


def main() -> int:
    end = date.today() - timedelta(days=1)      # options_eod has through T-1
    start = end - timedelta(days=7)
    _run(['scripts/build_options_surface.py',
          '--start', start.isoformat(), '--end', end.isoformat()])
    _run(['scripts/compute_rolling_options_fields.py'])
    print('[refresh_options_aggregates] done', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
