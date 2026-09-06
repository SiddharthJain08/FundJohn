"""Black-Scholes(-Merton) pricing/greeks wrapper over py_vollib plus a CRR
American tree for the synthetic options backtest engine (SP-4 Phase 0;
spec 2026-09-06 B.2). All functions are pure + deterministic.

Rate: `as_of=None` keeps the legacy flat RISK_FREE (4 %); with `as_of` the
rate comes from backtest.risk_free (DGS3MO behind OPENCLAW_RF_SOURCE).
Dividends: `q` (continuous yield) defaults to 0.0, and the q == 0 path calls
the SAME py_vollib black_scholes functions as before — bit-identical.
American exercise: CRR binomial tree (ruling G7), delta by central difference.
"""
from __future__ import annotations
import math
from datetime import date, timedelta
import numpy as np
from scipy.optimize import brentq
from py_vollib.black_scholes import black_scholes as _bs
from py_vollib.black_scholes.greeks import analytical as _greeks
from py_vollib.black_scholes_merton import black_scholes_merton as _bsm
from py_vollib.black_scholes_merton.greeks import analytical as _bsm_greeks

RISK_FREE = 0.04  # flat annual risk-free when as_of is None; see module docstring
AMERICAN_STEPS = 200
EXERCISES = ('european', 'american')


def _rate(r, as_of):
    if as_of is None:
        return RISK_FREE if r is None else r
    from backtest.risk_free import rf_annual_asof
    return rf_annual_asof(as_of) if r is None else r


def _clean(t, sigma):
    return max(float(t), 1e-6), max(float(sigma), 1e-4)


def bs_price(flag: str, S: float, K: float, t: float, sigma: float,
             r: float | None = None, as_of=None, q: float = 0.0) -> float:
    """flag 'c'|'p'; t in years; q = continuous dividend yield. Guards degenerate t/sigma."""
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    if q:
        return float(_bsm(flag, float(S), float(K), t, r, sigma, float(q)))
    return float(_bs(flag, float(S), float(K), t, r, sigma))


def bs_greeks(flag: str, S: float, K: float, t: float, sigma: float,
              r: float | None = None, as_of=None, q: float = 0.0) -> dict:
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    if q:
        g, args = _bsm_greeks, (flag, S, K, t, r, sigma, float(q))
    else:
        g, args = _greeks, (flag, S, K, t, r, sigma)
    return {
        'delta': float(g.delta(*args)),
        'gamma': float(g.gamma(*args)),
        'theta': float(g.theta(*args)),
        'vega':  float(g.vega(*args)),
    }


def implied_vol(price: float, flag: str, S: float, K: float, t: float,
                r: float | None = None, as_of=None, q: float = 0.0) -> float:
    r = _rate(r, as_of)
    if q:
        from py_vollib.black_scholes_merton.implied_volatility import implied_volatility
        return float(implied_volatility(float(price), float(S), float(K),
                                        max(float(t), 1e-6), r, float(q), flag))
    from py_vollib.black_scholes.implied_volatility import implied_volatility
    return float(implied_volatility(float(price), float(S), float(K),
                                    max(float(t), 1e-6), r, flag))


def strike_for_target_delta(flag: str, S: float, t: float, sigma: float,
                            target_delta: float, r: float | None = None, as_of=None,
                            q: float = 0.0) -> float:
    """Solve for the strike whose |delta| == target_delta at (S, t, sigma).
    Calls: strike increases as delta decreases (OTM). Puts: |delta|.
    """
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    td = abs(float(target_delta))

    def f(K):
        return abs(bs_greeks(flag, S, K, t, sigma, r=r, q=q)['delta']) - td

    lo, hi = S * 0.30, S * 3.0
    try:
        return float(brentq(f, lo, hi, maxiter=100, xtol=1e-4))
    except ValueError:
        return float(S)


def american_price(flag: str, S: float, K: float, t: float, sigma: float,
                   r: float | None = None, as_of=None, q: float = 0.0,
                   steps: int = AMERICAN_STEPS) -> float:
    """Cox–Ross–Rubinstein binomial tree (ruling G7). A call on a non-dividend
    payer is never exercised early ⇒ its European price, exactly and cheaply."""
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    S, K, q = float(S), float(K), float(q)
    is_call = flag == 'c'
    if is_call and q <= 0.0:
        return bs_price('c', S, K, t, sigma, r=r)
    n = max(int(steps), 1)
    dt = t / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    p = (math.exp((r - q) * dt) - d) / (u - d)
    p = min(max(p, 0.0), 1.0)
    disc = math.exp(-r * dt)
    j = np.arange(n + 1)
    ST = S * u ** (2 * j - n)
    V = np.maximum(ST - K, 0.0) if is_call else np.maximum(K - ST, 0.0)
    for i in range(n - 1, -1, -1):
        ST = S * u ** (2 * np.arange(i + 1) - i)
        V = disc * (p * V[1:] + (1.0 - p) * V[:-1])
        ex = np.maximum(ST - K, 0.0) if is_call else np.maximum(K - ST, 0.0)
        V = np.maximum(V, ex)
    return float(V[0])


def american_delta(flag: str, S: float, K: float, t: float, sigma: float,
                   r: float | None = None, as_of=None, q: float = 0.0,
                   steps: int = AMERICAN_STEPS) -> float:
    h = max(1e-3 * float(S), 1e-6)
    up = american_price(flag, float(S) + h, K, t, sigma, r=r, as_of=as_of, q=q, steps=steps)
    dn = american_price(flag, float(S) - h, K, t, sigma, r=r, as_of=as_of, q=q, steps=steps)
    return float((up - dn) / (2.0 * h))


def _check_exercise(exercise: str) -> str:
    if exercise not in EXERCISES:
        raise ValueError(f'exercise must be one of {EXERCISES}, got {exercise!r}')
    return exercise


def price(flag: str, S: float, K: float, t: float, sigma: float, r: float | None = None,
          as_of=None, q: float = 0.0, exercise: str = 'european') -> float:
    if _check_exercise(exercise) == 'american':
        return american_price(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)
    return bs_price(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)


def delta(flag: str, S: float, K: float, t: float, sigma: float, r: float | None = None,
          as_of=None, q: float = 0.0, exercise: str = 'european') -> float:
    if _check_exercise(exercise) == 'american':
        return american_delta(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)
    return bs_greeks(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)['delta']


def nearest_monthly_expiry(as_of: date, dte_target: int) -> date:
    """Nearest standard monthly expiry at least `dte_target` calendar days after
    as_of. The listed expiry is the third Friday, or the last session before it
    when that Friday is an exchange holiday (Good Friday 2019-04-19 → 04-18)."""
    from lib.trading_calendar import expiry_session

    def third_friday(year: int, month: int) -> date:
        d = date(year, month, 1)
        offset = (4 - d.weekday()) % 7
        first_friday = d + timedelta(days=offset)
        return first_friday + timedelta(days=14)

    earliest = as_of + timedelta(days=int(dte_target))
    y, m = as_of.year, as_of.month
    for _ in range(18):
        tf = expiry_session(third_friday(y, m))
        if tf >= earliest:
            return tf
        m += 1
        if m > 12:
            m = 1; y += 1
    return expiry_session(third_friday(y, m))
