"""SP-5.1a — option_routing system_check sentinel refactor.

The check must NOT place a marketable (filling) order. It routes a
non-marketable sentinel (limit_price_override=1.00) that RESTS, cancels it,
and confirms flat. Fast + deterministic: executor calls are mocked.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.types import Status
from system_checks.checks.option_routing import _option_routing


def test_gate_off_skips(monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTION_EXEC', raising=False)
    status, detail = _option_routing()
    assert status == Status.SKIP
    assert 'OPENCLAW_OPTION_EXEC=0' in detail


def test_sentinel_passes_override_and_cancels(monkeypatch):
    """Gate ON: the routed order must carry a non-marketable limit_price_override,
    and a submitted sentinel must be canceled with a flat cross-check -> PASS."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    calls = []
    routed = {}

    def _fake_route(order, equity, coid):
        routed.update(order)
        return {'ticker': 'SPY260717C00750000', 'status': 'submitted',
                'order_id': 'sentinel-1', 'client_order_id': coid,
                'instrument_class': 'option', 'entry': 1.00}

    def _fake_cli(args, *a, **kw):
        calls.append(list(args))
        if list(args[:2]) == ['order', 'get']:
            return True, {'status': 'canceled'}, None  # cancel confirmed terminal
        return True, {}, None

    with patch('execution.alpaca_executor._route_option_order', side_effect=_fake_route), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli), \
         patch('execution.alpaca_executor._options_current_qty', return_value=0):
        status, detail = _option_routing()

    assert status == Status.PASS, detail
    # the routed order must request a NON-MARKETABLE sentinel limit
    assert routed.get('limit_price_override') is not None
    assert float(routed['limit_price_override']) < 5.0  # far below an ATM SPY call
    # a cancel was issued AND confirmed terminal (poll), not just acked
    assert any(c[:2] == ['order', 'cancel'] for c in calls), calls
    assert any(c[:2] == ['order', 'get'] for c in calls), calls
    assert 'canceled' in detail


def test_sentinel_cancel_unconfirmed_fails(monkeypatch):
    """If the cancel doesn't reach a terminal state, claim it FAIL (a resting
    order may linger) rather than falsely reporting 'canceled'."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')

    def _fake_route(order, equity, coid):
        return {'ticker': 'SPY260717C00750000', 'status': 'submitted',
                'order_id': 'sentinel-3', 'client_order_id': coid,
                'instrument_class': 'option', 'entry': 1.00}

    def _fake_cli(args, *a, **kw):
        if list(args[:2]) == ['order', 'cancel']:
            return False, None, 'cancel rejected'   # cancel ack fails
        if list(args[:2]) == ['order', 'get']:
            return True, {'status': 'new'}, None     # never reaches terminal
        return True, {}, None

    # Fake clock so the bounded poll loop exits after one read instead of
    # spinning the real 5s timeout; sleep no-op'd.
    import system_checks.checks.option_routing as orc

    class _Clock:
        def __init__(self): self.n = 0
        def __call__(self):
            self.n += 1
            return 1000.0 if self.n <= 3 else 1e9  # jump past deadline after first read

    with patch('execution.alpaca_executor._route_option_order', side_effect=_fake_route), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli), \
         patch('execution.alpaca_executor._options_current_qty', return_value=0), \
         patch.object(orc.time, 'time', new=_Clock()), \
         patch.object(orc.time, 'sleep', new=lambda *_: None):
        status, detail = _option_routing()

    assert status == Status.FAIL
    assert 'UNCONFIRMED' in detail


def test_sentinel_filled_flattens_and_fails(monkeypatch):
    """If the sentinel unexpectedly FILLS (residual != 0), flatten + FAIL loudly."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    calls = []

    def _fake_route(order, equity, coid):
        return {'ticker': 'SPY260717C00750000', 'status': 'submitted',
                'order_id': 'sentinel-2', 'client_order_id': coid,
                'instrument_class': 'option', 'entry': 1.00}

    def _fake_cli(args, *a, **kw):
        calls.append(list(args))
        if list(args[:2]) == ['order', 'get']:
            return True, {'status': 'filled'}, None  # terminal: the sentinel filled
        return True, {}, None

    with patch('execution.alpaca_executor._route_option_order', side_effect=_fake_route), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli), \
         patch('execution.alpaca_executor._options_current_qty', return_value=1.0):
        status, detail = _option_routing()

    assert status == Status.FAIL
    assert 'FILLED' in detail
    assert any(c[:2] == ['position', 'close'] for c in calls), calls


def test_option_routing_includes_mleg_sentinel(monkeypatch):
    """Gate ON: the check routes a straddle sentinel with limit_price_override and
    cancels it; detail mentions the structure sentinel."""
    monkeypatch.setenv('OPENCLAW_OPTION_EXEC', '1')
    def _fake_route(order, equity, coid):
        # both the single-leg sentinel and the mleg sentinel route through here;
        # return a submitted dict for whichever is asked.
        is_mleg = (getattr(order.get('option_spec'), 'structure', 'single') != 'single')
        return {'ticker':'SPY','structure':'straddle' if is_mleg else None,
                'status':'submitted','order_id':('mleg-s1' if is_mleg else 'sl1'),
                'client_order_id':coid,'instrument_class':'option',
                'order_class':('mleg' if is_mleg else 'simple'),
                'limit_price_override': order.get('limit_price_override')}
    def _fake_cli(args, *a, **kw):
        if list(args[:2]) == ['order','get']: return True, {'status':'canceled'}, None
        return True, {}, None
    with patch('execution.alpaca_executor._route_option_order', side_effect=_fake_route), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli), \
         patch('execution.alpaca_executor._options_current_qty', return_value=0):
        from system_checks.checks.option_routing import _option_routing
        status, detail = _option_routing()
    from system_checks.types import Status
    assert status == Status.PASS
    assert 'mleg' in detail
