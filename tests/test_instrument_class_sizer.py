import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest
from execution.instrument_class_sizer import apply_instrument_class_sizing  # noqa


def test_equity_is_identity():
    order = {'ticker': 'AAPL', 'notional_usd': 1000.0}
    assert apply_instrument_class_sizing(order, 'equity')['notional_usd'] == 1000.0


def test_etp_is_identity():
    order = {'ticker': 'GLD', 'notional_usd': 1000.0}
    assert apply_instrument_class_sizing(order, 'etp')['notional_usd'] == 1000.0


def test_option_scales_by_delta_when_present():
    order = {'ticker': 'SPY', 'notional_usd': 1000.0, 'delta': 0.5}
    assert apply_instrument_class_sizing(order, 'option')['notional_usd'] == 500.0


def test_option_without_delta_passes_through_failopen():
    order = {'ticker': 'SPY', 'notional_usd': 1000.0}
    assert apply_instrument_class_sizing(order, 'option')['notional_usd'] == 1000.0


def test_crypto_raises_no_handler():
    with pytest.raises(NotImplementedError, match='crypto'):
        apply_instrument_class_sizing({'ticker': 'BTC-USD', 'notional_usd': 1.0}, 'crypto')
