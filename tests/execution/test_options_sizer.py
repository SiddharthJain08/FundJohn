import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution.instrument_class_sizer import apply_instrument_class_sizing


def test_equity_etp_crypto_passthrough_unchanged():
    o = {'ticker': 'AAPL', 'notional_usd': 1000.0}
    for ic in ('equity', 'etp', 'crypto'):
        assert apply_instrument_class_sizing(dict(o), ic) == o


def test_option_delta_dollar_sizing():
    # target delta-dollar = notional_usd; contracts sized so |delta|*S*100*n ≈ notional
    o = {'ticker': 'SPY', 'notional_usd': 5000.0, 'delta': 0.5,
         'underlying_price': 500.0}
    out = apply_instrument_class_sizing(o, 'option')
    # delta-dollar per contract = 0.5 * 500 * 100 = 25000 → contracts = 5000/25000 = 0.2
    assert abs(out['contracts'] - 0.2) < 1e-6


def test_option_fail_open_without_delta():
    o = {'ticker': 'SPY', 'notional_usd': 5000.0}
    out = apply_instrument_class_sizing(o, 'option')
    assert out['notional_usd'] == 5000.0  # unchanged, fail-open
