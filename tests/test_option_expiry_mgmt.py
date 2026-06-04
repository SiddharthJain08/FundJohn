"""SP-5.3 — option expiry management + held-awareness.

C1: _occ_dte / _expiry_close_dte. C2: held-open suppression (the activation-safety
stacking guard). C3: __close_option_expiry__ emission. C4: held-fetch None contract.
Drives the REAL _sharpe_cadence_path (no stub) by monkeypatching only the DB/broker
seams — same conventions as tests/test_sizer_class_consolidation.py.
"""
import logging
from datetime import date, timedelta
from unittest import mock as _mock

import pytest

import execution.regime_blended_sizer as _sizer

_STRADDLE_SPEC = {'underlying': 'SPY', 'structure': 'straddle', 'hedge': 'delta',
                  'strike_rule': 'atm', 'right': 'both'}


def _occ(root, days_out, right='C', strike='00500000'):
    """OCC symbol expiring `days_out` calendar days from today."""
    exp = date.today() + timedelta(days=days_out)
    return f"{root}{exp.strftime('%y%m%d')}{right}{strike}"


# ===========================================================================
# C1 — _occ_dte + _expiry_close_dte
# ===========================================================================

def test_occ_dte_basic():
    assert _sizer._occ_dte(_occ('SPY', 7)) == 7
    assert _sizer._occ_dte(_occ('SPY', 0)) == 0
    assert _sizer._occ_dte(_occ('A', 30, right='P')) == 30   # 1-char root


def test_occ_dte_explicit_today():
    # SPY 2026-07-18 call seen from 2026-07-11 = 7 days.
    assert _sizer._occ_dte('SPY260718C00500000', today=date(2026, 7, 11)) == 7


def test_occ_dte_parse_failure_is_zero(caplog):
    """An unreadable expiry is an unmanageable position — DTE 0 (close it) + warn."""
    with caplog.at_level(logging.WARNING):
        assert _sizer._occ_dte('SPY999999C00500000') == 0
    assert any('unparseable' in r.message.lower() for r in caplog.records)


def test_expiry_close_dte_default_and_override(monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', raising=False)
    assert _sizer._expiry_close_dte() == 7
    monkeypatch.setenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', '10')
    assert _sizer._expiry_close_dte() == 10
    monkeypatch.setenv('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE', 'junk')
    assert _sizer._expiry_close_dte() == 7   # parse-guarded fallback
