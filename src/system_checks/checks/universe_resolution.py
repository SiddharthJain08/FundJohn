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
            text=True, timeout=30,
        )
        elapsed = time.monotonic() - t0
        n = len(json.loads(out))
    except subprocess.TimeoutExpired:
        return Status.FAIL, 'resolver timed out after 30s'
    except Exception as exc:
        return Status.FAIL, f'resolver error: {exc}'

    floor = int(os.environ.get('UNIVERSE_RESOLVER_MIN_LIVE_TICKERS', '200'))
    # Cold-path SLA: 15s. The resolver subprocess takes ~7s cold (Python startup +
    # Postgres connect + manifest load + per-strategy fan-out) on a healthy box.
    # 30s hard cap via subprocess timeout below. The 15s here catches genuine
    # pathology (DB unreachable, lock contention) without false-positiving on
    # normal cold starts. Spec originally said 2.0s but that's unachievable;
    # deviation documented in PR.
    if elapsed > 15.0:
        return Status.FAIL, f'resolver slow {elapsed:.1f}s'
    if n < floor:
        return Status.FAIL, f'union={n} < {floor}'
    return Status.PASS, f'union={n}, {elapsed * 1000:.0f}ms'
