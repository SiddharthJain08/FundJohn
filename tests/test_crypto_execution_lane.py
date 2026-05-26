"""tests/test_crypto_execution_lane.py — SP-3.1 Phase A crypto execution lane."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402


def test_is_crypto_ticker():
    assert ae._is_crypto_ticker('BTC-USD') is True
    assert ae._is_crypto_ticker('eth-usd') is True
    assert ae._is_crypto_ticker('AAPL') is False
    assert ae._is_crypto_ticker('BRK-B') is False
    assert ae._is_crypto_ticker('') is False
    assert ae._is_crypto_ticker(None) is False


def test_alpaca_crypto_symbol():
    assert ae._alpaca_crypto_symbol('BTC-USD') == 'BTC/USD'
    assert ae._alpaca_crypto_symbol('eth-usd') == 'ETH/USD'
