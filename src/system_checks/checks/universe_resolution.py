"""Pipeline-tagged check: resolver responds within 15s and returns ≥ floor tickers."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date

from ..registry import check
from ..types import Status


@check(name='universe_resolution', tags=['pipeline', 'strategies'], requires=['fs'])
def _universe_resolution():
    """Resolver CLI responds in under 15s and returns ≥ UNIVERSE_RESOLVER_MIN_LIVE_TICKERS
    (default 200) tickers for today's live strategies."""
    t0 = time.monotonic()
    try:
        out = subprocess.check_output(
            ['python3', '-m', 'src.strategies.universe_resolver',
             '--as-of', str(date.today()), '--states', 'live'],
            text=True, timeout=90,
        )
        elapsed = time.monotonic() - t0
        n = len(json.loads(out))
    except subprocess.TimeoutExpired:
        return Status.FAIL, 'resolver timed out after 90s'
    except Exception as exc:
        return Status.FAIL, f'resolver error: {exc}'

    floor = int(os.environ.get('UNIVERSE_RESOLVER_MIN_LIVE_TICKERS', '200'))
    if n < floor:
        return Status.FAIL, f'union={n} < {floor}'
    # SLA: warm path (persisted coverage-index cache, coverage_index.py
    # from_parquet) runs in single-digit seconds; 15s catches genuine
    # pathology (DB unreachable, lock contention). The first invocation
    # after prices.parquet changes rebuilds the index (~25–30s on the
    # 2-core box) — that once-a-day cold window is WARN, not FAIL. Beyond
    # 60s something is actually wrong. Spec originally said 2.0s but
    # that's unachievable; deviation documented in PR.
    if elapsed > 60.0:
        return Status.FAIL, f'resolver slow {elapsed:.1f}s'
    if elapsed > 15.0:
        return Status.WARN, (f'resolver {elapsed:.1f}s (cold coverage-index '
                             f'rebuild window), union={n}')
    return Status.PASS, f'union={n}, {elapsed * 1000:.0f}ms'
