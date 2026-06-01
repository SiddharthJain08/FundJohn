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
        return True, {}, None

    with patch('execution.alpaca_executor._route_option_order', side_effect=_fake_route), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli), \
         patch('execution.alpaca_executor._options_current_qty', return_value=0):
        status, detail = _option_routing()

    assert status == Status.PASS, detail
    # the routed order must request a NON-MARKETABLE sentinel limit
    assert routed.get('limit_price_override') is not None
    assert float(routed['limit_price_override']) < 5.0  # far below an ATM SPY call
    # a cancel was issued for the resting sentinel
    assert any(c[:2] == ['order', 'cancel'] for c in calls), calls
    assert 'canceled' in detail


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
        return True, {}, None

    with patch('execution.alpaca_executor._route_option_order', side_effect=_fake_route), \
         patch('execution.alpaca_executor._run_alpaca_cli', side_effect=_fake_cli), \
         patch('execution.alpaca_executor._options_current_qty', return_value=1.0):
        status, detail = _option_routing()

    assert status == Status.FAIL
    assert 'FILLED' in detail
    assert any(c[:2] == ['position', 'close'] for c in calls), calls
