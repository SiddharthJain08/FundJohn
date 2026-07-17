"""SP-5.2 gate-off parity test — vertical order returns None when OPENCLAW_OPTION_EXEC is unset."""
import execution.alpaca_executor as ex
from strategies.base import OptionSpec


def test_vertical_route_skips_fail_closed_when_gate_off(monkeypatch):
    # NIT-1 contract: option order + gate OFF -> SKIP dict, never equity fall-through.
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'option_spec': OptionSpec(underlying='SPY', structure='vertical', right='call')}
    res = ex._route_option_order(order, equity=100_000.0, coid='c')
    assert res is not None and res.get('status') == 'skipped'
    assert 'gate is OFF' in (res.get('reason') or '')
