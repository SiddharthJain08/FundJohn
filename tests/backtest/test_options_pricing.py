import sys, math
from pathlib import Path
from datetime import date
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest.options_pricing import (
    bs_price, bs_greeks, implied_vol, strike_for_target_delta,
    nearest_monthly_expiry, RISK_FREE,
)


def test_bs_price_atm_call_known_value():
    # S=K=100, t=0.5, r=RISK_FREE=0.04, sigma=0.2 → ~6.627 (py_vollib reference)
    p = bs_price('c', 100, 100, 0.5, 0.2)
    assert abs(p - 6.627) < 0.01


def test_bs_greeks_atm_call_delta_near_half():
    g = bs_greeks('c', 100, 100, 0.5, 0.2)
    assert 0.50 < g['delta'] < 0.60  # r=0.04 pushes ATM call delta to ~0.584
    assert g['vega'] > 0


def test_iv_round_trip():
    price = bs_price('c', 100, 100, 0.5, 0.30)
    iv = implied_vol(price, 'c', 100, 100, 0.5)
    assert abs(iv - 0.30) < 1e-3


def test_strike_for_target_delta_calls_otm():
    K = strike_for_target_delta('c', S=100, t=30/365, sigma=0.2, target_delta=0.30)
    assert K > 100
    g = bs_greeks('c', 100, K, 30/365, 0.2)
    assert abs(g['delta'] - 0.30) < 0.03


def test_nearest_monthly_expiry_at_least_dte():
    exp = nearest_monthly_expiry(date(2024, 1, 2), dte_target=30)
    assert (exp - date(2024, 1, 2)).days >= 30
    assert exp.weekday() == 4  # Friday
