"""SP-5.1c Task 6 — lift executor structure envelope to allow hedge='delta'
for LONG structures when OPENCLAW_OPTION_DELTA_HEDGE=1.

TDD: tests written first (red), then guard lifted (green).
5.1a single-leg + 5.1b-i long-no-hedge mleg paths must remain byte-identical.
"""
import execution.alpaca_executor as ex
from strategies.base import OptionSpec


def _order(direction='long', hedge='delta'):
    return {'ticker': 'SPY', 'instrument_class': 'option', 'direction': direction,
            'contracts': 1, 'notional_usd': 2000.0,
            'option_spec': OptionSpec(underlying='SPY', structure='straddle',
                                      hedge=hedge, strike_rule='atm')}


def test_long_delta_straddle_not_refused_by_hedge_guard(monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setenv('OPENCLAW_OPTION_DELTA_HEDGE', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: __import__('datetime').date(2026, 7, 18))
    monkeypatch.setattr(ex, '_resolve_structure_legs', lambda s, t, e: [('call', 500.0), ('put', 500.0)])
    monkeypatch.setattr(ex, '_structure_net_quote', lambda s, l, e: (20.0, [('SPY260718C00500000', 'call', 10.0), ('SPY260718P00500000', 'put', 10.0)]))
    monkeypatch.setattr(ex, '_resolve_option_qty', lambda d, lim: (1, None))
    monkeypatch.setattr(ex, '_build_mleg_legs_json', lambda lq, d: '[]')
    monkeypatch.setattr(ex, '_run_alpaca_cli', lambda args: (True, {'id': 'ord-1'}, None))
    res = ex._route_option_order(_order('long', 'delta'), equity=100_000.0, coid='c1')
    assert res is not None and res.get('status') == 'submitted'


def test_non_long_structure_still_refused(monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: __import__('datetime').date(2026, 7, 18))
    res = ex._route_option_order(_order('short', 'delta'), equity=100_000.0, coid='c2')
    assert res.get('status') == 'skipped' and 'long-only' in (res.get('reason') or '')


def test_unknown_hedge_value_refused(monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: __import__('datetime').date(2026, 7, 18))
    res = ex._route_option_order(_order('long', 'collar'), equity=100_000.0, coid='c3')
    assert res.get('status') == 'skipped'
