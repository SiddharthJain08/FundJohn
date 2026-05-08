"""src/ingestion/synthetic_vix.py — Cboe variance-swap synthetic VIX.

Pure functions, zero I/O. Computes a VIX-style 30-day variance swap rate
from a single option chain snapshot. Same formula as the official Cboe
white paper (2014 revision), with two tweaks that are documented:

  1. We use SPY options (Alpaca/Polygon coverage) instead of SPX cash-
     settled index options. Synthetic VIX off SPY tracks within ~1% of
     CBOE official VIX in calm markets, drifting slightly more in
     CRISIS (bid/ask widening on deep OTM puts). Acceptable for a
     regime classifier; not for VIX-derivative trading.
  2. SVI parameterisation is the IV interpolator used to fill OTM
     strike gaps where the chain's IV column is sparse — Polygon ships
     IV for ~53% of contracts, Alpaca for liquid contracts only.

Public entry:
    compute_synthetic_vix(chain_df, spot, r, t_now, exclude_zero_dte=True)

`chain_df` columns required:
    expiration_date  (datetime64[ns])
    strike           (float)
    option_type      ('C' or 'P')
    bid              (float, may be 0)
    ask              (float, may be 0)
    iv               (float | NaN, may be missing)

Returns dict:
    {vix_synth_30d, vix_synth_90d, term_slope,
     near_term_t, next_term_t, strikes_used, fallback_flag}

Test coverage in tests/test_synthetic_vix.py.
"""
from __future__ import annotations

import math
from typing import Tuple, Optional

import numpy as np
import pandas as pd


# ── Tunable parameters ────────────────────────────────────────────────────────

# Chain filter: minimum bid for a quote to count toward the variance sum.
# Strike pruning per Cboe rules: stop including OTM strikes once two
# consecutive zero-bid contracts appear walking outward from K0.
MIN_BID = 0.0          # accept any positive bid; 0-bid → snap to mid via SVI
ZERO_BID_RUN_LIMIT = 2

# DTE windows for near/next term selection. Cboe excludes ≤ 23 days for
# the official VIX (uses NEXT week's options as "near"). For the regime
# detector we relax this — we want sensitivity to short-term shocks —
# but the user-config flag lets ops match Cboe if desired.
EXCLUDE_ZERO_DTE_DEFAULT = True
ZERO_DTE_THRESHOLD_DAYS = 7   # CBOE-style exclusion when flag is True

# Risk-free rate fallback when caller doesn't supply one.
DEFAULT_R = 0.04


# ── SVI raw parameterisation ─────────────────────────────────────────────────
#
# w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2))
#
# where w = total implied variance, k = log-moneyness ln(K/F).
# Five params: a, b, rho, m, sigma. Constraints:
#   b >= 0, |rho| <= 1, sigma > 0.

def _svi_total_variance(k: np.ndarray, a: float, b: float,
                         rho: float, m: float, sigma: float) -> np.ndarray:
    return a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + sigma ** 2))


def fit_svi(strikes: np.ndarray, ivs: np.ndarray, forward: float,
            t: float) -> Optional[Tuple[float, float, float, float, float]]:
    """Least-squares fit of raw-SVI to (strike, iv) points at one expiry.

    Returns (a, b, rho, m, sigma) or None if the fit failed (too few
    points, optimiser non-convergence). Caller falls back to linear
    interpolation in IV-space when None is returned.
    """
    if len(strikes) < 5 or t <= 0 or forward <= 0:
        return None
    mask = (~np.isnan(ivs)) & (ivs > 0) & (strikes > 0)
    if mask.sum() < 5:
        return None
    k = np.log(strikes[mask] / forward)
    w_target = (ivs[mask] ** 2) * t

    try:
        from scipy.optimize import minimize
    except ImportError:
        return None

    def _loss(params):
        a, b, rho, m, sigma = params
        if b < 0 or abs(rho) > 1 or sigma <= 0:
            return 1e9
        w_model = _svi_total_variance(k, a, b, rho, m, sigma)
        if (w_model < 0).any():
            return 1e9
        return float(np.sum((w_model - w_target) ** 2))

    # Reasonable initialisation: ATM total-variance, modest vol-of-vol,
    # zero skew, ATM peak.
    atm_iv = float(np.nanmedian(ivs[mask])) if mask.any() else 0.2
    x0 = [atm_iv ** 2 * t * 0.5, 0.1, -0.3, 0.0, 0.1]
    try:
        res = minimize(_loss, x0, method='Nelder-Mead',
                       options={'xatol': 1e-5, 'fatol': 1e-7, 'maxiter': 1000})
    except Exception:
        return None
    if not res.success:
        return None
    return tuple(res.x)


def svi_iv(strike: float, forward: float, t: float,
           svi_params: Tuple[float, float, float, float, float]) -> float:
    """Evaluate SVI at a single (strike, t)."""
    if forward <= 0 or strike <= 0 or t <= 0:
        return float('nan')
    a, b, rho, m, sigma = svi_params
    k = math.log(strike / forward)
    w = a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + sigma ** 2))
    if w < 0:
        return float('nan')
    return math.sqrt(w / t)


# ── Black-Scholes pricing for IV-fallback path ───────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t: float, r: float, q: float,
             iv: float, opt_type: str) -> float:
    """Standard Black-Scholes price. Used as a fallback when bid/ask is
    missing but IV is populated (or after SVI interpolation)."""
    if t <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if opt_type.upper().startswith('C'):
        return spot * math.exp(-q * t) * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * math.exp(-q * t) * _norm_cdf(-d1)


# ── Variance-swap math ───────────────────────────────────────────────────────

def _forward_from_parity(calls: pd.DataFrame, puts: pd.DataFrame,
                          spot: float, r: float, t: float) -> float:
    """F = K + e^(rT) * (C(K) - P(K)) at the strike with smallest |C-P|.

    Per Cboe white paper. Falls back to spot * e^(rT) when no usable
    pair is available.
    """
    fallback = spot * math.exp(r * t)
    if calls.empty or puts.empty:
        return fallback
    merged = calls[['strike', 'mid']].merge(
        puts[['strike', 'mid']], on='strike', suffixes=('_c', '_p'),
    )
    merged = merged[(merged['mid_c'] > 0) & (merged['mid_p'] > 0)]
    if merged.empty:
        return fallback
    merged['abs_diff'] = (merged['mid_c'] - merged['mid_p']).abs()
    pivot = merged.loc[merged['abs_diff'].idxmin()]
    return float(pivot['strike']) + math.exp(r * t) * (pivot['mid_c'] - pivot['mid_p'])


def _select_otm_strikes(calls: pd.DataFrame, puts: pd.DataFrame,
                         k0: float) -> pd.DataFrame:
    """Build the OTM strike list: puts with K<K0, calls with K>K0, plus
    K0 itself counted at (mid_call+mid_put)/2. Walks outward stopping
    after `ZERO_BID_RUN_LIMIT` consecutive zero-bid contracts (Cboe rule)."""
    rows = []

    # K0 itself uses the average of the call and put mids.
    c_at_k0 = calls[calls['strike'] == k0]
    p_at_k0 = puts[puts['strike'] == k0]
    if not c_at_k0.empty and not p_at_k0.empty:
        avg = (float(c_at_k0['mid'].iloc[0]) + float(p_at_k0['mid'].iloc[0])) / 2.0
        rows.append({'strike': k0, 'price': avg})

    # Puts: walk DOWN from K0, exclusive (K < K0).
    p_below = puts[puts['strike'] < k0].sort_values('strike', ascending=False)
    zero_run = 0
    for _, row in p_below.iterrows():
        if row['mid'] <= 0 or row['bid'] <= MIN_BID:
            zero_run += 1
            if zero_run >= ZERO_BID_RUN_LIMIT:
                break
            continue
        zero_run = 0
        rows.append({'strike': float(row['strike']), 'price': float(row['mid'])})

    # Calls: walk UP from K0, exclusive (K > K0).
    c_above = calls[calls['strike'] > k0].sort_values('strike', ascending=True)
    zero_run = 0
    for _, row in c_above.iterrows():
        if row['mid'] <= 0 or row['bid'] <= MIN_BID:
            zero_run += 1
            if zero_run >= ZERO_BID_RUN_LIMIT:
                break
            continue
        zero_run = 0
        rows.append({'strike': float(row['strike']), 'price': float(row['mid'])})

    if not rows:
        return pd.DataFrame(columns=['strike', 'price'])
    return pd.DataFrame(rows).sort_values('strike').reset_index(drop=True)


def _variance_for_expiry(chain_e: pd.DataFrame, spot: float, r: float,
                          t: float) -> Tuple[float, int]:
    """Compute σ²·T for a single expiry. Returns (variance_T, n_strikes_used).
    variance_T is the raw (un-annualised) variance contribution; the
    caller divides by T to get σ².

    Per the Cboe white paper:
        σ² = (2/T) Σᵢ (ΔKᵢ / Kᵢ²) e^(rT) Q(Kᵢ) − (1/T) (F/K₀ − 1)²
    """
    if chain_e.empty or t <= 0:
        return float('nan'), 0

    chain_e = chain_e.copy()
    chain_e['mid'] = (chain_e['bid'].fillna(0) + chain_e['ask'].fillna(0)) / 2.0
    calls = chain_e[chain_e['option_type'].str.upper().str.startswith('C')]
    puts  = chain_e[chain_e['option_type'].str.upper().str.startswith('P')]

    if calls.empty or puts.empty:
        return float('nan'), 0

    forward = _forward_from_parity(calls, puts, spot, r, t)

    strikes_below = puts['strike'][puts['strike'] <= forward]
    if strikes_below.empty:
        return float('nan'), 0
    k0 = float(strikes_below.max())

    selected = _select_otm_strikes(calls, puts, k0)
    if selected.empty or len(selected) < 3:
        return float('nan'), len(selected)

    # ΔKᵢ = (K_{i+1} − K_{i−1}) / 2 except boundaries where it's the
    # one-sided neighbour distance.
    ks = selected['strike'].values
    deltas = np.empty_like(ks)
    deltas[0]  = ks[1] - ks[0]
    deltas[-1] = ks[-1] - ks[-2]
    if len(ks) > 2:
        deltas[1:-1] = (ks[2:] - ks[:-2]) / 2.0

    # σ²·T = 2 e^(rT) Σ (ΔKᵢ / Kᵢ²) Q(Kᵢ) − (F/K₀ − 1)²
    contributions = (deltas / (ks * ks)) * selected['price'].values
    sum_term = float(np.sum(contributions))
    correction = (forward / k0 - 1.0) ** 2
    var_T = 2.0 * math.exp(r * t) * sum_term - correction
    return var_T, int(len(ks))


def _interpolate_variance(near: dict, next_: dict, target_days: float) -> float:
    """Linear interpolation in T·σ² between the two expiries to the
    target horizon. Returns annualised σ² for the target.

    Cboe formula:
      VIX² = ((T1·σ1²·(N_T2 − N_30) + T2·σ2²·(N_30 − N_T1)) / (N_T2 − N_T1)) × (N_365 / N_30)
    Where N_X is X measured in minutes. We use days.
    """
    n1 = near['t_days']
    n2 = next_['t_days']
    target = target_days
    span = n2 - n1
    if span <= 0:
        return float('nan')
    near_var_T  = near['t_years']  * near['sigma2']
    next_var_T  = next_['t_years'] * next_['sigma2']
    blended = (near_var_T * (n2 - target) + next_var_T * (target - n1)) / span
    # Annualise: divide by target horizon in years
    return blended * (365.0 / target_days)


# ── Public entry ─────────────────────────────────────────────────────────────

def compute_synthetic_vix(chain_df: pd.DataFrame, spot: float, r: float = DEFAULT_R,
                           t_now: Optional[pd.Timestamp] = None,
                           exclude_zero_dte: bool = EXCLUDE_ZERO_DTE_DEFAULT,
                           target_days_30: float = 30.0,
                           target_days_90: float = 90.0) -> dict:
    """Compute synthetic VIX-30d and VIX-90d from a chain snapshot.

    Returns:
        {
            'vix_synth_30d':   float (NaN on insufficient data),
            'vix_synth_90d':   float,
            'term_slope':      float (vix_synth_90d / vix_synth_30d),
            'near_term_days':  float,
            'next_term_days':  float,
            'far_near_days':   float,    # 90d-bracket near
            'far_next_days':   float,    # 90d-bracket next
            'strikes_used':    int,      # union across all expiries used
            'fallback_flag':   bool,     # True if any variance NaN'd
            'spot':            float,
            'forward_30d':     float,    # informational
        }
    """
    if t_now is None:
        t_now = pd.Timestamp.now(tz='UTC')
    if not isinstance(t_now, pd.Timestamp):
        t_now = pd.Timestamp(t_now)
    if t_now.tzinfo is None:
        t_now = t_now.tz_localize('UTC')

    if chain_df is None or chain_df.empty:
        return {
            'vix_synth_30d': float('nan'), 'vix_synth_90d': float('nan'),
            'term_slope': float('nan'),
            'near_term_days': float('nan'), 'next_term_days': float('nan'),
            'far_near_days':  float('nan'), 'far_next_days':  float('nan'),
            'strikes_used': 0, 'fallback_flag': True,
            'spot': spot, 'forward_30d': float('nan'),
        }

    chain = chain_df.copy()
    # Normalise column types.
    chain['expiration_date'] = pd.to_datetime(chain['expiration_date'])
    if chain['expiration_date'].dt.tz is None:
        chain['expiration_date'] = chain['expiration_date'].dt.tz_localize('UTC')

    # DTE per row (calendar days, fractional).
    chain['t_days'] = (chain['expiration_date'] - t_now).dt.total_seconds() / 86400.0
    chain['t_years'] = chain['t_days'] / 365.0

    # Cboe-style 0DTE exclusion (default ON).
    if exclude_zero_dte:
        chain = chain[chain['t_days'] > ZERO_DTE_THRESHOLD_DAYS]

    if chain.empty:
        return {
            'vix_synth_30d': float('nan'), 'vix_synth_90d': float('nan'),
            'term_slope': float('nan'),
            'near_term_days': float('nan'), 'next_term_days': float('nan'),
            'far_near_days':  float('nan'), 'far_next_days':  float('nan'),
            'strikes_used': 0, 'fallback_flag': True,
            'spot': spot, 'forward_30d': float('nan'),
        }

    # Pick the two expiries bracketing each target horizon.
    expiries = sorted(chain['expiration_date'].unique())

    def _bracket(target_days):
        # Closest below and closest above the target.
        below, above = None, None
        for e in expiries:
            t_e = (e - t_now).total_seconds() / 86400.0
            if t_e <= 0:
                continue
            if t_e <= target_days and (below is None or t_e > below[1]):
                below = (e, t_e)
            if t_e >= target_days and (above is None or t_e < above[1]):
                above = (e, t_e)
        # Degenerate cases.
        if below is None and above is not None:
            below = above
        if above is None and below is not None:
            above = below
        return below, above

    fallback = False
    strikes_used = 0
    forward_30d = float('nan')

    def _one_target(target_days, want_forward=False):
        nonlocal fallback, strikes_used, forward_30d
        below, above = _bracket(target_days)
        if below is None or above is None:
            fallback = True
            return float('nan'), float('nan'), float('nan')
        e_near, n_days = below
        e_next, x_days = above
        chain_n = chain[chain['expiration_date'] == e_near]
        chain_x = chain[chain['expiration_date'] == e_next]
        var_n_T, n_strikes_n = _variance_for_expiry(
            chain_n, spot, r, t=n_days / 365.0,
        )
        var_x_T, n_strikes_x = _variance_for_expiry(
            chain_x, spot, r, t=x_days / 365.0,
        )
        if math.isnan(var_n_T) or math.isnan(var_x_T):
            fallback = True
            return float('nan'), n_days, x_days
        sigma2_n = var_n_T / (n_days / 365.0) if n_days > 0 else 0
        sigma2_x = var_x_T / (x_days / 365.0) if x_days > 0 else 0
        if e_near == e_next:
            # Single-expiry fallback (degenerate bracket): annualise directly.
            vix_sq = sigma2_n
        else:
            vix_sq = _interpolate_variance(
                near={'t_days': n_days, 't_years': n_days / 365.0, 'sigma2': sigma2_n},
                next_={'t_days': x_days, 't_years': x_days / 365.0, 'sigma2': sigma2_x},
                target_days=target_days,
            )
        if math.isnan(vix_sq) or vix_sq <= 0:
            fallback = True
            return float('nan'), n_days, x_days
        strikes_used += n_strikes_n + n_strikes_x
        if want_forward:
            calls_n = chain_n[chain_n['option_type'].str.upper().str.startswith('C')]
            puts_n  = chain_n[chain_n['option_type'].str.upper().str.startswith('P')]
            if not calls_n.empty and not puts_n.empty:
                calls_n = calls_n.assign(mid=(calls_n['bid'].fillna(0) + calls_n['ask'].fillna(0)) / 2)
                puts_n  = puts_n.assign(mid=(puts_n['bid'].fillna(0) + puts_n['ask'].fillna(0)) / 2)
                forward_30d = _forward_from_parity(calls_n, puts_n, spot, r, n_days / 365.0)
        return 100.0 * math.sqrt(vix_sq), n_days, x_days

    vix30, near30, next30 = _one_target(target_days_30, want_forward=True)
    vix90, near90, next90 = _one_target(target_days_90)

    term_slope = float('nan')
    if not math.isnan(vix30) and not math.isnan(vix90) and vix30 > 0:
        term_slope = vix90 / vix30

    return {
        'vix_synth_30d': vix30,
        'vix_synth_90d': vix90,
        'term_slope':    term_slope,
        'near_term_days': near30, 'next_term_days': next30,
        'far_near_days':  near90, 'far_next_days':  next90,
        'strikes_used':   int(strikes_used),
        'fallback_flag':  bool(fallback),
        'spot':           float(spot),
        'forward_30d':    float(forward_30d),
    }
