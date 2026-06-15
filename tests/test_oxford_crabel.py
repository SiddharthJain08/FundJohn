import pandas as pd, numpy as np
from strategies.oxford_crabel import (
    atr, donchian_prev, sma, rsi_wilder, true_range_series,
    avg_noise, is_nrn, gap_dir, OXFORD_ETF_BASKET)

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
