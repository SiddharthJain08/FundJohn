"""SP-5.2 gate-off parity test — vertical order returns None when OPENCLAW_OPTION_EXEC is unset."""
import execution.alpaca_executor as ex
from strategies.base import OptionSpec


def test_vertical_route_none_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'option_spec': OptionSpec(underlying='SPY', structure='vertical', right='call')}
    assert ex._route_option_order(order, equity=100_000.0, coid='c') is None
