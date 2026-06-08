"""Strategies-tagged check: data/.cache/options_eligibility.json is fresh + populated."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..registry import check
from ..types import Status

_CACHE = Path(os.environ.get(
    'OPTIONS_ELIGIBILITY_CACHE',
    '/root/openclaw/data/.cache/options_eligibility.json'))
_MAX_AGE_DAYS = 10
_MIN_ELIGIBLE = int(os.environ.get('OPTIONS_ELIGIBILITY_MIN_FLOOR', '1000'))


@check(name='options_eligibility_freshness', tags=['strategies'], requires=[])
def _options_eligibility_freshness():
    """Advisory: WARN if the eligibility cache is missing / stale / too small.
    Never FAILs (the producer is weekly + gated)."""
    if not _CACHE.exists():
        return Status.WARN, f'cache missing: {_CACHE} (producer not yet run/enabled)'
    try:
        data = json.loads(_CACHE.read_text())
    except Exception as e:  # noqa: BLE001
        return Status.WARN, f'cache unreadable: {e}'
    n = sum(1 for v in data.values() if v)
    age_days = (time.time() - _CACHE.stat().st_mtime) / 86400
    if n < _MIN_ELIGIBLE:
        return Status.WARN, f'only {n} eligible (< {_MIN_ELIGIBLE})'
    if age_days > _MAX_AGE_DAYS:
        return Status.WARN, f'stale {age_days:.0f}d (>{_MAX_AGE_DAYS}d), {n} eligible'
    return Status.PASS, f'{n} eligible, {age_days:.0f}d old'
