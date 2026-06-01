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


def test_list_expiries_passes_expiration_date_gte_when_given():
    """Regression: smoke 2026-05-29 found `option contracts --limit 500` returns
    500 same-day rows for SPY → _list_expiries saw only today's expiry, eligible
    set was empty, resolver fail-closed every option submit. Fix: narrow the
    page by --expiration-date-gte so future expiries appear in the first page."""
    from execution.alpaca_executor import _list_expiries
    captured = {}
    def _fake_cli(args, *a, **kw):
        captured['args'] = list(args)
        return True, {'option_contracts': [
            {'expiration_date': '2026-07-16'},
            {'expiration_date': '2026-08-20'},
        ]}, None
    with patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        out = _list_expiries('SPY', gte='2026-06-20')
    assert '--expiration-date-gte' in captured['args']
    i = captured['args'].index('--expiration-date-gte')
    assert captured['args'][i+1] == '2026-06-20'
    assert out == [dt.date(2026,7,16), dt.date(2026,8,20)]


def test_resolve_expiry_forwards_target_to_list_expiries():
    """_resolve_expiry must hand its computed target ISO-date to _list_expiries
    so the API narrows server-side. Otherwise the first-page-of-500 bug returns."""
    spec = type('S',(),{'underlying':'SPY','dte_target':22,'right':'call'})()
    captured = {}
    def _fake_list(underlying, gte=None):
        captured['underlying'] = underlying
        captured['gte'] = gte
        return [dt.date(2026,6,22), dt.date(2026,7,16)]
    with patch('execution.alpaca_executor._list_expiries', side_effect=_fake_list):
        e = _resolve_expiry(spec, as_of=dt.date(2026,5,29))
    assert captured['underlying'] == 'SPY'
    assert captured['gte'] == '2026-06-20'  # 2026-05-29 + 22d
    assert e == dt.date(2026,6,22)


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

from execution.alpaca_executor import _cash_collateral_required, _apply_cash_collateral_guard

def test_collateral_zero_for_long_calls():
    assert _cash_collateral_required(right='call', side='buy', strike=750, qty=5) == 0.0

def test_collateral_zero_for_long_puts():
    assert _cash_collateral_required(right='put', side='buy', strike=200, qty=5) == 0.0

def test_collateral_zero_for_short_calls():
    # Short calls are REFUSED elsewhere; collateral function returns 0 (no responsibility).
    assert _cash_collateral_required(right='call', side='sell', strike=750, qty=1) == 0.0

def test_collateral_required_for_short_puts():
    assert _cash_collateral_required(right='put', side='sell', strike=200, qty=2) == 40000.0

def test_cash_guard_passes_when_cash_sufficient():
    qty, reason = _apply_cash_collateral_guard(right='put', side='sell',
                                                strike=200, qty=2, account_cash=50000)
    assert qty == 2 and reason is None

def test_cash_guard_reduces_qty_when_partial_fit():
    qty, reason = _apply_cash_collateral_guard(right='put', side='sell',
                                                strike=200, qty=5, account_cash=50000)
    assert qty == 2 and reason and 'reduced' in reason  # 50000/(200*100)=2.5 -> 2

def test_cash_guard_skips_when_zero_fit():
    qty, reason = _apply_cash_collateral_guard(right='put', side='sell',
                                                strike=200, qty=1, account_cash=10000)
    assert qty == 0 and reason and 'insufficient' in reason

from execution.alpaca_executor import _resolve_option_qty

def test_qty_uses_contracts_when_ge_one():
    qty, reason = _resolve_option_qty(order={'contracts':3.2, 'notional_usd':50000},
                                       limit_price=20.0)
    assert qty == 3 and reason is None

def test_qty_floors_to_one_when_sub_contract_and_above_min_notional():
    qty, reason = _resolve_option_qty(order={'contracts':0.5, 'notional_usd':200},
                                       limit_price=1.5)
    assert qty == 1 and reason is None

def test_qty_skips_when_sub_contract_and_below_min_notional():
    qty, reason = _resolve_option_qty(order={'contracts':0.1, 'notional_usd':50},
                                       limit_price=0.5)
    assert qty == 0 and reason and 'below minimum' in reason

def test_qty_falls_back_to_notional_when_contracts_missing():
    qty, reason = _resolve_option_qty(order={'notional_usd':2000}, limit_price=10.0)
    assert qty == 2 and reason is None  # 2000 / (10*100) = 2

def test_qty_zero_contracts_skips():
    qty, reason = _resolve_option_qty(order={'contracts':0, 'notional_usd':100},
                                       limit_price=1.0)
    assert qty == 0 and reason and 'zero contracts' in reason

from execution.alpaca_executor import _options_session_gate

def test_options_session_gate_rth_allows():
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='rth'):
        ok, reason = _options_session_gate()
    assert ok and reason is None

def test_options_session_gate_premarket_skips():
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='premarket'):
        ok, reason = _options_session_gate()
    assert not ok and 'RTH-only' in reason

def test_options_session_gate_closed_skips():
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='closed'):
        ok, reason = _options_session_gate()
    assert not ok and 'market closed' in reason

def test_options_session_gate_afterhours_skips():
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='afterhours'):
        ok, reason = _options_session_gate()
    assert not ok and 'RTH-only' in reason


import os as _os
from unittest.mock import patch, MagicMock
from execution.alpaca_executor import _route_option_order

def _spec(structure='single', right='call', strike_rule='atm', moneyness=None,
          target_delta=0.30, dte_target=30, underlying='SPY'):
    s = MagicMock()
    s.structure = structure; s.right = right; s.strike_rule = strike_rule
    s.moneyness = moneyness; s.target_delta = target_delta
    s.dte_target = dte_target; s.underlying = underlying
    return s

def _option_order(direction='long', right='call', contracts=1, notional=20000,
                  strike_rule='atm', underlying='SPY'):
    return {
        'ticker': underlying, 'strategy_id': 'S_test',
        'direction': direction, 'instrument_class': 'option',
        'contracts': contracts, 'notional_usd': notional,
        'option_spec': _spec(right=right, strike_rule=strike_rule, underlying=underlying),
    }

def test_route_returns_none_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    assert _route_option_order(_option_order(), equity=100000, coid='c1') is None

def test_route_returns_none_when_not_option():
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    order = {'ticker':'AAPL','instrument_class':'equity','notional_usd':1000}
    try: assert _route_option_order(order, equity=100000, coid='c1') is None
    finally: _os.environ.pop('OPENCLAW_OPTION_EXEC', None)

def test_route_returns_none_when_missing_option_spec():
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    order = {'ticker':'SPY','instrument_class':'option','notional_usd':1000}  # no option_spec
    try: assert _route_option_order(order, equity=100000, coid='c1') is None
    finally: _os.environ.pop('OPENCLAW_OPTION_EXEC', None)

def test_route_skips_outside_rth():
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='closed'):
        res = _route_option_order(_option_order(), equity=100000, coid='c1')
    _os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    assert res and res.get('status') == 'skipped' and 'market closed' in res.get('reason','')

def test_route_skips_when_strike_unresolved():
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='rth'), \
         patch('execution.alpaca_executor._spot_price', return_value=None), \
         patch('execution.alpaca_executor._list_expiries',
               return_value=[__import__('datetime').date(2026,7,16)]):
        res = _route_option_order(_option_order(), equity=100000, coid='c1')
    _os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    assert res and res.get('status') == 'skipped' and 'strike' in res.get('reason','')

def test_route_refuses_short_call():
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    import datetime as _dt
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='rth'), \
         patch('execution.alpaca_executor._spot_price', return_value=750.0), \
         patch('execution.alpaca_executor._list_strikes', return_value=[745, 750, 755]), \
         patch('execution.alpaca_executor._list_expiries',
               return_value=[_dt.date(2026,7,16)]):
        res = _route_option_order(_option_order(direction='short', right='call'),
                                   equity=100000, coid='c1')
    _os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    assert res and res.get('status') == 'skipped' and 'short call' in res.get('reason','')

def test_route_short_put_with_insufficient_cash_skips():
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    import datetime as _dt
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='rth'), \
         patch('execution.alpaca_executor._spot_price', return_value=750.0), \
         patch('execution.alpaca_executor._list_strikes', return_value=[745, 750, 755]), \
         patch('execution.alpaca_executor._list_expiries',
               return_value=[_dt.date(2026,7,16)]), \
         patch('execution.alpaca_executor._account_cash', return_value=1000.0), \
         patch('execution.alpaca_executor._option_quote',
               return_value={'bid':5.0, 'ask':5.10}), \
         patch('execution.alpaca_executor._options_current_qty', return_value=0):
        res = _route_option_order(_option_order(direction='short', right='put',
                                                strike_rule='atm'),
                                   equity=100000, coid='c1')
    _os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    assert res and res.get('status') == 'skipped' and 'insufficient cash' in res.get('reason','')


def test_record_submission_writes_instrument_class():
    """A submitted option row carries instrument_class='option' (NULL for legacy equity)."""
    from execution.alpaca_executor import record_submission
    # Use a mocked psycopg2 connection; capture execute() args.
    captured = []
    class _C:
        def cursor(self): return self
        def execute(self, sql, params): captured.append((sql, params))
        def commit(self): pass
        def close(self): pass
    conn = _C()
    order = {'ticker': 'SPY260618C00750000', 'strategy_id': 'S_test', 'direction': 'long',
             'instrument_class': 'option'}
    resp = {'qty': 1, 'entry': 20.0, 'notional': 2000.0, 'order_id': 'xyz'}
    record_submission(conn, '2026-05-29', order, resp,
                      tif='day', order_class='simple', coid='c1')
    assert captured, 'execute was not called'
    _, params = captured[-1]
    assert 'option' in params  # the instrument_class column value


# --- _option_quote: latest-quotes endpoint (regression: chain limit=100 paging) ---
from execution.alpaca_executor import _option_quote

def test_option_quote_uses_latest_quotes_endpoint_not_chain():
    """Regression: SP-5.1a smoke 2026-06-01 fail-closed 'no quote for limit price'.
    `data option chain` defaults to limit=100; SPY has hundreds of strikes per
    expiry so (even filtered) the resolved OCC is truncated out of the page and
    the snapshot lookup misses → None → fail-closed. The exact-symbol
    `data option latest-quotes --symbols <OCC>` endpoint has no such truncation."""
    captured = {}
    def _fake_cli(args, *a, **kw):
        captured['args'] = list(args)
        return True, {'quotes': {'SPY260626C00756000': {'bp': 10.42, 'ap': 10.45}}}, None
    with patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        out = _option_quote('SPY260626C00756000')
    assert captured['args'][:3] == ['data', 'option', 'latest-quotes']
    assert '--symbols' in captured['args']
    assert captured['args'][captured['args'].index('--symbols') + 1] == 'SPY260626C00756000'
    assert 'chain' not in captured['args']  # the paginated endpoint must NOT be used
    assert out == {'bid': 10.42, 'ask': 10.45}

def test_option_quote_returns_none_when_symbol_absent():
    def _fake_cli(args, *a, **kw):
        return True, {'quotes': {}}, None
    with patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        assert _option_quote('SPY260626C00756000') is None

def test_option_quote_returns_none_on_zero_bid_or_ask():
    def _fake_cli(args, *a, **kw):
        return True, {'quotes': {'IWM260626P00272000': {'bp': 0, 'ap': 2.2}}}, None
    with patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        assert _option_quote('IWM260626P00272000') is None

def test_option_quote_returns_none_on_cli_failure():
    def _fake_cli(args, *a, **kw):
        return False, None, 'boom'
    with patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        assert _option_quote('IWM260626P00272000') is None


# --- limit_price_override: non-marketable sentinel hook (system_check / smoke probe) ---
def test_route_honors_limit_price_override_sentinel():
    """A non-marketable sentinel probe (system_check / smoke rest-then-cancel)
    passes limit_price_override; the broker submit must use it verbatim instead
    of the computed marketable limit, so the order RESTS instead of filling.
    Production sizer never sets this key, so real orders are byte-identical."""
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    import datetime as _dt
    captured = {}
    def _fake_cli(args, *a, **kw):
        if list(args[:2]) == ['order', 'submit']:
            captured['args'] = list(args)
            return True, {'id': 'sentinel-1'}, None
        return True, {}, None
    order = _option_order(direction='long', right='call', strike_rule='atm')
    order['limit_price_override'] = 1.00
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='rth'), \
         patch('execution.alpaca_executor._spot_price', return_value=750.0), \
         patch('execution.alpaca_executor._list_strikes', return_value=[745, 750, 755]), \
         patch('execution.alpaca_executor._list_expiries', return_value=[_dt.date(2026,7,16)]), \
         patch('execution.alpaca_executor._option_quote', return_value={'bid': 5.0, 'ask': 5.10}), \
         patch('execution.alpaca_executor._account_cash', return_value=100000.0), \
         patch('execution.alpaca_executor._options_current_qty', return_value=0), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        res = _route_option_order(order, equity=100000, coid='sentinel')
    _os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    assert res and res.get('status') == 'submitted'
    assert res.get('entry') == 1.00, f"entry should be the override, got {res.get('entry')}"
    a = captured['args']
    assert a[a.index('--limit-price') + 1] == '1.00'  # override, NOT 5.12 marketable

def test_route_uses_marketable_limit_when_no_override():
    """Without limit_price_override the computed marketable buy limit (ask+slip) is used."""
    _os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    import datetime as _dt
    captured = {}
    def _fake_cli(args, *a, **kw):
        if list(args[:2]) == ['order', 'submit']:
            captured['args'] = list(args)
            return True, {'id': 'mkt-1'}, None
        return True, {}, None
    order = _option_order(direction='long', right='call', strike_rule='atm')
    with patch('execution.alpaca_executor._alpaca_session_kind', return_value='rth'), \
         patch('execution.alpaca_executor._spot_price', return_value=750.0), \
         patch('execution.alpaca_executor._list_strikes', return_value=[745, 750, 755]), \
         patch('execution.alpaca_executor._list_expiries', return_value=[_dt.date(2026,7,16)]), \
         patch('execution.alpaca_executor._option_quote', return_value={'bid': 5.0, 'ask': 5.10}), \
         patch('execution.alpaca_executor._account_cash', return_value=100000.0), \
         patch('execution.alpaca_executor._options_current_qty', return_value=0), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        res = _route_option_order(order, equity=100000, coid='mkt')
    _os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    assert res and res.get('status') == 'submitted'
    assert res.get('entry') == 5.12  # ask 5.10 + 0.02 slip


def test_option_chain_greeks_returns_strike_delta_occ():
    """Strike-band chain fetch returns (strike, delta, occ) per contract, filtered
    by expiry+type+strike band. delta path: snapshots[occ].greeks.delta."""
    from execution.alpaca_executor import _option_chain_greeks
    captured = {}
    def _fake_cli(args, *a, **kw):
        captured['args'] = list(args)
        return True, {'snapshots': {
            'SPY260717C00760000': {'greeks': {'delta': 0.42}},
            'SPY260717C00780000': {'greeks': {'delta': 0.28}},
        }}, None
    with patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli):
        out = _option_chain_greeks('SPY', __import__('datetime').date(2026,7,17),
                                   'call', spot=750.0, band_pct=0.15)
    a = captured['args']
    assert a[:3] == ['data','option','chain']
    assert '--expiration-date' in a and '--type' in a
    assert '--strike-price-gte' in a and '--strike-price-lte' in a
    assert (780.0, 0.28, 'SPY260717C00780000') in [(s,d,o) for (s,d,o) in out]
