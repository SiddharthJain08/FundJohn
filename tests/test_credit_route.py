# tests/test_credit_route.py
import execution.alpaca_executor as ex
from strategies.base import OptionSpec
import datetime as dt


def _order(structure='credit_vertical', direction='short', contracts=1):
    spec = (OptionSpec(underlying='SPY', structure=structure, right='call',
                       strike_rule='target_delta', target_delta=0.25, spread_width_pct=0.03)
            if structure == 'credit_vertical' else
            OptionSpec(underlying='SPY', structure=structure, target_delta=0.25, spread_width_pct=0.03))
    return {'ticker': 'SPY', 'instrument_class': 'option', 'direction': direction,
            'contracts': contracts, 'notional_usd': 1000.0, 'option_spec': spec}


def _stub(monkeypatch, net, legs=None, leg_q=None):
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: dt.date(2026, 7, 18))
    monkeypatch.setattr(ex, '_resolve_structure_legs',
                        lambda s, t, e: legs or [('call', 520.0, 'sell'), ('call', 535.0, 'buy')])
    monkeypatch.setattr(ex, '_structure_net_quote',
                        lambda s, l, e: (net, leg_q or [('C520', 'call', 'sell'), ('C535', 'call', 'buy')]))
    monkeypatch.setattr(ex, '_resolve_option_qty', lambda d, lim: (1, None))
    monkeypatch.setattr(ex, '_build_mleg_legs_json', lambda lq: '[]')
    submitted = []
    monkeypatch.setattr(ex, '_run_alpaca_cli',
                        lambda args: (submitted.append(args) or (True, {'id': 'cr-1'}, None)))
    return submitted


def test_short_credit_vertical_routes_with_negative_eq_limit(monkeypatch):
    submitted = _stub(monkeypatch, net=-4.50)
    res = ex._route_option_order(_order(), equity=100_000.0, coid='c1')
    assert res is not None and res.get('status') == 'submitted'
    args = submitted[0]
    assert '--limit-price=-4.48' in args
    assert '--limit-price' not in args


def test_iron_condor_routes_short(monkeypatch):
    legs = [('call', 520.0, 'sell'), ('call', 535.0, 'buy'), ('put', 480.0, 'sell'), ('put', 465.0, 'buy')]
    leg_q = [('C520', 'call', 'sell'), ('C535', 'call', 'buy'), ('P480', 'put', 'sell'), ('P465', 'put', 'buy')]
    _stub(monkeypatch, net=-2.10, legs=legs, leg_q=leg_q)
    res = ex._route_option_order(_order('iron_condor'), equity=100_000.0, coid='c2')
    assert res.get('status') == 'submitted' and res.get('structure') == 'iron_condor'


def test_long_credit_structure_refused(monkeypatch):
    _stub(monkeypatch, net=-4.50)
    res = ex._route_option_order(_order(direction='long'), equity=100_000.0, coid='c3')
    assert res.get('status') == 'skipped' and 'short-only' in (res.get('reason') or '')


def test_short_straddle_still_refused(monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: dt.date(2026, 7, 18))
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'short', 'contracts': 1,
             'option_spec': OptionSpec(underlying='SPY', structure='straddle')}
    res = ex._route_option_order(order, equity=100_000.0, coid='c4')
    assert res.get('status') == 'skipped' and 'long-only' in (res.get('reason') or '')


def test_credit_with_nonnegative_net_refused(monkeypatch):
    submitted = _stub(monkeypatch, net=0.75)
    res = ex._route_option_order(_order(), equity=100_000.0, coid='c5')
    assert res.get('status') == 'skipped' and submitted == []


def test_credit_without_contracts_refused(monkeypatch):
    _stub(monkeypatch, net=-4.50)
    res = ex._route_option_order(_order(contracts=None), equity=100_000.0, coid='c6')
    assert res.get('status') == 'skipped'


def test_credit_gate_off_none(monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    assert ex._route_option_order(_order(), equity=100_000.0, coid='c7') is None


def test_credit_tiny_net_pad_crossover_refused(monkeypatch):
    # net=-0.01: raw-net guard passes (<0) but pad flips limit to +0.01 -> MUST refuse, never submit
    submitted = _stub(monkeypatch, net=-0.01)
    res = ex._route_option_order(_order(), equity=100_000.0, coid='c8')
    assert res.get('status') == 'skipped' and submitted == []


def test_credit_net_exactly_pad_refused(monkeypatch):
    # net=-0.02 -> limit 0.00 -> refuse
    submitted = _stub(monkeypatch, net=-0.02)
    res = ex._route_option_order(_order(), equity=100_000.0, coid='c9')
    assert res.get('status') == 'skipped' and submitted == []


def test_credit_positive_override_refused(monkeypatch):
    submitted = _stub(monkeypatch, net=-4.50)
    o = _order(); o['limit_price_override'] = 1.00   # positive override on a credit -> refuse
    res = ex._route_option_order(o, equity=100_000.0, coid='c10')
    assert res.get('status') == 'skipped' and submitted == []
