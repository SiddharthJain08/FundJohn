# tests/test_credit_structure_legs.py
import execution.alpaca_executor as ex
from strategies.base import OptionSpec
import datetime as dt

EXP = dt.date(2026, 7, 18)


def _chain(monkeypatch, call_strikes, put_strikes, spot=500.0):
    monkeypatch.setattr(ex, '_spot_price', lambda u: spot)
    def grk(u, e, right, s, **k):
        rows = call_strikes if right == 'call' else put_strikes
        return [(k_, d_, f'o{k_}') for k_, d_ in rows]
    monkeypatch.setattr(ex, '_option_chain_greeks', grk)


def test_credit_vertical_call_sell_near_buy_higher(monkeypatch):
    _chain(monkeypatch, [(515.0, 0.30), (520.0, 0.25), (535.0, 0.15)], [])
    monkeypatch.setattr(ex, '_resolve_strike', lambda spec, a, e: 520.0)
    spec = OptionSpec(underlying='SPY', structure='credit_vertical', right='call',
                      strike_rule='target_delta', target_delta=0.25, spread_width_pct=0.03)
    legs = ex._resolve_structure_legs(spec, EXP, EXP)
    assert legs == [('call', 520.0, 'sell'), ('call', 535.0, 'buy')]


def test_credit_vertical_put_sell_near_buy_lower(monkeypatch):
    _chain(monkeypatch, [], [(465.0, -0.15), (480.0, -0.25), (485.0, -0.30)])
    monkeypatch.setattr(ex, '_resolve_strike', lambda spec, a, e: 480.0)
    spec = OptionSpec(underlying='SPY', structure='credit_vertical', right='put',
                      strike_rule='target_delta', target_delta=0.25, spread_width_pct=0.03)
    legs = ex._resolve_structure_legs(spec, EXP, EXP)
    assert legs == [('put', 480.0, 'sell'), ('put', 465.0, 'buy')]


def test_iron_condor_four_legs_ordered(monkeypatch):
    _chain(monkeypatch,
           [(520.0, 0.25), (535.0, 0.15)],
           [(465.0, -0.15), (480.0, -0.25)])
    spec = OptionSpec(underlying='SPY', structure='iron_condor',
                      target_delta=0.25, spread_width_pct=0.03)
    legs = ex._resolve_structure_legs(spec, EXP, EXP)
    assert legs == [('call', 520.0, 'sell'), ('call', 535.0, 'buy'),
                    ('put', 480.0, 'sell'), ('put', 465.0, 'buy')]


def test_iron_condor_crossed_strikes_failclosed(monkeypatch):
    _chain(monkeypatch, [(520.0, 0.25), (535.0, 0.15)], [(505.0, -0.25), (490.0, -0.15)])
    spec = OptionSpec(underlying='SPY', structure='iron_condor',
                      target_delta=0.25, spread_width_pct=0.03)
    assert ex._resolve_structure_legs(spec, EXP, EXP) is None


def test_credit_vertical_zero_width_failclosed(monkeypatch):
    _chain(monkeypatch, [(520.0, 0.25)], [])
    monkeypatch.setattr(ex, '_resolve_strike', lambda spec, a, e: 520.0)
    spec = OptionSpec(underlying='SPY', structure='credit_vertical', right='call',
                      spread_width_pct=0.0)
    assert ex._resolve_structure_legs(spec, EXP, EXP) is None
