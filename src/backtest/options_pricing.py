"""Black-Scholes pricing/greeks wrapper over py_vollib for the synthetic
options backtest engine (SP-4 Phase 0). All functions are pure + deterministic.
Risk-free rate is a fixed constant — the realized-vol×VRP IV model dominates
pricing error far more than the short-rate, and we have no clean historical
short-rate series wired in. Revisit if parity (scripts/options_parity_check.py)
shows rate sensitivity matters.
"""
from __future__ import annotations
from datetime import date, timedelta
from scipy.optimize import brentq
from py_vollib.black_scholes import black_scholes as _bs
from py_vollib.black_scholes.greeks import analytical as _greeks

RISK_FREE = 0.04  # flat annual risk-free; see module docstring


def bs_price(flag: str, S: float, K: float, t: float, sigma: float,
             r: float = RISK_FREE) -> float:
    """flag 'c'|'p'; t in years. Guards degenerate t/sigma."""
    t = max(float(t), 1e-6)
    sigma = max(float(sigma), 1e-4)
    return float(_bs(flag, float(S), float(K), t, r, sigma))


def bs_greeks(flag: str, S: float, K: float, t: float, sigma: float,
              r: float = RISK_FREE) -> dict:
    t = max(float(t), 1e-6)
    sigma = max(float(sigma), 1e-4)
    return {
        'delta': float(_greeks.delta(flag, S, K, t, r, sigma)),
        'gamma': float(_greeks.gamma(flag, S, K, t, r, sigma)),
        'theta': float(_greeks.theta(flag, S, K, t, r, sigma)),
        'vega':  float(_greeks.vega(flag, S, K, t, r, sigma)),
    }


def implied_vol(price: float, flag: str, S: float, K: float, t: float,
                r: float = RISK_FREE) -> float:
    from py_vollib.black_scholes.implied_volatility import implied_volatility
    return float(implied_volatility(float(price), float(S), float(K),
                                    max(float(t), 1e-6), r, flag))


def strike_for_target_delta(flag: str, S: float, t: float, sigma: float,
                            target_delta: float, r: float = RISK_FREE) -> float:
    """Solve for the strike whose |delta| == target_delta at (S, t, sigma).
    Calls: strike increases as delta decreases (OTM). Puts: |delta|.
    """
    t = max(float(t), 1e-6)
    sigma = max(float(sigma), 1e-4)
    td = abs(float(target_delta))

    def f(K):
        return abs(_greeks.delta(flag, S, K, t, r, sigma)) - td

    lo, hi = S * 0.30, S * 3.0
    try:
        return float(brentq(f, lo, hi, maxiter=100, xtol=1e-4))
    except ValueError:
        return float(S)


def nearest_monthly_expiry(as_of: date, dte_target: int) -> date:
    """Nearest standard monthly expiry (3rd Friday) at least `dte_target`
    calendar days after as_of."""
    def third_friday(year: int, month: int) -> date:
        d = date(year, month, 1)
        offset = (4 - d.weekday()) % 7
        first_friday = d + timedelta(days=offset)
        return first_friday + timedelta(days=14)

    earliest = as_of + timedelta(days=int(dte_target))
    y, m = as_of.year, as_of.month
    for _ in range(18):
        tf = third_friday(y, m)
        if tf >= earliest:
            return tf
        m += 1
        if m > 12:
            m = 1; y += 1
    return third_friday(y, m)
