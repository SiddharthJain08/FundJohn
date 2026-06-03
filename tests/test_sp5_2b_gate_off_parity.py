"""SP-5.2b gate-off parity test — credit_vertical and iron_condor return None when
OPENCLAW_OPTION_EXEC is unset (gate-check fires before any spec/chain resolution)."""
import execution.alpaca_executor as ex
from strategies.base import OptionSpec


def test_credit_structures_none_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    for spec in (OptionSpec(underlying='SPY', structure='credit_vertical', right='call'),
                 OptionSpec(underlying='SPY', structure='iron_condor')):
        order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'short',
                 'contracts': 1, 'option_spec': spec}
        assert ex._route_option_order(order, equity=100_000.0, coid='c') is None
