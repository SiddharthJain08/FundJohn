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
    # 5.2 side-aware shapes: legs=(right, strike, side); leg_q=(occ, right, side);
    # _build_mleg_legs_json takes leg_q only.
    monkeypatch.setattr(ex, '_resolve_structure_legs', lambda s, t, e: [('call', 500.0, 'buy'), ('put', 500.0, 'buy')])
    monkeypatch.setattr(ex, '_structure_net_quote', lambda s, l, e: (20.0, [('SPY260718C00500000', 'call', 'buy'), ('SPY260718P00500000', 'put', 'buy')]))
    monkeypatch.setattr(ex, '_resolve_option_qty', lambda d, lim: (1, None))
    monkeypatch.setattr(ex, '_build_mleg_legs_json', lambda lq: '[]')
    monkeypatch.setattr(ex, '_run_alpaca_cli', lambda args: (True, {'id': 'ord-1'}, None))
    # hedge='delta' + submit-success reaches the on-fill ledger write; make sure
    # this unit test can never touch a real DB (the write is guarded/non-aborting).
    monkeypatch.delenv('POSTGRES_URI', raising=False)
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


def test_delta_hedged_vertical_refused_fail_closed(monkeypatch):
    """Post-5.2 merge guard: hedge='delta' admits only ALL-BUY structures
    (straddle/strangle). A vertical has a SHORT far leg, but the ledger leg
    schema carries no side and compute_structure_delta sums deltas unsigned —
    a delta-hedged vertical would produce a WRONG hedge target. Refuse."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setenv('OPENCLAW_OPTION_DELTA_HEDGE', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: __import__('datetime').date(2026, 7, 18))
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'contracts': 1, 'notional_usd': 2000.0,
             'option_spec': OptionSpec(underlying='SPY', structure='vertical',
                                       hedge='delta', strike_rule='atm')}
    res = ex._route_option_order(order, equity=100_000.0, coid='c4')
    assert res.get('status') == 'skipped'
    assert 'straddle/strangle' in (res.get('reason') or '')
