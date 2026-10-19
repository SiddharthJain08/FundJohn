import numpy as np
import pandas as pd
import pytest

from strategies.windows import (episodes_to_trades, event_window, session_gaps,
                                state_window, statecount, stateduration)


def B(*v):
    return pd.Series([bool(x) for x in v], dtype=bool)


def L(s):
    return list(s.values)


def test_state_window_nan_breaks_both_modes():
    k = pd.Series(['A', 'A', np.nan, 'A', 'B', 'B'])
    assert L(state_window(k)) == [1, 1, 0, 2, 3, 3]
    assert L(state_window(k, nan_breaks=False)) == [1, 1, 0, 1, 2, 2]
    assert L(state_window(pd.Series(['A', None, 'A']))) == [1, 0, 2]
    assert L(state_window(pd.Series(['A', None, 'A']), nan_breaks=False)) == [1, 0, 1]


def test_statecount_and_stateduration():
    idx = pd.to_datetime(['2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08',
                          '2024-01-09', '2024-01-10', '2024-01-11'])
    c = pd.Series([False, True, True, True, False, True, True], index=idx)
    assert L(statecount(c)) == [0, 1, 2, 3, 0, 1, 2]
    # Fri 01-05 -> Mon 01-08 spans a weekend: calendar days
    assert L(stateduration(c)) == [-1, 0, 1, 4, -1, 0, 1]
    with pytest.raises(ValueError):
        stateduration(B(1, 1))


def test_event_window_basic_collapse_and_noop_end():
    s = B(0, 1, 1, 0, 0, 0, 1, 0)
    e = B(1, 0, 0, 0, 1, 1, 0, 1)
    assert L(event_window(s, e)) == [0, 1, 1, 1, 0, 0, 2, 0]


def test_event_window_same_bar_start_and_end_both_modes():
    # flat case
    s = B(0, 1, 0)
    e = B(0, 1, 0)
    assert L(event_window(s, e, close_unfinished=True)) == [0, 1, 1]
    assert L(event_window(s, e, same_bar_single=True)) == [0, 0, 0]
    # open case
    s = B(1, 0, 1, 0, 0)
    e = B(0, 0, 1, 0, 1)
    assert L(event_window(s, e)) == [1, 1, 2, 2, 0]
    assert L(event_window(s, e, same_bar_single=True)) == [1, 1, 0, 0, 0]


def test_event_window_true_for_hysteresis():
    s = B(1, 0, 0, 0, 0, 0, 0)
    e = B(0, 0, 1, 0, 1, 1, 0)
    assert L(event_window(s, e)) == [1, 1, 0, 0, 0, 0, 0]
    assert L(event_window(s, e, end_true_for=2)) == [1, 1, 1, 1, 1, 0, 0]
    s2 = B(1, 0, 1, 1, 0, 0)
    e2 = B(0, 0, 0, 0, 0, 1)
    assert L(event_window(s2, e2, start_true_for=2)) == [0, 0, 0, 1, 1, 0]


def test_event_window_close_unfinished_both_modes():
    s = B(1, 0, 0, 0, 1, 0)
    e = B(0, 0, 1, 0, 0, 0)
    assert L(event_window(s, e)) == [1, 1, 0, 0, 0, 0]
    assert L(event_window(s, e, close_unfinished=True)) == [1, 1, 0, 0, 2, 2]


def test_event_window_rejects_nan_and_non_bool():
    ok = B(0, 1, 0)
    with pytest.raises(ValueError):
        event_window(pd.Series([0.0, np.nan, 1.0]), ok)
    with pytest.raises(ValueError):
        event_window(pd.Series([0, 1, 0]), ok)
    with pytest.raises(ValueError):
        event_window(ok, pd.Series([True, None, False]))


def test_session_gaps():
    idx = pd.to_datetime(['2024-01-01 09:30', '2024-01-01 09:31', '2024-01-01 09:32',
                          '2024-01-02 09:30', '2024-01-02 09:31', '2024-01-02 11:00'])
    assert L(session_gaps(idx, '5min')) == [1, 1, 1, 2, 2, 3]
    assert L(session_gaps(idx, '1D')) == [1, 1, 1, 1, 1, 1]


def test_episodes_to_trades_long_short_and_final_bar_close():
    dates = pd.date_range('2024-01-01', periods=8)
    price = np.array([10, 11, 12, 10, 10, 20, 22, 24.0])
    ids = np.array([0, 1, 1, 0, 2, 2, 0, 3])
    rows = episodes_to_trades(ids, price, dates)
    assert [r['signal_date'] for r in rows] == ['2024-01-02', '2024-01-05', '2024-01-08']
    assert [r['exit_date'] for r in rows] == ['2024-01-04', '2024-01-07', '2024-01-08']
    assert rows[0]['pnl'] == pytest.approx((10 - 11) / 11)
    assert rows[2]['pnl'] == 0.0 and rows[2]['exit_price'] == 24.0
    assert all(r['regime_state'] is None and r['direction'] == 'LONG' for r in rows)
    sh = episodes_to_trades(ids, price, dates, direction='SHORT', regime='X')
    assert sh[0]['pnl'] == pytest.approx((11 - 10) / 11) and sh[0]['regime_state'] == 'X'
    dr = pd.Series(['LONG', 'LONG', 'LONG', 'LONG', 'SHORT', 'SHORT', 'LONG', 'LONG'])
    mixed = episodes_to_trades(ids, price, dates, direction=dr)
    assert mixed[1]['direction'] == 'SHORT'
    assert mixed[1]['pnl'] == pytest.approx((10 - 22) / 10)


# ---- loop-parity fixtures (copies of existing strategy loops) --------------

def _golden_loop(s, fast, slow, regime_map):
    rows = []
    arr = s.values.astype(float)
    dates = s.index
    sma_fast = s.rolling(fast).mean().values
    sma_slow = s.rolling(slow).mean().values
    in_position = False
    entry_idx = None
    for idx in range(slow, len(arr)):
        if pd.isna(sma_fast[idx]) or pd.isna(sma_slow[idx]):
            continue
        bullish_state = sma_fast[idx] > sma_slow[idx]
        if in_position and not bullish_state:
            exit_price = arr[idx]
            entry_price = arr[entry_idx]
            raw_ret = (exit_price - entry_price) / entry_price
            sig_date = dates[entry_idx]
            rows.append({'signal_date': str(sig_date.date()),
                         'regime_state': regime_map.get(sig_date, 'LOW_VOL'),
                         'pnl': float(raw_ret)})
            in_position = False
        if not in_position and bullish_state:
            in_position = True
            entry_idx = idx
    return rows


def test_parity_golden_cross_loop():
    fast_n, slow_n = 5, 20
    n = 300
    dates = pd.bdate_range('2022-01-03', periods=n)
    t = np.arange(n)
    s = pd.Series(100 + 0.05 * t + 6 * np.sin(2 * np.pi * t / 41 + 4.5), index=dates)
    regimes = ['LOW_VOL', 'TRANS', 'HIGH_VOL']
    regime_map = {d: regimes[(i // 50) % 3] for i, d in enumerate(dates) if i % 7 != 0}

    ref = _golden_loop(s, fast_n, slow_n, regime_map)

    f = s.rolling(fast_n).mean()
    sl = s.rolling(slow_n).mean()
    start = (f > sl)
    end = (f <= sl)
    pos = np.arange(n)
    start_m = start & (pos >= slow_n)
    end_m = end & (pos >= slow_n)
    ids = event_window(start_m, end_m)
    reg = pd.Series(dates).map(regime_map).fillna('LOW_VOL').values
    got = episodes_to_trades(ids.values, s.values, dates, regime=reg)

    # quirks exercised
    assert bool(start.iloc[slow_n - 1]), 'unmasked start must be True at slow-1'
    assert ids.iloc[slow_n - 1] == 0 and ids.iloc[slow_n] == 1
    assert bool(f.iloc[-1] > sl.iloc[-1]), 'raw state bullish on last bar'
    assert len(ref) >= 3

    key = lambda rows: [(r['signal_date'], r['regime_state'], r['pnl']) for r in rows]
    a, b = key(ref), key(got)
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x[:2] == y[:2]
        assert x[2] == pytest.approx(y[2], abs=1e-12)


def _czar_loop(prices_s, dir_at, min_lb, regime_s, step=5):
    rows, in_trade, ep, ed, er, edir = [], False, None, None, None, None
    for pos in range(min_lb, len(prices_s), step):
        dt, price = prices_s.index[pos], float(prices_s.iloc[pos])
        reg = regime_s.iloc[pos]
        cur_dir = dir_at[pos]
        active = cur_dir is not None
        if active and (not in_trade or cur_dir != edir):
            if in_trade:
                pnl = (price - ep) / ep if edir == 'LONG' else (ep - price) / ep
                rows.append({'signal_date': str(ed)[:10], 'regime_state': er, 'pnl': pnl})
            in_trade, ep, ed, er, edir = True, price, dt, reg, cur_dir
        elif not active and in_trade:
            pnl = (price - ep) / ep if edir == 'LONG' else (ep - price) / ep
            rows.append({'signal_date': str(ed)[:10], 'regime_state': er, 'pnl': pnl})
            in_trade = False
    if in_trade:
        price = float(prices_s.iloc[-1])
        pnl = (price - ep) / ep if edir == 'LONG' else (ep - price) / ep
        rows.append({'signal_date': str(ed)[:10], 'regime_state': er, 'pnl': pnl})
    return rows


def test_parity_czar_loop():
    n, min_lb, step = 200, 30, 5
    assert (n - 1 - min_lb) % step != 0
    rng = np.random.default_rng(7)
    dates = pd.date_range('2023-01-01', periods=n)
    prices_s = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=dates)
    regime_s = pd.Series(np.array(['LOW_VOL', 'TRANS', 'HIGH_VOL', 'CRISIS'])[(np.arange(n) // 23) % 4],
                         index=dates)
    sampled = list(range(min_lb, n, step))
    pattern = [None, 'LONG', 'LONG', 'SHORT', 'SHORT', None, None, 'SHORT', 'LONG', 'LONG', 'LONG',
               None, 'LONG', 'SHORT', None, 'LONG', 'LONG', 'SHORT', 'SHORT', None, 'SHORT',
               'SHORT', 'LONG', None, 'LONG', 'LONG', 'LONG', 'SHORT', 'SHORT', 'SHORT', None,
               'LONG', 'LONG', 'LONG'][:len(sampled)]
    assert len(pattern) == len(sampled)
    dir_at = [None] * n
    for p, d in zip(sampled, pattern):
        dir_at[p] = d
    # quirks: flip, flat gap, run open at the end
    assert any(a and b and a != b for a, b in zip(pattern, pattern[1:]))
    assert any(a and not b for a, b in zip(pattern, pattern[1:]))
    assert pattern[-1] is not None

    ref = _czar_loop(prices_s, dir_at, min_lb, regime_s, step)

    key = pd.Series([dir_at[p] for p in sampled] + [pattern[-1]], dtype=object)
    pos_all = sampled + [n - 1]
    ids = state_window(key)
    got = episodes_to_trades(ids.values, prices_s.iloc[pos_all].values, dates[pos_all],
                             direction=key, regime=regime_s.iloc[pos_all].values)

    k = lambda rows: [(r['signal_date'], r['regime_state'], r['pnl']) for r in rows]
    a, b = k(ref), k(got)
    assert len(a) == len(b) >= 5
    for x, y in zip(a, b):
        assert x[:2] == y[:2]
        assert x[2] == pytest.approx(y[2], abs=1e-12)
