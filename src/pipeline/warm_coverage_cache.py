"""src/pipeline/warm_coverage_cache.py — orchestrator entry point for the
daily ``coverage_warm`` pipeline step.

Lives in ``src/pipeline/`` (not ``scripts/``) so that
``src.execution.pipeline_orchestrator._resolve_script`` discovers it via
its standard ``src/pipeline/<script>.py`` lookup without any orchestrator
code changes.

Why this exists (2026-10-12 incident):
  `CoverageIndex.from_parquet()` persists a (ticker x month) bar-count
  cache (`data/cache/coverage_index_counts.parquet` + `.meta`) keyed on
  the master `prices.parquet`'s mtime/size/quarantine-fingerprint. Every
  `collect` step appends to `prices.parquet`, which invalidates that
  cache. Nothing in the 10am cycle previously rebuilt it, so the cache
  sat stale for days (last successful build: 2026-10-09) and the FIRST
  caller each day — `doctor.py`'s `union_universe_size` check, subprocess
  calling `universe_resolver.py` with a 20s timeout — ate the ~35s cold
  rebuild and timed out every single day (`union=0 (resolver timeout
  (>20s))`), masking the real union-universe size.

  This step runs right after `collect` (when the invalidation happens)
  so the rebuild (~35s and growing with the append-only master parquet)
  happens here, synchronously, on the step's generous 600s default
  timeout — not inside some other caller's tight SLA. By the time
  `doctor.py` / `system_checks` / the resolver's union_universe check run
  later in the day, the cache is warm and loads in milliseconds.

  Advisory only: a failure here never blocks the cycle (exit 0 always)
  — the next real caller just eats one slow rebuild instead of a fast
  cache hit, same as before this step existed.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.strategies.coverage_index import CoverageIndex


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='Run date (unused — cache key is the '
                                    'master parquet mtime/size, not the '
                                    'calendar date); accepted for CLI '
                                    'consistency with other pipeline steps.')
    ap.parse_args()

    t0 = time.monotonic()
    try:
        CoverageIndex.from_parquet(str(ROOT / 'data' / 'master' / 'prices.parquet'))
    except Exception as e:
        print(f'coverage_warm: rebuild failed ({type(e).__name__}: {e}) — '
              f'advisory only, not blocking the cycle', flush=True)
        return 0
    print(f'coverage_warm: cache warm in {time.monotonic() - t0:.1f}s', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
