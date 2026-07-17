# tests/test_vertical_net_guard.py
import execution.alpaca_executor as ex
from strategies.base import OptionSpec
import datetime as dt


def _order(width=0.03):
    return {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
            'contracts': 1, 'notional_usd': 1000.0,
            'option_spec': OptionSpec(underlying='SPY', structure='vertical', right='call',
                                      strike_rule='atm', spread_width_pct=width)}


def _stub(monkeypatch, net):
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: dt.date(2026, 7, 18))
    monkeypatch.setattr(ex, '_resolve_structure_legs',
                        lambda s, t, e: [('call', 500.0, 'buy'), ('call', 515.0, 'sell')])
    monkeypatch.setattr(ex, '_structure_net_quote',
                        lambda s, l, e: (net, [('C500', 'call', 'buy'), ('C515', 'call', 'sell')]))
    monkeypatch.setattr(ex, '_resolve_option_qty', lambda d, lim: (1, None))  # explicit contracts -> would submit
    monkeypatch.setattr(ex, '_build_mleg_legs_json', lambda lq: '[]')
    submitted = []
    monkeypatch.setattr(ex, '_run_alpaca_cli', lambda args: (submitted.append(args) or (True, {'id': 'x'}, None)))
    return submitted


def test_negative_net_debit_refused_even_with_explicit_contracts(monkeypatch):
    submitted = _stub(monkeypatch, net=-3.50)   # crossed quotes
    res = ex._route_option_order(_order(), equity=100_000.0, coid='ng1')
    assert res.get('status') == 'skipped' and 'net debit' in (res.get('reason') or '')
    assert submitted == []        # NO order placed


def test_positive_net_debit_routes(monkeypatch):
    submitted = _stub(monkeypatch, net=8.0)
    res = ex._route_option_order(_order(), equity=100_000.0, coid='ng2')
    assert res.get('status') == 'submitted'


def test_nonpositive_width_failsclosed(monkeypatch):
    # real _resolve_structure_legs with width=0 -> None -> structure legs unresolved skip
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    monkeypatch.setattr(ex, '_options_session_gate', lambda: (True, ''))
    monkeypatch.setattr(ex, '_resolve_expiry', lambda spec, today: dt.date(2026, 7, 18))
    monkeypatch.setattr(ex, '_spot_price', lambda u: 500.0)
    monkeypatch.setattr(ex, '_resolve_strike', lambda spec, a, e: 500.0)
    monkeypatch.setattr(ex, '_option_chain_greeks', lambda u, e, r, s, **k: [(515.0, 0.25, 'o')])
    res = ex._route_option_order(_order(width=0.0), equity=100_000.0, coid='ng3')
    assert res.get('status') == 'skipped'
