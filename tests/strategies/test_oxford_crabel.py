import pandas as pd, numpy as np
from strategies.oxford_crabel import (
    atr, donchian_prev, sma, rsi_wilder, true_range_series,
    avg_noise, is_nrn, gap_dir, OXFORD_ETF_BASKET,
    ema, macd, roc, linreg_slope, hma, zlma, kaufman_ama, frama, vortex,
    aroon, bollinger, keltner, heikin_ashi, heikin_ashi_series,
    swing_pivots, td_setup_count, gsv)

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


# --- Batch-2 indicators (pattern / structure / bands) ----------------------
# Oxford formulas fetched 2026-06-15 from oxfordstrat.com per-strategy pages.

def test_aroon_up_100_when_current_is_highest():
    # AroonUp = 100*(n - bars_since_highest_high_in_(n+1)) / n.
    # If the LAST bar is the highest high in the last n+1 bars → 0 bars since → 100.
    n = 5
    b = _bars(highs=[10, 11, 12, 13, 14, 20], lows=[1] * 6, closes=[5] * 6)
    up, dn = aroon(b, n)
    assert abs(up - 100.0) < 1e-9

def test_aroon_down_100_when_current_is_lowest():
    n = 5
    b = _bars(highs=[20] * 6, lows=[14, 13, 12, 11, 10, 1], closes=[5] * 6)
    up, dn = aroon(b, n)
    assert abs(dn - 100.0) < 1e-9

def test_aroon_up_zero_when_oldest_is_highest():
    # Highest high is the OLDEST bar of the n+1 window → n bars since → AroonUp = 0.
    n = 5
    b = _bars(highs=[99, 11, 12, 13, 14, 15], lows=[1] * 6, closes=[5] * 6)
    up, dn = aroon(b, n)
    assert abs(up - 0.0) < 1e-9

def test_aroon_uptrend_up_dominates():
    n = 14
    m = n + 5
    hi = np.arange(1.0, 1.0 + m)
    b = _bars(highs=list(hi), lows=list(hi - 1), closes=list(hi - 0.5))
    up, dn = aroon(b, n)
    assert up > dn and up == 100.0

def test_bollinger_upper_above_lower():
    # Upper = SMA + k*std (population sigma); lower = SMA - k*std.
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    mid, up, lo = bollinger(s, n=10, k=2.0)
    assert lo < mid < up
    sigma = float(np.std(s.to_numpy(), ddof=0))
    assert abs(mid - 5.5) < 1e-9
    assert abs(up - (5.5 + 2.0 * sigma)) < 1e-9

def test_bollinger_constant_series_collapses():
    s = pd.Series([4.0] * 20)
    mid, up, lo = bollinger(s, n=20, k=2.0)
    assert abs(up - 4.0) < 1e-9 and abs(lo - 4.0) < 1e-9

def test_keltner_uses_average_range_not_atr():
    # Oxford keltner-channels-1: center = SMA(typical_price), band = SMA(High-Low)*mult.
    # NOT ATR. Build bars where typical price and range are constant → easy to verify.
    n = 4
    b = _bars(highs=[12.0] * 6, lows=[8.0] * 6, closes=[10.0] * 6)  # TP=(12+8+10)/3=10, range=4
    mid, buy, sell = keltner(b, lb=n, mult=1.0)
    assert abs(mid - 10.0) < 1e-9
    assert abs(buy - (10.0 + 4.0)) < 1e-9   # 14
    assert abs(sell - (10.0 - 4.0)) < 1e-9  # 6

def test_keltner_multiplier_scales_band():
    n = 4
    b = _bars(highs=[12.0] * 6, lows=[8.0] * 6, closes=[10.0] * 6)
    _, buy2, _ = keltner(b, lb=n, mult=2.0)
    assert abs(buy2 - (10.0 + 8.0)) < 1e-9  # range 4 * mult 2 = 8

def test_heikin_ashi_constant_series_fixed_point():
    # On a flat O=H=L=C series, HA candle collapses to that constant (open==close).
    n = 30
    b = _bars(highs=[5.0] * n, lows=[5.0] * n, closes=[5.0] * n, opens=[5.0] * n)
    ha_open, ha_close, ha_high, ha_low = heikin_ashi(b)
    assert abs(ha_open - 5.0) < 1e-9 and abs(ha_close - 5.0) < 1e-9

def test_heikin_ashi_bullish_on_uptrend():
    # Steady uptrend → latest HA candle bullish (HAclose > HAopen).
    n = 30
    base = np.arange(1.0, 1.0 + n)
    b = _bars(highs=list(base + 0.5), lows=list(base - 0.5),
              closes=list(base + 0.3), opens=list(base - 0.3))
    ha_open, ha_close, ha_high, ha_low = heikin_ashi(b)
    assert ha_close > ha_open

def test_heikin_ashi_series_recursion_open():
    # HaOpen[i] = (HaOpen[i-1] + HaClose[i-1])/2; HaClose[i] = mean(O,H,L,C).
    b = _bars(highs=[11.0, 13.0], lows=[9.0, 11.0], closes=[10.0, 12.0], opens=[10.0, 12.0])
    ser = heikin_ashi_series(b)
    # bar0 seed: HaClose0=(10+11+9+10)/4=10; HaOpen0=(10+10)/2=10
    assert abs(ser['ha_close'].iloc[0] - 10.0) < 1e-9
    assert abs(ser['ha_open'].iloc[0] - 10.0) < 1e-9
    # bar1: HaClose1=(12+13+11+12)/4=12; HaOpen1=(HaOpen0+HaClose0)/2=(10+10)/2=10
    assert abs(ser['ha_close'].iloc[1] - 12.0) < 1e-9
    assert abs(ser['ha_open'].iloc[1] - 10.0) < 1e-9

def test_swing_pivots_detects_confirmed_high_only():
    # A peak at index 3 (value 20) with k=2 lower bars on each side is a swing HIGH.
    # The pivot must be CONFIRMED: needs k bars to its right. With k=2 and a peak at
    # index 3 in a length-8 series, index 3 IS confirmable (right bars 4,5 exist).
    closes = [10, 12, 15, 20, 16, 14, 13, 11]
    b = _bars(highs=closes, lows=[c - 1 for c in closes], closes=closes)
    highs, lows = swing_pivots(b, k=2)
    assert len(highs) >= 1
    # most recent confirmed swing high is at index 3 (price 20)
    assert highs[-1][0] == 3 and abs(highs[-1][1] - 20.0) < 1e-9

def test_swing_pivots_no_lookahead_recent_bars_excluded():
    # The last k bars can NEVER be a confirmed pivot (no right-side confirmation).
    # Build a monotonic ramp where the final bar is the highest — it must NOT be
    # reported as a swing high (it has no right neighbours).
    closes = list(range(1, 13))
    b = _bars(highs=closes, lows=[c - 1 for c in closes], closes=closes)
    highs, lows = swing_pivots(b, k=2)
    last_idx = len(closes) - 1
    assert all(h[0] <= last_idx - 2 for h in highs)  # all highs confirmed (>=k from end)

def test_swing_pivots_low_detection():
    closes = [20, 18, 15, 8, 12, 14, 16, 19]
    b = _bars(highs=[c + 1 for c in closes], lows=closes, closes=closes)
    highs, lows = swing_pivots(b, k=2)
    assert len(lows) >= 1
    assert lows[-1][0] == 3 and abs(lows[-1][1] - 8.0) < 1e-9

def test_td_setup_count_buy_setup_reaches_nine():
    # Buy setup = consecutive closes < close[i-4]. Build 4 anchor bars + 9 declining
    # closes each below the close 4 bars earlier → setup count 9, signed negative (buy).
    anchor = [100.0, 100.0, 100.0, 100.0]
    decline = [95.0, 94.0, 93.0, 92.0, 91.0, 90.0, 89.0, 88.0, 87.0]
    closes = anchor + decline
    b = _bars(highs=closes, lows=closes, closes=closes)
    cnt = td_setup_count(b)
    assert cnt == -9  # buy setup of 9 (negative sign = down-run / bottom exhaustion)

def test_td_setup_count_sell_setup_positive():
    anchor = [100.0, 100.0, 100.0, 100.0]
    rise = [105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0]
    closes = anchor + rise
    b = _bars(highs=closes, lows=closes, closes=closes)
    cnt = td_setup_count(b)
    assert cnt == 9  # sell setup of 9 (positive = up-run / top exhaustion)

def test_td_setup_count_resets_on_break():
    # A break in the run resets the count.
    anchor = [100.0, 100.0, 100.0, 100.0]
    closes = anchor + [95.0, 94.0, 99.0, 92.0]  # 3rd decline-bar (99) breaks (>=anchor 100)
    b = _bars(highs=closes, lows=closes, closes=closes)
    cnt = td_setup_count(b)
    # last bar (92<100) is 1 fresh down-bar after the break (90 vs 99 anchor? -> close[i-4])
    # i=7 close 92 vs close[3]=100 -> down (count from break). |cnt| < 9
    assert abs(cnt) < 9

def test_heikin_ashi_look_back_1_equals_raw():
    # Oxford default Look_Back=1 -> AvgOHLC == raw OHLC -> identical to no smoothing.
    n = 20
    base = np.arange(1.0, 1.0 + n)
    b = _bars(highs=list(base + 0.5), lows=list(base - 0.5),
              closes=list(base + 0.2), opens=list(base - 0.1))
    raw = heikin_ashi_series(b)
    lb1 = heikin_ashi_series(b, look_back=1)
    for col in ('ha_open', 'ha_close', 'ha_high', 'ha_low'):
        assert np.allclose(raw[col].to_numpy(), lb1[col].to_numpy(), equal_nan=True)

# --- Batch-3 indicator: Greatest Swing Value (gsv) -------------------------
# Oxford greatest-swing-value-trend (fetched 2026-06-15):
#   If Close>Open: Noise = Open - Low     (down-shadow on an up day)
#   If Close<Open: Noise = High - Open    (up-shadow on a down day)
#   If Close==Open: Noise = min(Open-Low, High-Open)
#   Average_Noise = SMA(Noise, GSV_Length);  GSV = Average_Noise * GSV_Multiple
# gsv() returns Average_Noise (the directional-noise SMA); the strategy applies
# the multiple, so this is NOT double-multiplied.

def test_gsv_up_days_uses_open_minus_low():
    # All up days (close>open): Noise = open-low. open=10, low=8 -> noise=2 each.
    n = 4
    b = _bars(highs=[12.0] * 6, lows=[8.0] * 6, closes=[11.0] * 6, opens=[10.0] * 6)
    assert abs(gsv(b, n) - 2.0) < 1e-9  # SMA of constant 2.0

def test_gsv_down_days_uses_high_minus_open():
    # All down days (close<open): Noise = high-open. high=12, open=10 -> noise=2.
    n = 4
    b = _bars(highs=[12.0] * 6, lows=[8.0] * 6, closes=[9.0] * 6, opens=[10.0] * 6)
    assert abs(gsv(b, n) - 2.0) < 1e-9

def test_gsv_doji_uses_min_shadow():
    # close==open: Noise = min(open-low, high-open) = min(10-8, 12-10) = 2.
    n = 4
    b = _bars(highs=[12.0] * 6, lows=[8.0] * 6, closes=[10.0] * 6, opens=[10.0] * 6)
    assert abs(gsv(b, n) - 2.0) < 1e-9

def test_gsv_averages_over_lookback():
    # Mixed noises: last 3 up days noise = open-low = [1,2,3]; SMA(3) = 2.0.
    opens = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    lows = [10.0, 10.0, 10.0, 9.0, 8.0, 7.0]   # last 3: open-low = 1,2,3
    closes = [10.5] * 6  # up days
    highs = [11.0] * 6
    b = _bars(highs=highs, lows=lows, closes=closes, opens=opens)
    assert abs(gsv(b, 3) - 2.0) < 1e-9  # mean(1,2,3)

def test_gsv_nan_when_short():
    b = _bars(highs=[1, 2], lows=[0, 1], closes=[1, 2], opens=[1, 2])
    assert gsv(b, 10) != gsv(b, 10) or gsv(b, 10) != gsv(b, 10)  # NaN (len<n)
    import numpy as _np
    assert _np.isnan(gsv(b, 10))


def test_heikin_ashi_look_back_smooths_ohlc_first():
    # Oxford page (verbatim): AvgOpen[i]=Average(Open,Look_Back) etc., THEN the HA
    # transform on the averaged series; HaHigh=max(AvgHigh,HaOpen,HaClose). Verify
    # the transform runs on the trailing-SMA-smoothed OHLC, not the raw OHLC.
    n = 12
    opens = [10.0, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0, 14.0, 16.0, 15.0, 17.0]
    highs = [o + 1.0 for o in opens]
    lows = [o - 1.0 for o in opens]
    closes = [o + 0.5 for o in opens]
    b = _bars(highs=highs, lows=lows, closes=closes, opens=opens)
    lb = 3
    ser = heikin_ashi_series(b, look_back=lb)
    # Build the expected smoothed OHLC (trailing SMA, min_periods=1 so early bars defined).
    df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows, 'close': closes})
    avg = df.rolling(lb, min_periods=1).mean()
    ao, ah, al, ac = (avg['open'].to_numpy(), avg['high'].to_numpy(),
                      avg['low'].to_numpy(), avg['close'].to_numpy())
    exp_close = (ao + ah + al + ac) / 4.0
    exp_open = np.empty(n)
    exp_open[0] = (ao[0] + ac[0]) / 2.0
    for i in range(1, n):
        exp_open[i] = (exp_open[i - 1] + exp_close[i - 1]) / 2.0
    exp_high = np.maximum.reduce([ah, exp_open, exp_close])
    exp_low = np.minimum.reduce([al, exp_open, exp_close])
    assert np.allclose(ser['ha_close'].to_numpy(), exp_close)
    assert np.allclose(ser['ha_open'].to_numpy(), exp_open)
    assert np.allclose(ser['ha_high'].to_numpy(), exp_high)
    assert np.allclose(ser['ha_low'].to_numpy(), exp_low)
    # Smoothed differs from raw (sanity: smoothing actually changed something).
    raw = heikin_ashi_series(b)
    assert not np.allclose(ser['ha_close'].to_numpy(), raw['ha_close'].to_numpy())
