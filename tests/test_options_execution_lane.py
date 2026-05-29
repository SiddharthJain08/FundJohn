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

from unittest.mock import patch
from execution.alpaca_executor import _resolve_strike, _resolve_expiry

def test_resolve_strike_atm_returns_nearest_listed():
    spec = type('S',(),{'underlying':'SPY','strike_rule':'atm','right':'call'})()
    with patch('execution.alpaca_executor._spot_price', return_value=750.15), \
         patch('execution.alpaca_executor._list_strikes',
               return_value=[745.0, 750.0, 755.0]):
        k = _resolve_strike(spec, as_of=dt.date(2026,5,29), expiry=dt.date(2026,6,18))
    assert k == 750.0

def test_resolve_strike_atm_returns_none_on_empty_chain():
    spec = type('S',(),{'underlying':'SPY','strike_rule':'atm','right':'call'})()
    with patch('execution.alpaca_executor._spot_price', return_value=750.0), \
         patch('execution.alpaca_executor._list_strikes', return_value=[]):
        k = _resolve_strike(spec, as_of=dt.date(2026,5,29), expiry=dt.date(2026,6,18))
    assert k is None

def test_resolve_strike_atm_returns_none_on_spot_fetch_failure():
    spec = type('S',(),{'underlying':'SPY','strike_rule':'atm','right':'call'})()
    with patch('execution.alpaca_executor._spot_price', return_value=None):
        k = _resolve_strike(spec, as_of=dt.date(2026,5,29), expiry=dt.date(2026,6,18))
    assert k is None

def test_resolve_strike_fixed_moneyness():
    spec = type('S',(),{'underlying':'SPY','strike_rule':'fixed_moneyness',
                        'moneyness':0.95,'right':'put'})()
    with patch('execution.alpaca_executor._spot_price', return_value=750.0), \
         patch('execution.alpaca_executor._list_strikes',
               return_value=[700, 710, 712.5, 715, 720]):
        k = _resolve_strike(spec, as_of=dt.date(2026,5,29), expiry=dt.date(2026,6,18))
    assert k == 712.5  # 712.5 is nearest to 712.5 (=750*0.95)

def test_resolve_expiry_nearest_monthly_listed():
    spec = type('S',(),{'underlying':'SPY','dte_target':30,'right':'call'})()
    with patch('execution.alpaca_executor._list_expiries',
               return_value=[dt.date(2026,6,18), dt.date(2026,7,16), dt.date(2026,8,20)]):
        e = _resolve_expiry(spec, as_of=dt.date(2026,5,29))
    assert e == dt.date(2026,7,16)  # nearest monthly >= 30 days from 5-29

def test_resolve_expiry_returns_none_on_empty():
    spec = type('S',(),{'underlying':'SPY','dte_target':30,'right':'call'})()
    with patch('execution.alpaca_executor._list_expiries', return_value=[]):
        assert _resolve_expiry(spec, as_of=dt.date(2026,5,29)) is None

from execution.alpaca_executor import _options_position_intent

def test_intent_long_no_position():
    s, i = _options_position_intent(direction='long', current_qty=0)
    assert (s, i) == ('buy', 'buy_to_open')

def test_intent_short_no_position():
    s, i = _options_position_intent(direction='short', current_qty=0)
    assert (s, i) == ('sell', 'sell_to_open')

def test_intent_long_closes_existing_short():
    s, i = _options_position_intent(direction='long', current_qty=-1)
    assert (s, i) == ('buy', 'buy_to_close')

def test_intent_short_closes_existing_long():
    s, i = _options_position_intent(direction='short', current_qty=1)
    assert (s, i) == ('sell', 'sell_to_close')
