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
from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator
from scipy.stats import norm

OPTIONS_FEATURES_VERSION = 3
FIT_DTE = (7, 120)          # expiries eligible for a smile fit
CHAIN_DTE = (1, 120)        # rows kept at all
FRONT_DTE_MAX = 45          # "front usable expiry" for greeks / iv_spread
ATM_DELTA = (0.40, 0.60)
CM_TARGETS = (30, 90)
CM_ONE_SIDED_TOL = 20   # was 10; amendment 2026-09-06 §H — a lone 14 d / 42 d monthly anchors the 30 d point
MIN_STRIKES = 5
IV_MIN = 0.01
DELTA_BAND = (0.05, 0.95)
_GREEKS = ('delta', 'gamma', 'theta', 'vega')
_D1_25_CALL = float(norm.ppf(0.25))   # −0.6745
_D1_25_PUT = float(norm.ppf(0.75))    # +0.6745

# Spec 2026-09-06 §A.1–A.2 — model-free strip on the fitted smile.
K_TRUNC = 5.0            # strip half-width in units of σ_atm·√T (ruling G2)
N_GRID = 401             # odd ⇒ k = 0 (the call/put switch) is a node
RN_TAIL_MOVE = 0.10      # ±10 % tail probabilities
RN_MOMENT_DTE_TOL = 15   # RN moments/tails from the expiry nearest 30 DTE within this (G4)

SCALAR_KEYS = [
    'spot', 'iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d',
    'ts_ratio', 'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm',
    'call_volume', 'put_volume', 'volume', 'pc_ratio', 'expiry_date',
    'n_expiries_fit', 'n_strikes_30d', 'options_features_version',
    # v3 (spec 2026-09-06 A.3): model-free variance + risk-neutral density
    'mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d',
    'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d',
    # amendment 2026-09-06 §H: thin-chain fallback
    'iv30_source', 'n_expiries_atm',
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
    mfiv: float | None = None        # model-free implied vol √(V/T) — spec 2026-09-06 A.2
    rn_skew: float | None = None     # BKM risk-neutral skewness of ln(S_T/F)
    rn_kurt: float | None = None     # BKM risk-neutral kurtosis (raw; 3 = lognormal)
    rn_p_dn10: float | None = None   # RN P(S_T ≤ 0.9·F)
    rn_p_up10: float | None = None   # RN P(S_T ≥ 1.1·F)
    source: str = 'smile'   # 'smile' | 'atm_band' (amendment §H)


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


def _sigma_on(smile, k, k_min: float, k_max: float, atm: float) -> np.ndarray:
    """Smile vol at log-moneyness k with FLAT extrapolation outside the observed
    strike range (ruling G1); non-finite or sub-floor values fall back to ATM."""
    s = np.asarray(smile(np.clip(np.asarray(k, dtype=float), k_min, k_max)), dtype=float)
    return np.where(np.isfinite(s) & (s > IV_MIN), s, atm)


def _strip_prices(sig: np.ndarray, k: np.ndarray, t: float) -> np.ndarray:
    """Normalised, undiscounted OTM Black prices q(k) = Q/F with F = S, r = q = 0:
    call for k ≥ 0, put for k < 0 (spec A.1)."""
    st = sig * math.sqrt(t)
    d1 = (-k + 0.5 * sig * sig * t) / st
    d2 = d1 - st
    call = norm.cdf(d1) - np.exp(k) * norm.cdf(d2)
    put = np.exp(k) * norm.cdf(-d2) - norm.cdf(-d1)
    return np.maximum(np.where(k >= 0.0, call, put), 0.0)


def _tail_prob_below(smile, k: float, k_min: float, k_max: float, atm: float, t: float) -> float | None:
    """RN P(S_T ≤ F·e^k) from the smile-adjusted digital (spec A.2):
    Φ(−d2) + e^{−k} φ(d1) √T σ′(k), σ′ = 0 in the flat wings, clipped to [0, 1] (G3)."""
    sig = float(_sigma_on(smile, np.array([k]), k_min, k_max, atm)[0])
    st = sig * math.sqrt(t)
    d1 = (-k + 0.5 * sig * sig * t) / st
    d2 = d1 - st
    dsig = 0.0
    if k_min <= k <= k_max:
        try:
            dsig = float(smile.derivative()(k))
        except Exception:  # noqa: BLE001 — a callable without .derivative() ⇒ flat
            dsig = 0.0
        if not math.isfinite(dsig):
            dsig = 0.0
    p = float(norm.cdf(-d2)) + math.exp(-k) * float(norm.pdf(d1)) * math.sqrt(t) * dsig
    return float(min(max(p, 0.0), 1.0)) if math.isfinite(p) else None


_STRIP_NONE = {'mfiv': None, 'rn_skew': None, 'rn_kurt': None, 'rn_p_dn10': None, 'rn_p_up10': None}


def strip_features(smile, k_min: float, k_max: float, atm: float, t: float) -> dict:
    """Model-free implied variance (DDKZ/VIX integral), BKM (2003) risk-neutral
    skewness/kurtosis and ±10 % tail probabilities for ONE expiry, from its
    fitted smile (spec 2026-09-06 §A.1–A.2). In log-moneyness dK/K² = e^{−k}/F·dk,
    so every integral is ∫ weight(k)·q(k)·e^{−k} dk over a ±K_TRUNC·σ√T grid.
    Returns None values for anything not finite; never raises."""
    out = dict(_STRIP_NONE)
    try:
        if not (atm > 0.0 and t > 0.0):
            return out
        L = K_TRUNC * atm * math.sqrt(t)
        k = np.linspace(-L, L, N_GRID)
        q = _strip_prices(_sigma_on(smile, k, k_min, k_max, atm), k, t)
        w = q * np.exp(-k)
        var_total = 2.0 * float(np.trapezoid(w, k))            # variance-swap total variance
        if var_total > 0.0:
            out['mfiv'] = math.sqrt(var_total / t)
        v2 = 2.0 * float(np.trapezoid((1.0 - k) * w, k))        # BKM V  (E[x²])
        w3 = float(np.trapezoid((6.0 * k - 3.0 * k * k) * w, k))  # BKM W  (E[x³])
        x4 = float(np.trapezoid((12.0 * k * k - 4.0 * k ** 3) * w, k))  # BKM X (E[x⁴])
        mu = -v2 / 2.0 - w3 / 6.0 - x4 / 24.0                     # E[x], r = 0
        var = v2 - mu * mu
        if var > 0.0:
            out['rn_skew'] = (w3 - 3.0 * mu * v2 + 2.0 * mu ** 3) / var ** 1.5
            out['rn_kurt'] = (x4 - 4.0 * mu * w3 + 6.0 * mu * mu * v2 - 3.0 * mu ** 4) / var ** 2
        out['rn_p_dn10'] = _tail_prob_below(smile, math.log(1.0 - RN_TAIL_MOVE), k_min, k_max, atm, t)
        below_up = _tail_prob_below(smile, math.log(1.0 + RN_TAIL_MOVE), k_min, k_max, atm, t)
        out['rn_p_up10'] = None if below_up is None else float(1.0 - below_up)
        for key, val in list(out.items()):
            if val is not None and not math.isfinite(val):
                out[key] = None
        return out
    except Exception:  # noqa: BLE001 — a degenerate smile must never break the row
        return dict(_STRIP_NONE)


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
    strip = strip_features(smile, float(k[0]), float(k[-1]), atm, t)
    return SmileFit(dte=int(dte), t=t, atm_iv=atm, iv_25d_put=ivp, iv_25d_call=ivc,
                    n_strikes=int(len(K)), k_min=float(k[0]), k_max=float(k[-1]), **strip)


ATM_BAND_MIN_ROWS = 1


def atm_band_fit(exp_rows: pd.DataFrame, dte: int) -> SmileFit | None:
    """Fallback per-expiry point when no smile can be fitted (amendment 2026-09-06 §H):
    the |Δ| .40–.60 band mean IV — v1's `iv_front` definition — carrying only
    `atm_iv`; every smile-only field is None and `source == 'atm_band'`.
    Never a fabricated 0: no band row ⇒ None."""
    if 'delta' not in exp_rows.columns:
        return None
    iv = pd.to_numeric(exp_rows['implied_volatility'], errors='coerce')
    d = pd.to_numeric(exp_rows['delta'], errors='coerce').abs()
    band = iv[(d >= ATM_DELTA[0]) & (d <= ATM_DELTA[1]) & (iv > IV_MIN)]
    if len(band) < ATM_BAND_MIN_ROWS:
        return None
    atm = float(band.mean())
    if not (atm > 0):
        return None
    return SmileFit(dte=int(dte), t=dte / 365.0, atm_iv=atm, iv_25d_put=None, iv_25d_call=None,
                    n_strikes=int(len(band)), k_min=0.0, k_max=0.0, source='atm_band')


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
                'n_expiries_fit': 0, 'n_strikes_30d': 0, 'n_expiries_atm': 0,
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
    n_smile = n_atm = 0
    for dte, exp_rows in ch[(ch['dte'] >= FIT_DTE[0]) & (ch['dte'] <= FIT_DTE[1])].groupby('dte'):
        side = _otm_side(exp_rows, spot_f)
        fit = fit_smile(side['strike'].to_numpy(), side['iv'].to_numpy(), spot_f, int(dte))
        if fit is not None:
            n_smile += 1
        else:
            fit = atm_band_fit(exp_rows, int(dte))       # amendment §H: smile first, band fills the gap
            if fit is not None:
                n_atm += 1
        if fit is not None:
            fits[int(dte)] = fit
    row['n_expiries_fit'] = n_smile
    row['n_expiries_atm'] = n_atm
    if not fits:
        return row
    # Band points may lift `iv30`/`iv90` (§H.1), but every smile-only key is
    # taken against the smile-only fits — a difference like `p30 − iv30` must
    # compare the SAME maturity mix, or a mixed ticker-day fabricates a skew
    # that is really the band-vs-smile term spread (final review C1).
    smile_fits = {d: f for d, f in fits.items() if f.source == 'smile'}
    iv30 = constant_maturity(fits, 30, 'atm_iv')
    iv90 = constant_maturity(fits, 90, 'atm_iv')
    iv30_s = constant_maturity(smile_fits, 30, 'atm_iv')
    p30 = constant_maturity(smile_fits, 30, 'iv_25d_put')
    c30 = constant_maturity(smile_fits, 30, 'iv_25d_call')
    near30 = min(fits, key=lambda d: abs(d - 30))            # the point iv30 leans on
    near30_s = min(smile_fits, key=lambda d: abs(d - 30)) if smile_fits else None
    row.update({
        'iv30': iv30, 'iv90': iv90, 'near_iv': iv30, 'far_iv': iv90,
        'ts_ratio': (iv30 / iv90) if (iv30 is not None and iv90 is not None and iv90 > 0) else None,
        'term_slope': (iv90 - iv30) if (iv30 is not None and iv90 is not None) else None,
        'iv_25d_put_30d': p30, 'iv_25d_call_30d': c30,
        'skew_25d_30d': (p30 - iv30_s) if (p30 is not None and iv30_s is not None) else None,
        'rr_25d_30d': (p30 - c30) if (p30 is not None and c30 is not None) else None,
        'n_strikes_30d': fits[near30].n_strikes,
    })
    # Honest label: 'smile' only when the smile-only 30 d point IS the served
    # one (same inputs through the same function ⇒ exact equality is legitimate);
    # any band participation in the interpolation reads 'atm_band'.
    row['iv30_source'] = (None if iv30 is None else
                          ('smile' if (iv30_s is not None and iv30 == iv30_s) else 'atm_band'))
    row['skew_20d'] = row['skew_25d_30d']
    # v3 (spec 2026-09-06 A.3): MFIV interpolates in total variance like the ATM
    # points; RN moments/tails come from the smile expiry nearest 30 DTE (G4).
    mf30 = constant_maturity(fits, 30, 'mfiv')      # band fits carry mfiv=None ⇒ already smile-only
    mf90 = constant_maturity(fits, 90, 'mfiv')
    row.update({'mfiv_30d': mf30, 'mfiv_90d': mf90,
                'mf_tail_premium_30d': (mf30 - iv30_s) if (mf30 is not None and iv30_s is not None) else None})
    if near30_s is not None and abs(near30_s - 30) <= RN_MOMENT_DTE_TOL:
        f30 = smile_fits[near30_s]
        row.update({'rn_skew_30d': f30.rn_skew, 'rn_kurt_30d': f30.rn_kurt,
                    'rn_p_dn10_30d': f30.rn_p_dn10, 'rn_p_up10_30d': f30.rn_p_up10})
    return row


# --- append to src/strategies/options_surface.py ---
HIST_LEN = 20
IV_RANK_WINDOW = 252
IV_RANK_MIN_OBS = 20
ZSCORE_WINDOW = 60
ZSCORE_MIN_OBS = 10
RV_WINDOW = 20
# series_features maps rv_20 AS-OF, not by exact date: under the production
# intraday overlay the chain rows are dated today while prices.parquet still
# ends at T−1, so an exact-date map returned NaN for today's row and silently
# dropped rv_20/vrp/vrp_zscore from the live v2 dict. Seven calendar days
# covers a long holiday weekend without ever reaching back a stale fortnight.
RV_ASOF_TOLERANCE = pd.Timedelta(days=7)
SERIES_KEYS = ['rv_20', 'vrp', 'iv_rank', 'vrp_zscore', 'iv_rank_history', 'vrp_history',
               'hv20_history', 'pc_ratio_history']


def rv_series_from_closes(closes: pd.Series) -> pd.Series:
    """20-session std of log returns × √252, indexed like `closes` (spec A.5)."""
    c = pd.to_numeric(closes, errors='coerce').astype(float)
    lr = np.log(c / c.shift(1))
    return lr.rolling(RV_WINDOW).std() * math.sqrt(252)


def _pct_rank(s: pd.Series) -> pd.Series:
    return s.rolling(IV_RANK_WINDOW, min_periods=IV_RANK_MIN_OBS).rank(pct=True) * 100.0


def _zscore(s: pd.Series) -> pd.Series:
    m = s.rolling(ZSCORE_WINDOW, min_periods=ZSCORE_MIN_OBS).mean()
    sd = s.rolling(ZSCORE_WINDOW, min_periods=ZSCORE_MIN_OBS).std()
    return (s - m) / sd.replace(0, np.nan)


def _history(s: pd.Series, window: int = HIST_LEN, min_len: int = 5) -> pd.Series:
    """Trailing-`window` list of the non-null values at each position, oldest
    first, or None below `min_len`.

    Sliding deque over a vectorised null mask — identical output to the
    O(n·window) `pd.isna`-per-element loop it replaces (pinned by
    test_history_matches_reference_loop), without the per-element scalar
    pandas calls that dominated its cost."""
    vals = s.to_numpy()
    isna = pd.isna(vals)
    out: list = []
    dq: deque = deque()
    for i in range(len(vals)):
        if not isna[i]:
            dq.append((i, float(vals[i])))
        while dq and dq[0][0] <= i - window:
            dq.popleft()
        out.append([v for _, v in dq] if len(dq) >= min_len else None)
    return pd.Series(out, index=s.index, dtype=object)


def _none_if_nan(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def series_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker time-series features over an ascending frame with columns
    date, iv30, pc_ratio, rv_20 (spec A.5). The single implementation behind
    both the enriched backtest panel and the live per-ticker call."""
    out = df.sort_values('date').reset_index(drop=True).copy()
    for c in ('iv30', 'pc_ratio', 'rv_20'):
        out[c] = pd.to_numeric(out.get(c), errors='coerce')
    out['vrp'] = out['iv30'] - out['rv_20']
    out['iv_rank'] = _pct_rank(out['iv30'])
    out['vrp_zscore'] = _zscore(out['vrp'])
    out['iv_rank_history'] = _history(out['iv_rank'])
    out['vrp_history'] = _history(out['vrp'])
    out['hv20_history'] = _history(out['rv_20'])
    out['pc_ratio_history'] = _history(out['pc_ratio'])
    for c in ('vrp', 'iv_rank', 'vrp_zscore'):
        out[c] = out[c].astype(object).where(out[c].notna(), None)
    return out


def series_features(today: dict, history: pd.DataFrame, rv: pd.Series) -> dict:
    """Series features for ONE day given the ticker's prior surface rows and its
    realized-vol series. Equivalent to the last row of series_frame over
    history + today — the parity contract with the enriched panel."""
    cols = ['date', 'iv30', 'pc_ratio']
    h = history[cols].copy() if history is not None and len(history) else pd.DataFrame(columns=cols)
    t = pd.DataFrame([{c: today.get(c) for c in cols}])
    frame = pd.concat([h, t], ignore_index=True)
    frame['date'] = pd.to_datetime(frame['date']).dt.normalize()
    frame = frame[frame['date'] <= frame['date'].iloc[-1]].drop_duplicates('date', keep='last')
    rv_s = pd.to_numeric(rv, errors='coerce') if rv is not None else pd.Series(dtype=float)
    if len(rv_s):
        # Normalised, unique, sorted — reindex(method='ffill') requires all three.
        rv_s = pd.Series(rv_s.to_numpy(dtype=float), index=pd.to_datetime(rv_s.index).normalize())
        rv_s = rv_s[~rv_s.index.duplicated(keep='last')].sort_index()
        # .to_numpy(): reindex returns a Series keyed by DATE, while `frame`
        # carries a RangeIndex — assigning the Series directly would align on
        # index and yield all-NaN.
        frame['rv_20'] = rv_s.reindex(frame['date'], method='ffill',
                                      tolerance=RV_ASOF_TOLERANCE).to_numpy(dtype=float)
    else:
        frame['rv_20'] = float('nan')
    last = series_frame(frame).iloc[-1]
    return {k: (_none_if_nan(last[k]) if not isinstance(last[k], list) else list(last[k])) for k in SERIES_KEYS}
