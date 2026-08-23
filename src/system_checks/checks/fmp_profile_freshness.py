"""Strategies-tagged check: data/.cache/fmp_profile.json is present, fresh, populated.

run_ticker_metadata_step.py has read this cache since SP-2 Phase A — it is
the ONLY source of sector / industry / ipoDate in ticker_metadata_snapshots —
but no producer existed until scripts/refresh_fmp_profiles.py (weekly
openclaw-fmp-profiles.timer, 2026-08-23). The consumer's load_json() turns a
missing file into {} so the gap was invisible: 14,254 snapshot rows on
2026-08-21, 0 with a sector. Same advisory shape as options_eligibility_freshness.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..registry import check
from ..types import Status

_CACHE = Path(os.environ.get('FMP_PROFILE_CACHE',
                             '/root/openclaw/data/.cache/fmp_profile.json'))
_MAX_AGE_DAYS = 10   # weekly producer + slack
# ~13.4k active tradable Alpaca equities; FMP has a profile for most listed
# operating companies. Floor well under that so a partial first sweep passes.
_MIN_WITH_SECTOR = int(os.environ.get('FMP_PROFILE_MIN_WITH_SECTOR', '3000'))


@check(name='fmp_profile_freshness', tags=['strategies'], requires=[])
def _fmp_profile_freshness():
    """Advisory: WARN if the profile cache is missing / stale / too thin.
    Never FAILs (the producer is weekly)."""
    if not _CACHE.exists():
        return Status.WARN, f'cache missing: {_CACHE} (openclaw-fmp-profiles not yet run)'
    try:
        data = json.loads(_CACHE.read_text())
    except Exception as e:  # noqa: BLE001
        return Status.WARN, f'cache unreadable: {e}'
    with_sector = sum(1 for v in data.values() if isinstance(v, dict) and v.get('sector'))
    tombstones = sum(1 for v in data.values() if isinstance(v, dict) and v.get('_empty'))
    age_days = (time.time() - _CACHE.stat().st_mtime) / 86400
    if with_sector < _MIN_WITH_SECTOR:
        return Status.WARN, (f'only {with_sector} profiles with a sector '
                             f'(< {_MIN_WITH_SECTOR}); {tombstones} tombstones')
    if age_days > _MAX_AGE_DAYS:
        return Status.WARN, f'stale {age_days:.0f}d (>{_MAX_AGE_DAYS}d), {with_sector} with sector'
    return Status.PASS, f'{with_sector} with sector, {tombstones} tombstones, {age_days:.0f}d old'
