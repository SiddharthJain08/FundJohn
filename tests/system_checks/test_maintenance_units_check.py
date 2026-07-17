"""Fix 1 — maintenance_runs_recent must point at the LIVE maintenance/audit
units, not the retired weekend-split saturday units.

The two saturday units (openclaw-botjohn-saturday-{maintenance,verify}) were
deliberately retired in the weekend-split migration and their timers are
disabled, so a check that still expects them FALSE-FAILs every run. The
active units are the daily one plus weekend-maintenance-{sat,sun}.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.checks.maintenance import MAINT_UNITS  # noqa: E402


def _unit_names():
    return [u[0] for u in MAINT_UNITS]


def test_weekend_maintenance_units_present():
    names = _unit_names()
    assert 'openclaw-weekend-maintenance-sat.service' in names
    assert 'openclaw-weekend-maintenance-sun.service' in names


def test_daily_maintenance_unit_kept():
    assert 'openclaw-botjohn-maintenance.service' in _unit_names()


def test_retired_saturday_units_absent():
    names = _unit_names()
    assert 'openclaw-botjohn-saturday-maintenance.service' not in names
    assert 'openclaw-botjohn-saturday-verify.service' not in names


def test_maint_units_shape_unchanged():
    """Each entry is still (unit, max_hours, label)."""
    for entry in MAINT_UNITS:
        assert len(entry) == 3
        unit, max_hours, label = entry
        assert isinstance(unit, str) and unit.endswith('.service')
        assert isinstance(max_hours, int) and max_hours > 0
        assert isinstance(label, str) and label
