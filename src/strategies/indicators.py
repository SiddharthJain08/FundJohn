"""Shared indicator library for strategy authors (exposed as ``BaseStrategy.ind``).

Every function takes a ``pd.Series`` (``atr(method='true_range')`` takes an OHLC
DataFrame), returns a ``pd.Series`` on the same index, and never raises on empty
or short input: it returns all-NaN instead.  Epsilon handling comes from
``safe_ops`` (one policy, ``EPS = 1e-9``).
"""
import math

import numpy as np
import pandas as pd

try:  # tolerant: base.py may be loaded as a top-level module (bare-script strategies)
    from strategies.safe_ops import EPS, safe_div
except ModuleNotFoundError as _e:  # pragma: no cover - top-level `base` import path
    if _e.name not in ('strategies', 'strategies.safe_ops'):
        raise
    from safe_ops import EPS, safe_div

_ATR_METHODS = ('true_range', 'close_diff')


def atr(bars_or_close, n: int = 14, *, method: str = 'true_range', fallback_pct=0.02) -> pd.Series:
    """Average true range.

    method='true_range': DataFrame with high/low/close, TR = max(h-l, |h-pc|, |l-pc|).
    method='close_diff': Series (or frame's close), |close.diff()| — the
    BaseStrategy stop-distance definition.
    Rolling mean over ``n``; where the result is NaN or <= 0 and ``fallback_pct``
    is not None it becomes ``close * fallback_pct``.
    """
    if method not in _ATR_METHODS:
        raise ValueError(f'atr: method={method!r} — expected one of {_ATR_METHODS}')
    if method == 'true_range':
        if not isinstance(bars_or_close, pd.DataFrame):
            raise ValueError("atr(method='true_range') needs a DataFrame with high/low/close")
        h, l, c = bars_or_close['high'], bars_or_close['low'], bars_or_close['close']
        pc = c.shift(1)
        raw = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    else:
        c = bars_or_close['close'] if isinstance(bars_or_close, pd.DataFrame) else bars_or_close
        raw = c.diff().abs()
    out = raw.rolling(n).mean()
    if fallback_pct is not None:
        bad = out.isna() | (out <= 0)
        out = out.where(~bad, c * fallback_pct)
    return out


def realized_vol(close: pd.Series, n: int = 20, *, log: bool = True, annualize=252) -> pd.Series:
    """Rolling std of returns (log or simple), scaled by sqrt(annualize) if truthy."""
    if log:
        with np.errstate(all='ignore'):
            rets = np.log(close).diff()
    else:
        rets = close / close.shift(1) - 1
    out = rets.rolling(n).std()
    if annualize:
        out = out * math.sqrt(annualize)
    return out


def zscore(s: pd.Series, n: int, *, min_periods=None) -> pd.Series:
    """(s - rolling mean) / rolling std (ddof=1); zero/NaN std -> NaN."""
    mp = n if min_periods is None else min_periods
    r = s.rolling(n, min_periods=mp)
    return safe_div(s - r.mean(), r.std(), fill=np.nan)


def mavg(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def csum(s: pd.Series) -> pd.Series:
    return s.cumsum()


def diff(s: pd.Series, *, ignore_negative: bool = False) -> pd.Series:
    d = s.diff()
    return d.where(~(d < 0)) if ignore_negative else d


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI; 100 where average loss <= EPS."""
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = safe_div(gain, loss, fill=np.inf)
    out = 100 - 100 / (1 + rs)
    out = out.where(~(loss <= EPS), 100.0)
    out.iloc[:n] = np.nan   # warm-up: need n+1 points (matches oxford rsi_wilder)
    return out
