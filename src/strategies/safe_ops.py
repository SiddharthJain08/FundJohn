"""Single epsilon policy for strategy code.

One guard constant, ``EPS = 1e-9``, used everywhere a denominator, log argument
or positive floor is needed.  Authors should call these helpers instead of
sprinkling raw literals such as ``+ 1e-9`` / ``1e-12`` / ``1e-6`` (the
``raw_epsilon_literal`` rule in ``strategy_lint`` points here).

Policy:
  * ``safe_div(a, b, fill=0.0)``  -> a / b where |b| >= EPS, ``fill`` where
    |b| < EPS; a NaN denominator stays NaN (NaN is never filled).
  * ``safe_log(x)``               -> log(x) where x > EPS, else NaN.
  * ``clip_pos(x)``               -> max(x, EPS), NaN preserved.

All accept a scalar, ndarray, Series or DataFrame and return the same kind
(index/columns kept).  None emits a numpy RuntimeWarning.
"""
import numpy as np
import pandas as pd

EPS: float = 1e-9


def _wrap(like, values):
    if isinstance(like, (pd.Series, pd.DataFrame)):
        return type(like)(values, index=like.index, **(
            {'columns': like.columns} if isinstance(like, pd.DataFrame) else {'name': like.name}))
    return values


def safe_div(a, b, fill=0.0):
    pa, pb = isinstance(a, (pd.Series, pd.DataFrame)), isinstance(b, (pd.Series, pd.DataFrame))
    if pa and pb:
        # Align on labels like pandas does (never divide by position).
        if isinstance(a, pd.DataFrame) and isinstance(b, pd.Series):
            a, b = a.align(b, axis=0)
            b = pd.DataFrame({c: b for c in a.columns}, index=a.index)
        elif isinstance(a, pd.Series) and isinstance(b, pd.DataFrame):
            a, b = a.align(b, axis=0)
            a = pd.DataFrame({c: a for c in b.columns}, index=b.index)
        else:
            a, b = a.align(b)
    ref = b if isinstance(b, (pd.Series, pd.DataFrame)) else a
    scalar = np.ndim(a) == 0 and np.ndim(b) == 0
    av = np.asarray(a, dtype=float)
    bv = np.asarray(b, dtype=float)
    with np.errstate(all='ignore'):
        small = np.abs(bv) < EPS          # False for NaN
        out = np.where(small, fill, av / np.where(small, 1.0, bv))
        out = np.where(np.isnan(bv), np.nan, out)
    if scalar:
        return float(out)
    return _wrap(ref, out)


def safe_log(x):
    xv = np.asarray(x, dtype=float)
    with np.errstate(all='ignore'):
        ok = xv > EPS                      # False for NaN
        out = np.where(ok, np.log(np.where(ok, xv, 1.0)), np.nan)
    if np.ndim(x) == 0:
        return float(out)
    return _wrap(x, out)


def clip_pos(x):
    xv = np.asarray(x, dtype=float)
    with np.errstate(all='ignore'):
        out = np.where(np.isnan(xv), np.nan, np.maximum(xv, EPS))
    if np.ndim(x) == 0:
        return float(out)
    return _wrap(x, out)
