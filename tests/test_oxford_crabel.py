import pandas as pd, numpy as np
from strategies.oxford_crabel import (
    atr, donchian_prev, sma, rsi_wilder, true_range_series,
    avg_noise, is_nrn, gap_dir, OXFORD_ETF_BASKET,
    ema, macd, roc, linreg_slope, hma, zlma, kaufman_ama, frama, vortex)

def _bars(highs, lows, closes, opens=None):
    n = len(closes)
    idx = pd.date_range('2020-01-01', periods=n, freq='B')
    opens = opens or closes
    return pd.DataFrame({'open':opens,'high':highs,'low':lows,'close':closes}, index=idx)

def test_atr_true_range():
    b = _bars([11,12,13,14,15,16],[9,10,11,12,13,14],[10,11,12,13,14,15])
    a = atr(b, n=3)
    assert a > 0 and np.isfinite(a)

def test_donchian_prev_excludes_current_bar():
    # Upper channel over prior n bars must NOT include the last (current) bar.
    b = _bars([10,10,10,99],[1,1,1,1],[5,5,5,50])
    up, lo = donchian_prev(b, n=3)
    assert up == 10.0 and lo == 1.0  # the 99 high on the current bar is excluded

def test_sma():
    s = pd.Series([1,2,3,4,5])
    assert sma(s, 5) == 3.0

def test_rsi_bounds():
    s = pd.Series(np.linspace(1, 2, 60))  # monotone up
    r = rsi_wilder(s, 14)
    assert 50 < r <= 100

def test_is_nrn_true_for_narrowest():
    # last bar range smallest of the prior n
    b = _bars([20,20,20,11],[10,10,10,10],[15,15,15,10.5])
    assert is_nrn(b, n=3) is True

def test_gap_dir():
    b = _bars([12,30],[8,25],[10,28],opens=[10,26])
    assert gap_dir(b) == 1  # today's low (25) > yesterday's high (12) → gap up

def test_basket_constant_is_present_tickers():
    assert 'SPY' in OXFORD_ETF_BASKET and 'GLD' in OXFORD_ETF_BASKET
    assert 'FXE' not in OXFORD_ETF_BASKET  # absent from panel, excluded


# --- Batch-1 indicators (golden values derived from each Oxford formula) ---

def test_ema_constant_series_equals_constant():
    # EMA of a constant series converges to (and equals) that constant.
    s = pd.Series([7.0] * 50)
    assert abs(ema(s, 10) - 7.0) < 1e-9

def test_ema_recursion_matches_textbook():
    # alpha=2/(n+1); EMA[i]=alpha*x+(1-alpha)*EMA[i-1], seeded on first value.
    s = pd.Series([1.0, 2.0, 3.0])
    alpha = 2 / (2 + 1)
    e0 = 1.0
    e1 = alpha * 2.0 + (1 - alpha) * e0
    e2 = alpha * 3.0 + (1 - alpha) * e1
    assert abs(ema(s, 2) - e2) < 1e-9

def test_macd_line_constant_series_is_zero():
    # MACD = EMA(fast) - EMA(slow); on a constant series both EMAs == const → 0.
    s = pd.Series([100.0] * 60)
    line = macd(s, 12, 26)
    assert abs(line) < 1e-9

def test_macd_line_positive_for_uptrend():
    s = pd.Series(np.linspace(1, 50, 60))  # rising → fast EMA above slow EMA
    assert macd(s, 12, 26) > 0

def test_roc_constant_is_zero():
    s = pd.Series([5.0] * 30)
    assert abs(roc(s, 10)) < 1e-9

def test_roc_matches_percent_change():
    # ROC(n) = 100*(close - close[-n-1]) / close[-n-1]
    s = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    # n=2 → (14-12)/12 * 100
    assert abs(roc(s, 2) - (14.0 - 12.0) / 12.0 * 100) < 1e-9

def test_linreg_slope_of_linear_ramp():
    # Least-squares slope of a perfect ramp y = 3*t + 7 over n bars == 3.0.
    s = pd.Series([3.0 * t + 7.0 for t in range(20)])
    assert abs(linreg_slope(s, 20) - 3.0) < 1e-9

def test_linreg_slope_sign_down():
    s = pd.Series([100.0 - 2.0 * t for t in range(15)])
    assert linreg_slope(s, 15) < 0

def test_hma_constant_series_equals_constant():
    # HMA of a constant series is that constant (all nested WMAs are the const).
    s = pd.Series([42.0] * 60)
    assert abs(hma(s, 16) - 42.0) < 1e-9

def test_hma_tracks_uptrend_above_simple_mean():
    # On a steady ramp HMA (low-lag) sits above the lagging SMA near the end.
    s = pd.Series(np.linspace(1, 100, 80))
    assert hma(s, 16) > sma(s, 16)

def test_zlma_constant_series_equals_constant():
    # ZLMA recursion on a constant series fixes to the constant (err -> 0).
    s = pd.Series([20.0] * 100)
    z, err = zlma(s, 20, gain=5.0)
    assert abs(z - 20.0) < 1e-6 and abs(err) < 1e-6

def test_zlma_uptrend_above_naive_ema():
    s = pd.Series(np.linspace(1, 100, 100))
    z, err = zlma(s, 20, gain=5.0)
    # Zero-lag construction should lead a plain EMA on a steady uptrend.
    assert z > ema(s, 20)

def test_kaufman_ama_constant_series_equals_constant():
    s = pd.Series([30.0] * 60)
    a = kaufman_ama(s, er_len=20, fast=2, slow=30)
    assert abs(a - 30.0) < 1e-6

def test_kaufman_ama_efficiency_on_trend_tracks_close():
    # On a perfectly efficient (straight-line) trend ER→1 so SC→fast²≈0.444 →
    # AMA rises and converges toward (but lags) the close. Assert it is rising
    # bar-over-bar and within a few points below the last close (59).
    from strategies.oxford_crabel import kaufman_ama_series
    s = pd.Series([float(t) for t in range(60)])
    ser = kaufman_ama_series(s, er_len=10, fast=2, slow=30)
    a = float(ser.iloc[-1])
    assert a > float(ser.iloc[-2])      # rising bar-over-bar
    assert 55.0 < a < 59.0              # tracks just below the efficient close (59)

def test_frama_constant_series_equals_constant():
    # FRAMA on a constant H=L=C series: D well-defined → recursion fixes to const.
    n = 40
    idx = pd.date_range('2020-01-01', periods=n + 20, freq='B')
    b = pd.DataFrame({'open': [9.0] * (n + 20), 'high': [9.0] * (n + 20),
                      'low': [9.0] * (n + 20), 'close': [9.0] * (n + 20)}, index=idx)
    assert abs(frama(b, n) - 9.0) < 1e-6

def test_frama_efficient_trend_alpha_one_tracks_price():
    # A straight-line ramp has fractal dimension D≈1 → alpha≈1 → FRAMA follows
    # the price (High+Low)/2 closely (the smoother does NOT lag an efficient
    # trend; lag only appears on choppy/high-D data). Assert it equals the
    # latest mid-price within a small tolerance.
    n = 16
    m = n + 40
    idx = pd.date_range('2020-01-01', periods=m, freq='B')
    rng = np.linspace(1, 100, m)
    b = pd.DataFrame({'open': rng, 'high': rng + 0.5, 'low': rng - 0.5, 'close': rng}, index=idx)
    f = frama(b, n)
    mid_last = float((b['high'].iloc[-1] + b['low'].iloc[-1]) / 2)
    assert np.isfinite(f) and abs(f - mid_last) < 0.5

def test_frama_choppy_data_lags_below_recent_high():
    # On noisy (high fractal-dimension) data alpha shrinks → FRAMA smooths and
    # sits between the window's extremes (not pinned to the last bar).
    n = 16
    m = n + 60
    rng = np.random.default_rng(3)
    idx = pd.date_range('2020-01-01', periods=m, freq='B')
    base = 100 + np.cumsum(rng.normal(0, 1.5, m))
    b = pd.DataFrame({'open': base, 'high': base + 1.0, 'low': base - 1.0, 'close': base}, index=idx)
    f = frama(b, n)
    win_hi = float(b['high'].iloc[-n:].max())
    win_lo = float(b['low'].iloc[-n:].min())
    assert win_lo <= f <= win_hi

def test_vortex_monotone_up_positive_dominates():
    # On a strict uptrend (each high/low above prior) +VI should exceed -VI.
    n = 14
    m = n + 5
    idx = pd.date_range('2020-01-01', periods=m, freq='B')
    hi = np.arange(2, 2 + m, dtype=float)
    lo = np.arange(1, 1 + m, dtype=float)
    cl = (hi + lo) / 2
    b = pd.DataFrame({'open': cl, 'high': hi, 'low': lo, 'close': cl}, index=idx)
    pvi, nvi = vortex(b, n)
    assert pvi > nvi and nvi >= 0

def test_vortex_returns_nan_when_short():
    idx = pd.date_range('2020-01-01', periods=3, freq='B')
    b = pd.DataFrame({'open': [1, 2, 3], 'high': [1, 2, 3],
                      'low': [1, 2, 3], 'close': [1, 2, 3]}, index=idx)
    pvi, nvi = vortex(b, 14)
    assert pvi != pvi  # NaN
