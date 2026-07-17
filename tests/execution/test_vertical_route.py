import execution.alpaca_executor as ex
from strategies.base import OptionSpec
import datetime as dt


def _order(direction='long', right='call'):
    return {'ticker': 'SPY', 'instrument_class': 'option', 'direction': direction,
            'contracts': 1, 'notional_usd': 1000.0,
            'option_spec': OptionSpec(underlying='SPY', structure='vertical', right=right,
                                      strike_rule='atm', spread_width_pct=0.03)}


def _stub_open(monkeypatch):
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: dt.date(2026, 7, 18))
    monkeypatch.setattr(ex, '_resolve_structure_legs',
                        lambda s, t, e: [('call', 500.0, 'buy'), ('call', 515.0, 'sell')])
    monkeypatch.setattr(ex, '_structure_net_quote',
                        lambda s, l, e: (8.0, [('SPY...C500', 'call', 'buy'), ('SPY...C515', 'call', 'sell')]))
    monkeypatch.setattr(ex, '_resolve_option_qty', lambda d, lim: (1, None))
    monkeypatch.setattr(ex, '_build_mleg_legs_json', lambda lq: '[]')
    monkeypatch.setattr(ex, '_run_alpaca_cli', lambda args: (True, {'id': 'ord-v1'}, None))


def test_long_debit_vertical_routes(monkeypatch):
    _stub_open(monkeypatch)
    res = ex._route_option_order(_order('long', 'call'), equity=100_000.0, coid='v1')
    assert res is not None and res.get('status') == 'submitted'
    assert res.get('structure') == 'vertical'


def test_vertical_gate_off_skips_fail_closed(monkeypatch):
    # NIT-1 contract: option order + gate OFF -> SKIP dict, never equity fall-through.
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    res = ex._route_option_order(_order('long', 'call'), equity=100_000.0, coid='v2')
    assert res is not None and res.get('status') == 'skipped'
    assert 'gate is OFF' in (res.get('reason') or '')


def test_vertical_non_long_refused(monkeypatch):
    _stub_open(monkeypatch)
    res = ex._route_option_order(_order('short', 'call'), equity=100_000.0, coid='v3')
    assert res.get('status') == 'skipped' and 'long-only' in (res.get('reason') or '')
