"""SP-7 Phase B Task 4 — universe_tier_coherence probe registration + logic."""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


def test_probe_registered():
    """Probe must be registered in the system_checks registry."""
    import system_checks.checks  # noqa: F401 — side-effect registration
    from system_checks import run_one
    from system_checks.types import CheckResult
    # run_one raises KeyError if the probe is unregistered; returns CheckResult if registered.
    # Without POSTGRES_URI the probe will SKIP (dep unavailable) — that's fine.
    res = run_one('universe_tier_coherence')
    assert isinstance(res, CheckResult), (
        f'Expected CheckResult, got {type(res)}: {res!r}'
    )
    assert res is not None


@pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='no db')
def test_probe_runs_against_live_db():
    """Against live DB the probe must not ERROR (FAIL pre-B0 is expected + correct)."""
    from system_checks import run_one
    from system_checks.types import Status
    res = run_one('universe_tier_coherence')
    # Pre-B0: expected FAIL (ghost rows — mega-caps absent from rank tiers).
    # Post-B0: expected PASS. Either way must not ERROR.
    assert res.status is not Status.ERROR, (
        f'Probe errored (should never happen): {res.detail}\n{res.error}'
    )
