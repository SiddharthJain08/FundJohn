from execution.b1_simulator import simulate

def _bars(vwaps):  # uniform tight bars (haircut≈impact only)
    return {i: {'vwap': v, 'high': v, 'low': v, 'volume': 100.0} for i, v in enumerate(vwaps)}

def test_buy_filled_below_close_is_positive_ledger():
    plan = [(0, 50.0), (1, 50.0)]
    bars = _bars([99.0, 99.0])
    r = simulate(plan, bars, close_t1=100.0, naive_fill=100.0, impact_bps=0.0)
    assert r['actual_fill'] == 99.0
    assert r['exec_ledger'] > 0          # bought at 99 vs close 100
    assert abs(r['completion'] - 1.0) < 1e-9

def test_haircut_is_adverse_for_buyer():
    plan = [(0, 100.0)]
    bars = {0: {'vwap': 100.0, 'high': 100.5, 'low': 99.5, 'volume': 1.0}}
    r = simulate(plan, bars, close_t1=100.0, impact_bps=0.0)
    assert r['actual_fill'] > 100.0      # buyer pays vwap + half-spread

def test_missing_bar_reduces_completion_and_is_skipped():
    plan = [(0, 50.0), (1, 50.0)]
    bars = {0: {'vwap': 100.0, 'high': 100.0, 'low': 100.0, 'volume': 1.0}}  # bucket 1 absent
    r = simulate(plan, bars, close_t1=100.0, impact_bps=0.0)
    assert abs(r['completion'] - 0.5) < 1e-9
    assert r['filled_qty'] == 50.0

def test_no_fills_returns_zero_ledger():
    r = simulate([(0, 10.0)], {}, close_t1=100.0)
    assert r['filled_qty'] == 0.0 and r['exec_ledger'] == 0.0 and r['actual_fill'] is None
