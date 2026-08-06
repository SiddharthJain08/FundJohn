"""§8 (2026-08-06 spec): signal target-date mode — positive flag with alias.

OPENCLAW_SAMEDAY_SIGNAL_TARGET=1 must mean the LIVE same-day mode; when the
new flag is unset the legacy OPENCLAW_EOD_SIGNAL_REGISTER spelling keeps its
exact old semantics (that is the whole point of the alias epoch — every
pre-existing test that sets only the legacy flag stays green). A
contradictory half-migrated environment must FAIL doctor, not resolve
silently.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import signal_target_mode as stm  # noqa: E402


def _env(new=None, legacy=None):
    e = {}
    if new is not None:
        e[stm.NEW_FLAG] = new
    if legacy is not None:
        e[stm.LEGACY_FLAG] = legacy
    return e


@pytest.mark.parametrize('new,legacy,sameday', [
    # New flag set → it wins, regardless of legacy.
    ('1', None, True), ('0', None, False),
    ('1', '0', True), ('1', '1', True), ('0', '1', False), ('0', '0', False),
    # New flag unset → exact legacy semantics (register=1 ⇒ T+1).
    (None, '1', False), (None, '0', True), (None, None, True),
    # Empty string counts as unset.
    ('', '1', False), ('', None, True),
])
def test_resolution_matrix(new, legacy, sameday):
    with patch.dict(os.environ, _env(new, legacy), clear=False):
        for k in (stm.NEW_FLAG, stm.LEGACY_FLAG):
            if _env(new, legacy).get(k) is None:
                os.environ.pop(k, None)
        assert stm.sameday_signal_target_on() is sameday
        assert stm.eod_register_on() is (not sameday)


@pytest.mark.parametrize('new,legacy,conflict', [
    ('1', '1', True),   # sameday vs T+1 — contradiction
    ('0', '0', True),   # T+1 vs sameday — contradiction
    ('1', '0', False), ('0', '1', False),   # agreement
    ('1', None, False), (None, '1', False), (None, None, False),
])
def test_alias_conflict_detection(new, legacy, conflict):
    with patch.dict(os.environ, _env(new, legacy), clear=False):
        for k in (stm.NEW_FLAG, stm.LEGACY_FLAG):
            if _env(new, legacy).get(k) is None:
                os.environ.pop(k, None)
        assert (stm.legacy_alias_conflict() is not None) is conflict


def test_engine_gate_delegates():
    from execution import engine
    with patch.dict(os.environ, {stm.NEW_FLAG: '0'}, clear=False):
        assert engine._eod_signal_register_gate_on() is True
    with patch.dict(os.environ, {stm.NEW_FLAG: '1'}, clear=False):
        assert engine._eod_signal_register_gate_on() is False
    # Legacy-only environments keep their old meaning.
    with patch.dict(os.environ, {stm.LEGACY_FLAG: '1'}, clear=False):
        os.environ.pop(stm.NEW_FLAG, None)
        assert engine._eod_signal_register_gate_on() is True


def test_doctor_fails_on_half_migrated_env():
    from maintenance import doctor
    with patch.dict(os.environ, {stm.NEW_FLAG: '1', stm.LEGACY_FLAG: '1'},
                    clear=False):
        res = doctor.check_eod_mutual_exclusion()
    assert res['severity'] == 'fail'
    assert 'contradicts' in res['detail']


def test_doctor_names_sameday_flow():
    with patch.dict(os.environ, {stm.NEW_FLAG: '1', stm.LEGACY_FLAG: '0',
                                 'OPENCLAW_CLOSE_EXEC_LIVE': '0'}, clear=False):
        from maintenance import doctor
        res = doctor.check_eod_mutual_exclusion()
    assert res['severity'] == 'pass'
    assert 'sameday_signal_target' in res['detail']


def test_js_twin_carries_the_alias():
    src = (ROOT / 'src' / 'engine' / 'cron-schedule.js').read_text()
    assert 'OPENCLAW_SAMEDAY_SIGNAL_TARGET' in src, \
        'cron-schedule.js lost the §8 alias resolver (JS twin of signal_target_mode.py)'
