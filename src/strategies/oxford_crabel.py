"""Shared Crabel/Oxford daily-bar helpers + OHLC-aware base class.

oxfordstrat.com strategies are single-instrument daily-OHLC rules. generate_signals
receives a CLOSE-ONLY wide panel, so OxfordBaseStrategy self-loads basket OHLC from
the master parquet (filtered to the basket) and caches it on the instance (the
backtest builds one instance per run, then loops bars). All indicator helpers are
pure functions over an OHLC DataFrame indexed by date with open/high/low/close.
"""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal

# Liquid ETF basket proxying Oxford's 4 futures sectors. Verified present in
# data/master/prices.parquet 2026-06-15. CORE = full ~10y history; EXT = ~5y.
OXFORD_ETF_BASKET_CORE = [
    'SPY','QQQ','IWM','DIA','EFA','EEM','VTI','TLT','IEF','SHY','LQD','HYG','AGG',
    'GLD','SLV','USO','UNG','XLE','XLF','XLK','XLV','XLI','XLP','XLU','XLY','XLB','GDX']
OXFORD_ETF_BASKET_EXT = [
    'DBC','DBA','DBB','CPER','PALL','PPLT','CORN','WEAT','SOYB','MDY','UUP','UDN','FXF']
OXFORD_ETF_BASKET = OXFORD_ETF_BASKET_CORE + OXFORD_ETF_BASKET_EXT


def _prices_parquet_path() -> Path:
    return Path(__file__).resolve().parents[2] / 'data' / 'master' / 'prices.parquet'


def true_range_series(bars: pd.DataFrame) -> pd.Series:
    h, l, c = bars['high'], bars['low'], bars['close']
    pc = c.shift(1)
    return pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(bars: pd.DataFrame, n: int = 20) -> float:
    if len(bars) < n + 1:
        return float('nan')
    return float(true_range_series(bars).rolling(n).mean().iloc[-1])


def donchian_prev(bars: pd.DataFrame, n: int) -> tuple[float, float]:
    """Upper/lower Donchian over the n bars BEFORE the current bar (no same-bar leak)."""
    if len(bars) < n + 1:
        return float('nan'), float('nan')
    up = float(bars['high'].iloc[-(n+1):-1].max())
    lo = float(bars['low'].iloc[-(n+1):-1].min())
    return up, lo


def sma(s: pd.Series, n: int) -> float:
    if len(s) < n:
        return float('nan')
    return float(s.rolling(n).mean().iloc[-1])


def rsi_wilder(s: pd.Series, n: int) -> float:
    if len(s) < n + 1:
        return float('nan')
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] > 1e-12 else float('inf')
    return float(100 - 100/(1+rs))


def avg_noise(bars: pd.DataFrame, n: int = 10) -> float:
    """Crabel stretch base: mean over n bars of min(open-low, high-open)."""
    if len(bars) < n:
        return float('nan')
    sub = bars.iloc[-n:]
    noise = np.minimum((sub['open']-sub['low']).abs(), (sub['high']-sub['open']).abs())
    return float(noise.mean())


def is_nrn(bars: pd.DataFrame, n: int) -> bool:
    """True if the current bar's range is the narrowest of the last n bars (NR-n / NR7)."""
    if len(bars) < n:
        return False
    rng = (bars['high'] - bars['low']).iloc[-n:]
    return bool(rng.iloc[-1] == rng.min() and rng.iloc[-1] < rng.iloc[:-1].min())


def gap_dir(bars: pd.DataFrame) -> int:
    """+1 if today gapped fully up (low > prior high), -1 if down (high < prior low), else 0."""
    if len(bars) < 2:
        return 0
    today, prev = bars.iloc[-1], bars.iloc[-2]
    if today['low'] > prev['high']:
        return 1
    if today['high'] < prev['low']:
        return -1
    return 0


# --------------------------------------------------------------------------
# Batch-1 indicators (MA / oscillator / momentum). Each cites its oxfordstrat
# slug; the form below is the exact Oxford definition (or, where Oxford only
# references a source, the standard textbook form noted in the docstring).
# All return a scalar (the latest value) or a small tuple, NaN if insufficient
# data — pure, deterministic, never raising.
# --------------------------------------------------------------------------

def _ema_series(s: pd.Series, n: int) -> pd.Series:
    """EMA series, alpha=2/(n+1), seeded on the first observation (adjust=False)."""
    return s.ewm(span=n, adjust=False).mean()


def ema(s: pd.Series, n: int) -> float:
    """Exponential MA latest value. alpha=2/(n+1) (oxfordstrat zero-lag-moving-average)."""
    if len(s) < 1:
        return float('nan')
    return float(_ema_series(s, n).iloc[-1])


def macd(s: pd.Series, fast: int = 12, slow: int = 26) -> float:
    """MACD line = EMA(fast) - EMA(slow), latest value (oxfordstrat macd-part-1)."""
    if len(s) < slow:
        return float('nan')
    return float(_ema_series(s, fast).iloc[-1] - _ema_series(s, slow).iloc[-1])


def roc(s: pd.Series, n: int) -> float:
    """Rate of change = 100*(close - close[-n-1])/close[-n-1]
    (oxfordstrat dual-momentum-rate-of-change)."""
    if len(s) < n + 1:
        return float('nan')
    prev = float(s.iloc[-n - 1])
    if prev == 0 or prev != prev:
        return float('nan')
    return float((float(s.iloc[-1]) - prev) / prev * 100.0)


def linreg_slope(s: pd.Series, n: int) -> float:
    """Least-squares slope of the last n closes vs. time index 0..n-1
    (oxfordstrat linear-regression). LRS = Σ(x-x̄)(y-ȳ) / Σ(x-x̄)². Raw (un-normalized)."""
    if len(s) < n or n < 2:
        return float('nan')
    y = s.iloc[-n:].to_numpy(dtype=float)
    if not np.all(np.isfinite(y)):
        return float('nan')
    x = np.arange(n, dtype=float)
    xm = x.mean()
    denom = float(((x - xm) ** 2).sum())
    if denom == 0:
        return float('nan')
    return float(((x - xm) * (y - y.mean())).sum() / denom)


def _wma_series(s: pd.Series, n: int) -> pd.Series:
    """Weighted MA, weights 1..n (most recent = n). WMA = Σ(w_i·x_i) / Σw_i."""
    w = np.arange(1, n + 1, dtype=float)
    denom = w.sum()
    return s.rolling(n).apply(lambda x: float(np.dot(x, w) / denom), raw=True)


def hma(s: pd.Series, n: int) -> float:
    """Hull Moving Average latest value (oxfordstrat hull-moving-average).
    HMA = WMA( 2·WMA(close, round(n/2)) − WMA(close, n), round(√n) ).
    Faithful nested-WMA Oxford form (M=round(n/2), K=round(√n))."""
    n = int(n)
    if n < 2:
        return float('nan')
    m = int(round(n / 2))
    k = int(round(np.sqrt(n)))
    if m < 1 or k < 1 or len(s) < n + k:
        return float('nan')
    wma_half = _wma_series(s, m)
    wma_full = _wma_series(s, n)
    delta = 2.0 * wma_half - wma_full
    hull = _wma_series(delta.dropna(), k)
    if len(hull) == 0:
        return float('nan')
    return float(hull.iloc[-1])


def zlma(s: pd.Series, n: int, gain: float = 5.0) -> tuple[float, float]:
    """Zero-Lag Moving Average (oxfordstrat zero-lag-moving-average), FIXED gain.

    alpha = 2/(n+1)
    ZLMA[i] = alpha·(EMA[i] + gain·(Close[i] − ZLMA[i-1])) + (1−alpha)·ZLMA[i-1]
    err = Close[i] − ZLMA[i]   (Oxford's Least_Error, here at the fixed gain)

    DEVIATION: Oxford optimizes `gain` per bar via an error-minimizing loop
    (non-causal, path-dependent). We use a fixed gain (default 5 = Oxford's
    Gain_Limit) — the recursion is the faithful Oxford form; the gain loop is a
    meta-optimization we deliberately omit. Returns (zlma_latest, err_latest)."""
    if len(s) < n + 1:
        return float('nan'), float('nan')
    alpha = 2.0 / (n + 1)
    e = _ema_series(s, n).to_numpy(dtype=float)
    c = s.to_numpy(dtype=float)
    z = c[0]
    for i in range(1, len(c)):
        z = alpha * (e[i] + gain * (c[i] - z)) + (1 - alpha) * z
    err = float(c[-1] - z)
    return float(z), err


def kaufman_ama(s: pd.Series, er_len: int = 20, fast: int = 2, slow: int = 30) -> float:
    """Kaufman Adaptive Moving Average latest value (oxfordstrat adaptive-moving-average-1).

    ER = |Close[i] − Close[i-er_len]| / Σ|ΔClose| over er_len
    SC = (ER·(2/(fast+1) − 2/(slow+1)) + 2/(slow+1))²
    AMA[i] = AMA[i-1] + SC·(Close[i] − AMA[i-1]);  seeded AMA = Close[er_len]."""
    if len(s) < er_len + 1:
        return float('nan')
    c = s.to_numpy(dtype=float)
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    a = c[er_len]  # seed AMA at the first bar where ER is computable
    for i in range(er_len + 1, len(c)):
        direction = abs(c[i] - c[i - er_len])
        volatility = float(np.abs(np.diff(c[i - er_len:i + 1])).sum())
        er = direction / volatility if volatility > 1e-12 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        a = a + sc * (c[i] - a)
    return float(a)


def kaufman_ama_series(s: pd.Series, er_len: int = 20, fast: int = 2, slow: int = 30) -> pd.Series:
    """Full KAMA series (NaN before er_len). Used to test 'rising' + MinAMA filter."""
    c = s.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) < er_len + 1:
        return pd.Series(out, index=s.index)
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    a = c[er_len]
    out[er_len] = a
    for i in range(er_len + 1, len(c)):
        direction = abs(c[i] - c[i - er_len])
        volatility = float(np.abs(np.diff(c[i - er_len:i + 1])).sum())
        er = direction / volatility if volatility > 1e-12 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        a = a + sc * (c[i] - a)
        out[i] = a
    return pd.Series(out, index=s.index)


def frama(bars: pd.DataFrame, n: int) -> float:
    """Fractal Adaptive Moving Average latest value (oxfordstrat fractal-adaptive-moving-average).

    Oxford only cites Ehlers; standard Ehlers FRAMA used (Price = (High+Low)/2):
      split the last n bars into two halves of n/2;
      N1 = (max(H,half1)-min(L,half1))/(n/2);  N2 = same over half2;
      N3 = (max(H,n)-min(L,n))/n;
      D  = (ln(N1+N2) − ln(N3)) / ln(2);
      alpha = exp(−4.6·(D−1)), clamped to [0.01, 1];
      FRAMA[i] = alpha·Price[i] + (1−alpha)·FRAMA[i-1].
    Requires even n (rounded up to even)."""
    n = int(n)
    if n % 2 != 0:
        n += 1
    half = n // 2
    if len(bars) < n + 1:
        return float('nan')
    price = ((bars['high'] + bars['low']) / 2.0).to_numpy(dtype=float)
    hi = bars['high'].to_numpy(dtype=float)
    lo = bars['low'].to_numpy(dtype=float)
    ln2 = np.log(2.0)
    # Seed FRAMA at the first bar with a full window, then recurse forward.
    f = price[n - 1]
    for i in range(n - 1, len(price)):
        w_hi = hi[i - n + 1:i + 1]
        w_lo = lo[i - n + 1:i + 1]
        h1_hi, h1_lo = w_hi[:half], w_lo[:half]
        h2_hi, h2_lo = w_hi[half:], w_lo[half:]
        n1 = (h1_hi.max() - h1_lo.min()) / half
        n2 = (h2_hi.max() - h2_lo.min()) / half
        n3 = (w_hi.max() - w_lo.min()) / n
        if n1 > 0 and n2 > 0 and n3 > 0:
            d = (np.log(n1 + n2) - np.log(n3)) / ln2
        else:
            d = 1.0  # flat window → dimension 1 (alpha=1, follow price)
        alpha = float(np.exp(-4.6 * (d - 1.0)))
        alpha = min(max(alpha, 0.01), 1.0)
        if i == n - 1:
            f = price[i]
        else:
            f = alpha * price[i] + (1.0 - alpha) * f
    return float(f)


def vortex(bars: pd.DataFrame, n: int) -> tuple[float, float]:
    """Vortex Indicator latest (+VI, -VI) (oxfordstrat vortex-indicator-1).
      +VM = |High[i] − Low[i-1]|;  -VM = |Low[i] − High[i-1]|;
      TR  = max(High,Close[i-1]) − min(Low,Close[i-1]);
      +VI = Σ(+VM, n) / Σ(TR, n);  -VI = Σ(-VM, n) / Σ(TR, n)."""
    if len(bars) < n + 1:
        return float('nan'), float('nan')
    h = bars['high'].to_numpy(dtype=float)
    l = bars['low'].to_numpy(dtype=float)
    c = bars['close'].to_numpy(dtype=float)
    pvm = np.abs(h[1:] - l[:-1])
    nvm = np.abs(l[1:] - h[:-1])
    tr = np.maximum(h[1:], c[:-1]) - np.minimum(l[1:], c[:-1])
    pvm_s = pvm[-n:].sum()
    nvm_s = nvm[-n:].sum()
    tr_s = tr[-n:].sum()
    if tr_s <= 1e-12:
        return float('nan'), float('nan')
    return float(pvm_s / tr_s), float(nvm_s / tr_s)


# --------------------------------------------------------------------------
# Batch-2 indicators (pattern / structure / bands). Each cites its oxfordstrat
# slug; formulas fetched 2026-06-15 from the per-strategy pages. All return a
# scalar / small tuple (latest value), NaN/empty on insufficient data — pure,
# deterministic, never raising. Each is single-pass over the relevant tail (no
# O(bars^2) rolling().apply()).
# --------------------------------------------------------------------------

def aroon(bars: pd.DataFrame, n: int) -> tuple[float, float]:
    """Aroon Up/Down latest values (oxfordstrat aroon-indicator-breakout-1).

    AroonUp   = 100 * (n - bars_since_highest_high_in_last_(n+1)) / n
    AroonDown = 100 * (n - bars_since_lowest_low_in_last_(n+1))  / n
    where 'bars since' counts back from the current bar (0 = current bar is the
    extreme). Window = the last n+1 bars (Aroon_Index + 1, Oxford). Returns
    (AroonUp, AroonDown), each in [0, 100]; NaN if fewer than n+1 bars."""
    n = int(n)
    if n < 1 or len(bars) < n + 1:
        return float('nan'), float('nan')
    win_h = bars['high'].iloc[-(n + 1):].to_numpy(dtype=float)
    win_l = bars['low'].iloc[-(n + 1):].to_numpy(dtype=float)
    if not (np.all(np.isfinite(win_h)) and np.all(np.isfinite(win_l))):
        return float('nan'), float('nan')
    last = n  # index of the current bar within the (n+1)-length window
    # argmax/argmin return the FIRST occurrence; Oxford counts the most recent
    # extreme, so scan from the right for ties (favours the latest bar).
    hh_pos = last - int(np.argmax(win_h[::-1]))   # most-recent highest high
    ll_pos = last - int(np.argmin(win_l[::-1]))   # most-recent lowest low
    bars_since_hh = last - hh_pos
    bars_since_ll = last - ll_pos
    up = 100.0 * (n - bars_since_hh) / n
    dn = 100.0 * (n - bars_since_ll) / n
    return float(up), float(dn)


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> tuple[float, float, float]:
    """Bollinger Bands latest (mid, upper, lower) (oxfordstrat bollinger-bands).

    mid = SMA(close, n); upper = mid + k*sigma; lower = mid - k*sigma, where
    sigma is the POPULATION std (ddof=0) over the same n closes (standard
    Bollinger). NaN tuple if fewer than n closes."""
    n = int(n)
    if n < 1 or len(s) < n:
        return float('nan'), float('nan'), float('nan')
    win = s.iloc[-n:].to_numpy(dtype=float)
    if not np.all(np.isfinite(win)):
        return float('nan'), float('nan'), float('nan')
    mid = float(win.mean())
    sigma = float(win.std(ddof=0))
    return mid, mid + k * sigma, mid - k * sigma


def keltner(bars: pd.DataFrame, lb: int = 10, mult: float = 1.0) -> tuple[float, float, float]:
    """Keltner Channels latest (mid, buy_line, sell_line) (oxfordstrat keltner-channels-1).

    DIVERGENCE FROM TEXTBOOK: Oxford's keltner-channels-1 uses AVERAGE RANGE, NOT
    ATR. Per the page:
      Typical_Price[i] = (High[i] + Low[i] + Close[i]) / 3
      MA_TP   = SMA(Typical_Price, Look_Back)
      Range[i]= High[i] - Low[i]
      MA_Range= SMA(Range, Look_Back)
      Buy_Line  = MA_TP + MA_Range * Range_Multiple
      Sell_Line = MA_TP - MA_Range * Range_Multiple
    Defaults Look_Back=10, Range_Multiple=1. NaN tuple if fewer than lb bars."""
    lb = int(lb)
    if lb < 1 or len(bars) < lb:
        return float('nan'), float('nan'), float('nan')
    sub = bars.iloc[-lb:]
    tp = (sub['high'] + sub['low'] + sub['close']) / 3.0
    rng = sub['high'] - sub['low']
    ma_tp = float(tp.mean())
    ma_rng = float(rng.mean())
    if not (np.isfinite(ma_tp) and np.isfinite(ma_rng)):
        return float('nan'), float('nan'), float('nan')
    return ma_tp, ma_tp + ma_rng * mult, ma_tp - ma_rng * mult


def heikin_ashi_series(bars: pd.DataFrame) -> pd.DataFrame:
    """Full Heikin-Ashi candle series (oxfordstrat heikin-ashi-1). Single forward pass.

    HaClose[i] = (Open[i] + High[i] + Low[i] + Close[i]) / 4
    HaOpen[0]  = (Open[0] + Close[0]) / 2
    HaOpen[i]  = (HaOpen[i-1] + HaClose[i-1]) / 2          (i > 0)
    HaHigh[i]  = max(High[i], HaOpen[i], HaClose[i])
    HaLow[i]   = min(Low[i],  HaOpen[i], HaClose[i])
    Returns a DataFrame (same index) with ha_open/ha_close/ha_high/ha_low."""
    o = bars['open'].to_numpy(dtype=float)
    h = bars['high'].to_numpy(dtype=float)
    l = bars['low'].to_numpy(dtype=float)
    c = bars['close'].to_numpy(dtype=float)
    m = len(c)
    ha_close = (o + h + l + c) / 4.0
    ha_open = np.empty(m)
    ha_high = np.empty(m)
    ha_low = np.empty(m)
    if m == 0:
        return pd.DataFrame({'ha_open': ha_open, 'ha_close': ha_close,
                             'ha_high': ha_high, 'ha_low': ha_low}, index=bars.index)
    ha_open[0] = (o[0] + c[0]) / 2.0
    ha_high[0] = max(h[0], ha_open[0], ha_close[0])
    ha_low[0] = min(l[0], ha_open[0], ha_close[0])
    for i in range(1, m):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
        ha_high[i] = max(h[i], ha_open[i], ha_close[i])
        ha_low[i] = min(l[i], ha_open[i], ha_close[i])
    return pd.DataFrame({'ha_open': ha_open, 'ha_close': ha_close,
                         'ha_high': ha_high, 'ha_low': ha_low}, index=bars.index)


def heikin_ashi(bars: pd.DataFrame) -> tuple[float, float, float, float]:
    """Latest Heikin-Ashi candle (ha_open, ha_close, ha_high, ha_low).
    NaN tuple if no bars. See heikin_ashi_series for the recursion."""
    if len(bars) == 0:
        return (float('nan'),) * 4
    ser = heikin_ashi_series(bars)
    return (float(ser['ha_open'].iloc[-1]), float(ser['ha_close'].iloc[-1]),
            float(ser['ha_high'].iloc[-1]), float(ser['ha_low'].iloc[-1]))


def swing_pivots(bars: pd.DataFrame, k: int = 2) -> tuple[list, list]:
    """Fractal swing-pivot detector — CONFIRMED-only, no look-ahead (general primitive).

    A bar j is a SWING HIGH if its high is strictly greater than the highs of the
    k bars on EACH side (j-k..j-1 and j+1..j+k); a SWING LOW symmetrically on lows.
    This is Oxford's DeMark 'Pivot_Size = k' definition (dow-theory-trend: "a low
    that has two higher lows on either side has Pivot_Size = 2") and the general
    detector reused by livermore / wyckoff / ross_hook.

    CONFIRMATION (no look-ahead): a pivot at bar j is only emitted once its k
    right-side bars exist, so the most-recent reported pivot is at least k bars
    back from the current bar — the current bar is NEVER its own pivot (mirrors
    donchian_prev's same-bar-leak care). Single pass, O(bars).

    Returns (highs, lows) where each is a list of (positional_index, price) in
    chronological order (oldest first); positional index is 0-based into `bars`."""
    k = int(k)
    h = bars['high'].to_numpy(dtype=float)
    l = bars['low'].to_numpy(dtype=float)
    m = len(h)
    highs: list = []
    lows: list = []
    if k < 1 or m < 2 * k + 1:
        return highs, lows
    # Only bars k..m-1-k can be confirmed pivots (need k bars on the right).
    for j in range(k, m - k):
        hj = h[j]
        lj = l[j]
        if not (np.isfinite(hj) and np.isfinite(lj)):
            continue
        left_h = h[j - k:j]
        right_h = h[j + 1:j + 1 + k]
        if np.all(hj > left_h) and np.all(hj > right_h):
            highs.append((j, float(hj)))
        left_l = l[j - k:j]
        right_l = l[j + 1:j + 1 + k]
        if np.all(lj < left_l) and np.all(lj < right_l):
            lows.append((j, float(lj)))
    return highs, lows


def td_setup_count(bars: pd.DataFrame) -> int:
    """TD Sequential SETUP count at the latest bar (oxfordstrat td-sequential-1).

    Walks the close series and counts the current consecutive run of:
      - DOWN closes (Close[i] < Close[i-4])  → BUY setup  → returned NEGATIVE
      - UP   closes (Close[i] > Close[i-4])  → SELL setup → returned POSITIVE
    The run RESETS to 0 whenever the comparison direction flips or ties (Close[i]
    == Close[i-4] breaks the run). Returns the SIGNED count of the run ending at
    the latest bar (e.g. -9 = a completed 9-bar buy setup / bottom exhaustion;
    +9 = a 9-bar sell setup / top exhaustion). 0 if fewer than 5 closes.

    Per Oxford: a BUY setup (9 declining closes) is bottom exhaustion → the long
    fires on the subsequent up-flip (Method 2), handled in the strategy."""
    c = bars['close'].to_numpy(dtype=float)
    m = len(c)
    if m < 5:
        return 0
    sign = 0       # +1 up-run, -1 down-run, 0 none
    count = 0
    for i in range(4, m):
        if not (np.isfinite(c[i]) and np.isfinite(c[i - 4])):
            sign, count = 0, 0
            continue
        if c[i] < c[i - 4]:
            cur = -1
        elif c[i] > c[i - 4]:
            cur = 1
        else:
            cur = 0  # tie breaks the run
        if cur == 0:
            sign, count = 0, 0
        elif cur == sign:
            count += 1
        else:
            sign, count = cur, 1
    return sign * count


class OxfordBaseStrategy(BaseStrategy):
    """BaseStrategy + lazy, cached, point-in-time basket OHLC self-load."""
    instrument_class = 'etp'

    def __init__(self, parameters: dict = None):
        super().__init__(parameters)
        self._ohlc_cache: Optional[Dict[str, pd.DataFrame]] = None

    def _load_basket_ohlc(self) -> Dict[str, pd.DataFrame]:
        if self._ohlc_cache is not None:
            return self._ohlc_cache
        cache: Dict[str, pd.DataFrame] = {}
        try:
            df = pd.read_parquet(
                _prices_parquet_path(),
                columns=['ticker','date','open','high','low','close'],
                filters=[('ticker','in',OXFORD_ETF_BASKET)])
            df['date'] = pd.to_datetime(df['date'])
            for t, g in df.groupby('ticker'):
                cache[t] = g.set_index('date')[['open','high','low','close']].sort_index()
        except Exception:
            cache = {}
        self._ohlc_cache = cache
        return cache

    def basket_ohlc(self, prices: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """Point-in-time OHLC per basket ticker, sliced to the signal date (no look-ahead)."""
        if prices is None or prices.empty:
            return {}
        asof = prices.index[-1]
        out: Dict[str, pd.DataFrame] = {}
        for t, bars in self._load_basket_ohlc().items():
            b = bars.loc[:asof]
            if len(b) >= self.min_lookback:
                out[t] = b
        return out

    # NOTE: brackets are NOT computed here. All oxf_* strategies use the house
    # BaseStrategy.compute_stops_and_targets (regime-scaled ATR(14)×2 stop +
    # 5/10/20% targets) — the SAME risk management every other candidate on the
    # page uses, so metrics are comparable. The Oxford contribution is the entry
    # SIGNAL; sizing/brackets are house-standard. (A custom ATR×6 Oxford bracket
    # was considered and rejected: it deviates from the book and, with the 21-day
    # house max_hold, would just make these strategies incomparable scalps.)
