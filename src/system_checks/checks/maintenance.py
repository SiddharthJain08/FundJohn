"""Maintenance-cycle freshness checks.

Defense-in-depth for the 2026-05-13..18 silent-maintenance incident: a
SyntaxError in run_maintenance.js killed the daily / weekend maintenance
runs for 5 days without anyone noticing. With OnFailure notifiers wired
(see docs/onfailure-dropins/) failures now reach Discord, but a unit that
*never starts* (timer disabled, masked, paused) would still be invisible.

These checks ask systemd directly: "when did the unit last run, and was
its last result a success?" If the answer is out of band, escalate.

Unit set note: the weekend-split migration retired the old
openclaw-botjohn-saturday-{maintenance,verify} units (their timers are now
disabled). The live maintenance/audit units are the daily one plus the two
weekend-maintenance-{sat,sun} units, so those are what we watch here.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from ..registry import check
from ..types import Status


# Unit → (max age in hours, label). Tuned to give one full cadence of
# tolerance: daily=26h covers a 2h overrun; the weekly weekend runs get
# 8d (8*24h) to cover a missed weekend plus 1d slack.
MAINT_UNITS = [
    ('openclaw-botjohn-maintenance.service',       26,     'daily 12:00 ET'),
    ('openclaw-weekend-maintenance-sat.service',   8 * 24, '~Sat 20:00 ET'),
    ('openclaw-weekend-maintenance-sun.service',   8 * 24, '~Sun 20:00 ET'),
]


def _show_unit(unit: str) -> dict:
    """Return parsed `systemctl show` properties, or {} on failure."""
    try:
        out = subprocess.run(
            ['systemctl', 'show', unit,
             '-p', 'Result', '-p', 'ActiveEnterTimestamp',
             '-p', 'InactiveEnterTimestamp', '-p', 'ExecMainStatus',
             '-p', 'UnitFileState'],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return dict(
            line.split('=', 1) for line in out.strip().splitlines() if '=' in line
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}


def _parse_ts(s: str) -> datetime | None:
    """systemd timestamps look like 'Sat 2026-05-16 20:00:14 UTC'. Empty
    string when the unit has never run."""
    if not s or s == '0' or s == 'n/a':
        return None
    # Drop the leading 'Sat ' / 'Sun ' etc. — strptime can't handle locale
    # day abbreviations reliably.
    parts = s.split()
    if len(parts) >= 4 and parts[0].endswith(',') is False and len(parts[0]) == 3:
        parts = parts[1:]
    try:
        return datetime.strptime(' '.join(parts), '%Y-%m-%d %H:%M:%S %Z').replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


@check(name='maintenance_runs_recent', tags=['ops'], requires=['systemd'])
def _maintenance_runs_recent():
    """Each maintenance unit must have completed successfully inside its
    expected cadence. Catches: timer disabled, unit masked, script that
    starts but produces no output, or simply never fired."""
    failures: list[str] = []
    warns: list[str] = []
    now = datetime.now(timezone.utc)

    for unit, max_hours, label in MAINT_UNITS:
        st = _show_unit(unit)
        if not st:
            failures.append(f'{unit}: systemctl show unavailable')
            continue

        # For timer-driven oneshots, the SERVICE's UnitFileState is normally
        # 'disabled' or 'static' — the TIMER is what's enabled. Check the
        # paired .timer's UnitFileState instead.
        timer_name = unit.replace('.service', '.timer')
        timer_st = _show_unit(timer_name)
        timer_ufs = timer_st.get('UnitFileState', '')
        if timer_ufs in {'masked', 'disabled'}:
            failures.append(f'{timer_name}: UnitFileState={timer_ufs}')
            continue

        # For Type=oneshot units, ActiveEnterTimestamp is empty when the
        # most recent run FAILED at module-load (never reached `active`).
        # Fall back to InactiveEnterTimestamp — when the run finished, pass
        # or fail — paired with Result to know how it ended.
        ts_active   = _parse_ts(st.get('ActiveEnterTimestamp', ''))
        ts_inactive = _parse_ts(st.get('InactiveEnterTimestamp', ''))
        ts = ts_active or ts_inactive
        if ts is None:
            warns.append(f'{unit}: never run')
            continue

        age_h = (now - ts).total_seconds() / 3600
        result = st.get('Result', 'unknown')
        if result != 'success':
            failures.append(f'{unit}: Result={result} (last finish {age_h:.1f}h ago)')
            continue
        if age_h > max_hours:
            failures.append(
                f'{unit} ({label}): last success {age_h:.1f}h ago > {max_hours}h'
            )

    if failures:
        return Status.FAIL, '; '.join(failures)
    if warns:
        return Status.WARN, '; '.join(warns)
    return Status.PASS, f'{len(MAINT_UNITS)}/{len(MAINT_UNITS)} units fresh'
