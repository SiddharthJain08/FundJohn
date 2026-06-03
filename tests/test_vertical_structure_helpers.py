# tests/test_vertical_structure_helpers.py
import json
import execution.alpaca_executor as ex
from strategies.base import OptionSpec
import datetime as dt

EXP = dt.date(2026, 7, 18)


def test_resolve_legs_vertical_call_buy_near_sell_far(monkeypatch):
    monkeypatch.setattr(ex, '_spot_price', lambda u: 500.0)
    monkeypatch.setattr(ex, '_resolve_strike', lambda spec, a, e: 500.0)
    monkeypatch.setattr(ex, '_option_chain_greeks',
                        lambda u, e, r, s, **k: [(505.0, 0.4, 'o1'), (515.0, 0.25, 'o2'), (520.0, 0.2, 'o3')])
    spec = OptionSpec(underlying='SPY', structure='vertical', right='call', spread_width_pct=0.03)
    legs = ex._resolve_structure_legs(spec, EXP, EXP)
    assert legs == [('call', 500.0, 'buy'), ('call', 515.0, 'sell')]


def test_resolve_legs_vertical_put_buy_near_sell_lower(monkeypatch):
    monkeypatch.setattr(ex, '_spot_price', lambda u: 500.0)
    monkeypatch.setattr(ex, '_resolve_strike', lambda spec, a, e: 500.0)
    monkeypatch.setattr(ex, '_option_chain_greeks',
                        lambda u, e, r, s, **k: [(480.0, -0.2, 'o1'), (485.0, -0.25, 'o2'), (495.0, -0.4, 'o3')])
    spec = OptionSpec(underlying='SPY', structure='vertical', right='put', spread_width_pct=0.03)
    legs = ex._resolve_structure_legs(spec, EXP, EXP)
    assert legs == [('put', 500.0, 'buy'), ('put', 485.0, 'sell')]


def test_resolve_legs_straddle_tagged_buy_identical_strikes(monkeypatch):
    monkeypatch.setattr(ex, '_spot_price', lambda u: 500.0)
    monkeypatch.setattr(ex, '_resolve_strike', lambda spec, a, e: 500.0)
    spec = OptionSpec(underlying='SPY', structure='straddle')
    legs = ex._resolve_structure_legs(spec, EXP, EXP)
    assert legs == [('call', 500.0, 'buy'), ('put', 500.0, 'buy')]


def test_net_quote_vertical_is_debit(monkeypatch):
    quotes = {'C500': {'ask': 12.0, 'bid': 11.5}, 'C515': {'ask': 4.5, 'bid': 4.0}}
    monkeypatch.setattr(ex, '_build_occ_symbol', lambda spec, k, e: 'C500' if k == 500.0 else 'C515')
    monkeypatch.setattr(ex, '_option_quote', lambda occ: quotes[occ])
    spec = OptionSpec(underlying='SPY', structure='vertical', right='call')
    legs = [('call', 500.0, 'buy'), ('call', 515.0, 'sell')]
    net, leg_q = ex._structure_net_quote(spec, legs, EXP)
    assert round(net, 2) == 8.0
    assert leg_q == [('C500', 'call', 'buy'), ('C515', 'call', 'sell')]


def test_net_quote_straddle_is_sum_byte_identical(monkeypatch):
    quotes = {'C': {'ask': 10.0, 'bid': 9.5}, 'P': {'ask': 11.0, 'bid': 10.5}}
    monkeypatch.setattr(ex, '_build_occ_symbol', lambda spec, k, e: 'C' if spec.right == 'call' else 'P')
    monkeypatch.setattr(ex, '_option_quote', lambda occ: quotes[occ])
    spec = OptionSpec(underlying='SPY', structure='straddle')
    legs = [('call', 500.0, 'buy'), ('put', 500.0, 'buy')]
    net, leg_q = ex._structure_net_quote(spec, legs, EXP)
    assert round(net, 2) == 21.0


def test_build_mleg_json_vertical_mixed_sides(monkeypatch):
    monkeypatch.setattr(ex, '_options_current_qty', lambda occ: 0.0)
    leg_q = [('C500', 'call', 'buy'), ('C515', 'call', 'sell')]
    out = json.loads(ex._build_mleg_legs_json(leg_q))
    assert out[0] == {'symbol': 'C500', 'side': 'buy', 'ratio_qty': '1', 'position_intent': 'buy_to_open'}
    assert out[1] == {'symbol': 'C515', 'side': 'sell', 'ratio_qty': '1', 'position_intent': 'sell_to_open'}


def test_build_mleg_json_straddle_uniform_buy_byte_identical(monkeypatch):
    monkeypatch.setattr(ex, '_options_current_qty', lambda occ: 0.0)
    leg_q = [('C', 'call', 'buy'), ('P', 'put', 'buy')]
    out = json.loads(ex._build_mleg_legs_json(leg_q))
    assert all(l['side'] == 'buy' and l['position_intent'] == 'buy_to_open' for l in out)
