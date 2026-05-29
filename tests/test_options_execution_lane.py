"""tests/test_options_execution_lane.py — SP-5.1a single-leg options exec."""
from __future__ import annotations
import sys, datetime as dt
sys.path.insert(0, 'src')
from execution.alpaca_executor import _build_occ_symbol

class _Spec:
    def __init__(self, underlying, right): self.underlying = underlying; self.right = right

def test_occ_builder_call_two_digit_strike():
    s = _build_occ_symbol(_Spec('SPY','call'), strike=750.0, expiry=dt.date(2026,6,18))
    assert s == 'SPY260618C00750000'

def test_occ_builder_put_three_digit_strike_with_decimal():
    s = _build_occ_symbol(_Spec('IWM','put'), strike=245.5, expiry=dt.date(2026,6,18))
    assert s == 'IWM260618P00245500'

def test_occ_builder_four_digit_strike():
    s = _build_occ_symbol(_Spec('SPX','call'), strike=4750.0, expiry=dt.date(2026,12,17))
    assert s == 'SPX261217C04750000'

def test_occ_builder_raises_on_negative_strike():
    import pytest
    with pytest.raises(ValueError):
        _build_occ_symbol(_Spec('SPY','call'), strike=-1, expiry=dt.date(2026,6,18))
