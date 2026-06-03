"""tests/test_sp5_1c_gate_off_parity.py — gates-off parity: _route_option_order returns
None (falls through to equity path) when OPENCLAW_OPTION_EXEC is unset.

This is the equity/byte-identical invariant test for SP-5.1c. When the option exec gate
is OFF, an order carrying an OptionSpec must produce EXACTLY the same outcome as if the
option helper did not exist — i.e., None from _route_option_order so the caller falls
through unchanged to the standard equity/crypto path.
"""
from __future__ import annotations
import execution.alpaca_executor as ex
from strategies.base import OptionSpec


def test_option_route_skips_fail_closed_when_gate_off(monkeypatch):
    # NIT-1 contract (opus review): an OPTION order with the gate OFF returns a
    # SKIP dict (fail-closed by construction) — it must NEVER fall through to
    # the equity path. Genuine equity orders (no instrument_class) still get None.
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'option_spec': OptionSpec(underlying='SPY', structure='straddle', hedge='delta')}
    res = ex._route_option_order(order, equity=100_000.0, coid='c')
    assert res is not None and res.get('status') == 'skipped'
    assert 'gate is OFF' in (res.get('reason') or '')
