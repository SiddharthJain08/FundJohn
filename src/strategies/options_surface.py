# src/strategies/options_surface.py
"""Options surface features — ONE implementation for live and backtest.

Spec: docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md Part A.
Pure functions over a single ticker's chain rows for a single session. No
environment reads, no I/O, deterministic. Both engine.load_aux_data (live) and
scripts/build_options_surface.py (history) call these; the parity test pins
that they agree on every shared key.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator
from scipy.stats import norm

OPTIONS_FEATURES_VERSION = 2
FIT_DTE = (7, 120)          # expiries eligible for a smile fit
CHAIN_DTE = (1, 120)        # rows kept at all
FRONT_DTE_MAX = 45          # "front usable expiry" for greeks / iv_spread
ATM_DELTA = (0.40, 0.60)
CM_TARGETS = (30, 90)
CM_ONE_SIDED_TOL = 10
MIN_STRIKES = 5
IV_MIN = 0.01
DELTA_BAND = (0.05, 0.95)
_GREEKS = ('delta', 'gamma', 'theta', 'vega')
_D1_25_CALL = float(norm.ppf(0.25))   # −0.6745
_D1_25_PUT = float(norm.ppf(0.75))    # +0.6745

SCALAR_KEYS = [
    'spot', 'iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d',
    'ts_ratio', 'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm',
    'call_volume', 'put_volume', 'volume', 'pc_ratio', 'expiry_date',
    'n_expiries_fit', 'n_strikes_30d', 'options_features_version',
]


@dataclass
class SmileFit:
    dte: int
    t: float
    atm_iv: float
    iv_25d_put: float | None
    iv_25d_call: float | None
    n_strikes: int
    k_min: float
    k_max: float


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _mean(s) -> float | None:
    x = pd.to_numeric(s, errors='coerce').dropna()
    return float(x.mean()) if len(x) else None


def prepare_chain(df: pd.DataFrame, as_of) -> pd.DataFrame:
    """Shared filters (spec A.3): zero-greek rows dropped, option_type upper,
    dte attached, 1 ≤ dte ≤ 120. Returns a copy; never mutates the input."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(df.columns) + ['dte'] if df is not None else ['dte'])
    out = df.copy()
    as_of_ts = pd.Timestamp(as_of).normalize()
    out['expiry'] = pd.to_datetime(out['expiry'], errors='coerce')
    out = out.dropna(subset=['expiry'])
    present = [c for c in _GREEKS if c in out.columns]
    if present:
        out = out[~(out[present].fillna(0) == 0).all(axis=1)]
    out['option_type'] = out['option_type'].astype(str).str.upper()
    out['dte'] = (out['expiry'].dt.normalize() - as_of_ts).dt.days.astype(int)
    out = out[(out['dte'] >= CHAIN_DTE[0]) & (out['dte'] <= CHAIN_DTE[1])]
    return out


def _otm_side(exp_rows: pd.DataFrame, spot: float) -> pd.DataFrame:
    """One IV per strike: PUT below spot, CALL above, mean of both at spot."""
    r = exp_rows.copy()
    r['iv'] = pd.to_numeric(r['implied_volatility'], errors='coerce')
    r = r[r['iv'] > IV_MIN]
    if 'delta' in r.columns:
        d = pd.to_numeric(r['delta'], errors='coerce').abs()
        r = r[(d.isna()) | (d == 0) | ((d >= DELTA_BAND[0]) & (d <= DELTA_BAND[1]))]
    r['strike'] = pd.to_numeric(r['strike'], errors='coerce')
    r = r.dropna(subset=['strike'])
    side = np.where(r['strike'] < spot, 'PUT', np.where(r['strike'] > spot, 'CALL', 'BOTH'))
    keep = (side == 'BOTH') | (r['option_type'].to_numpy() == side)
    r = r[keep]
    return r.groupby('strike', as_index=False)['iv'].mean().sort_values('strike')


def _moneyness_for_delta(smile, t: float, k_min: float, k_max: float, d1: float, sigma0: float) -> float:
    x = 0.0
    sig = sigma0
    for _ in range(3):
        x = -d1 * sig * math.sqrt(t) + 0.5 * sig * sig * t
        x = min(max(x, k_min), k_max)
        sig = float(smile(x))
        if not (sig > 0):
            sig = sigma0
    return x


def fit_smile(strikes, ivs, spot: float, dte: int) -> SmileFit | None:
    K = np.asarray(strikes, dtype=float)
    iv = np.asarray(ivs, dtype=float)
    ok = np.isfinite(K) & np.isfinite(iv) & (K > 0) & (iv > IV_MIN)
    K, iv = K[ok], iv[ok]
    if len(K) < MIN_STRIKES or not (spot and spot > 0):
        return None
    order = np.argsort(K)
    K, iv = K[order], iv[order]
    k = np.log(K / spot)
    if not (k[0] < 0.0 < k[-1]):
        return None
    smile = PchipInterpolator(k, iv, extrapolate=False)
    t = dte / 365.0
    atm = float(smile(0.0))
    if not (atm > 0):
        return None
    xp = _moneyness_for_delta(smile, t, k[0], k[-1], _D1_25_PUT, atm)
    xc = _moneyness_for_delta(smile, t, k[0], k[-1], _D1_25_CALL, atm)
    ivp = _f(smile(xp))
    ivc = _f(smile(xc))
    return SmileFit(dte=int(dte), t=t, atm_iv=atm, iv_25d_put=ivp, iv_25d_call=ivc,
                    n_strikes=int(len(K)), k_min=float(k[0]), k_max=float(k[-1]))


def constant_maturity(fits: dict, target_dte: int, attr: str) -> float | None:
    pts = sorted((f.dte, getattr(f, attr)) for f in fits.values() if getattr(f, attr) is not None)
    if not pts:
        return None
    for d, v in pts:
        if d == target_dte:
            return float(v)
    lower = [(d, v) for d, v in pts if d < target_dte]
    upper = [(d, v) for d, v in pts if d > target_dte]
    if lower and upper:
        d1, v1 = lower[-1]
        d2, v2 = upper[0]
        t1, t2, tt = d1 / 365.0, d2 / 365.0, target_dte / 365.0
        w1, w2 = v1 * v1 * t1, v2 * v2 * t2
        wt = w1 + (w2 - w1) * (tt - t1) / (t2 - t1)
        return float(math.sqrt(max(wt, 0.0) / tt)) if wt > 0 else None
    d, v = (lower[-1] if lower else upper[0])
    return float(v) if abs(d - target_dte) <= CM_ONE_SIDED_TOL else None


def _empty_row(spot, as_of) -> dict:
    row = {k: None for k in SCALAR_KEYS}
    row.update({'spot': _f(spot), 'last_price': _f(spot), 'near_iv': None, 'far_iv': None,
                'skew_20d': None, 'call_volume': 0.0, 'put_volume': 0.0, 'volume': 0.0,
                'n_expiries_fit': 0, 'n_strikes_30d': 0,
                'options_features_version': OPTIONS_FEATURES_VERSION})
    return row


def features_for_day(chain: pd.DataFrame, spot, as_of) -> dict:
    """Per-(ticker, session) surface features (spec A.4).

    The chain is always re-prepared against `as_of`, so a caller's earlier
    `prepare_chain` is harmless and a mismatched as_of cannot leak into
    `expiry_date`.
    """
    row = _empty_row(spot, as_of)
    if chain is None or len(chain) == 0:
        return row
    ch = prepare_chain(chain, as_of)
    if len(ch) == 0:
        return row
    calls = ch[ch['option_type'] == 'CALL']
    puts = ch[ch['option_type'] == 'PUT']
    cv = float(pd.to_numeric(calls.get('volume'), errors='coerce').fillna(0).sum()) if 'volume' in ch.columns else 0.0
    pv = float(pd.to_numeric(puts.get('volume'), errors='coerce').fillna(0).sum()) if 'volume' in ch.columns else 0.0
    row.update({'call_volume': cv, 'put_volume': pv, 'volume': cv + pv,
                'pc_ratio': (pv / cv) if cv > 0 else None})
    # Front usable expiry: greeks + iv_spread on the raw ATM band.
    front = ch[ch['dte'] <= FRONT_DTE_MAX]
    if front.empty:
        front = ch
    front_dte = int(front['dte'].min())
    fr = front[front['dte'] == front_dte]
    row['expiry_date'] = (pd.Timestamp(as_of).normalize() + pd.Timedelta(days=front_dte)).date().isoformat()
    if 'delta' in fr.columns:
        d = pd.to_numeric(fr['delta'], errors='coerce').abs()
        atm = fr[(d >= ATM_DELTA[0]) & (d <= ATM_DELTA[1])]
        row['gamma_atm'] = _mean(atm['gamma']) if 'gamma' in atm.columns else None
        row['theta_atm'] = _mean(atm['theta']) if 'theta' in atm.columns else None
        ca = _mean(atm[atm['option_type'] == 'CALL']['implied_volatility'])
        pa = _mean(atm[atm['option_type'] == 'PUT']['implied_volatility'])
        row['iv_spread'] = (ca - pa) if (ca is not None and pa is not None) else None
    spot_f = _f(spot)
    if not (spot_f and spot_f > 0):
        return row
    fits: dict[int, SmileFit] = {}
    for dte, exp_rows in ch[(ch['dte'] >= FIT_DTE[0]) & (ch['dte'] <= FIT_DTE[1])].groupby('dte'):
        side = _otm_side(exp_rows, spot_f)
        fit = fit_smile(side['strike'].to_numpy(), side['iv'].to_numpy(), spot_f, int(dte))
        if fit is not None:
            fits[int(dte)] = fit
    row['n_expiries_fit'] = len(fits)
    if not fits:
        return row
    iv30 = constant_maturity(fits, 30, 'atm_iv')
    iv90 = constant_maturity(fits, 90, 'atm_iv')
    p30 = constant_maturity(fits, 30, 'iv_25d_put')
    c30 = constant_maturity(fits, 30, 'iv_25d_call')
    near30 = min(fits, key=lambda d: abs(d - 30))
    row.update({
        'iv30': iv30, 'iv90': iv90, 'near_iv': iv30, 'far_iv': iv90,
        'ts_ratio': (iv30 / iv90) if (iv30 is not None and iv90 is not None and iv90 > 0) else None,
        'term_slope': (iv90 - iv30) if (iv30 is not None and iv90 is not None) else None,
        'iv_25d_put_30d': p30, 'iv_25d_call_30d': c30,
        'skew_25d_30d': (p30 - iv30) if (p30 is not None and iv30 is not None) else None,
        'rr_25d_30d': (p30 - c30) if (p30 is not None and c30 is not None) else None,
        'n_strikes_30d': fits[near30].n_strikes,
    })
    row['skew_20d'] = row['skew_25d_30d']
    return row
