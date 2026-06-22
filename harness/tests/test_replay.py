import sys, os, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import replay as R

def _bars(rows):  # rows: list of (high, low, close)
    idx = pd.date_range("2026-05-04", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["high","low","close"], index=idx)

def P(stop, takes, tsb=30, d=1):
    return dict(stop_dist=stop, takes=[dict(distance=b, fraction=f) for b,f in takes],
               time_stop_bars=tsb, direction=d)

def test_take_only_full():
    bars = _bars([(101,99,100),(106,100,105)])   # long, entry 100
    o = R.first_touch_multiday(P(5,[(5,1.0)]), bars, 100.0)
    assert o["exit_kind"] == "take" and abs(o["R"] - 0.05) < 1e-9 and o["tau"] == 1

def test_stop_only():
    bars = _bars([(101,99,100),(101,94,95)])      # hits -5
    o = R.first_touch_multiday(P(5,[(20,1.0)]), bars, 100.0)
    assert o["exit_kind"] == "stop" and abs(o["R"] + 0.05) < 1e-9

def test_stop_wins_on_tie():
    bars = _bars([(101,99,100),(106,94,100)])      # both +5 take and -5 stop in one bar
    o = R.first_touch_multiday(P(5,[(5,1.0)]), bars, 100.0)
    assert o["exit_kind"] == "stop"

def test_partial_take_then_timestop():
    bars = _bars([(101,99,100),(106,100,105),(105,101,103),(104,102,103)])
    o = R.first_touch_multiday(P(20,[(5,0.5)], tsb=3), bars, 100.0)
    assert 0 < o["frac_filled"] < 1.0001 and o["exit_kind"] in ("time","take")
    # 0.5 booked at +5%, 0.5 marked at close[3]=+3%  -> ~ 0.5*0.05+0.5*0.03 = 0.04
    assert abs(o["R"] - 0.04) < 2e-3

def test_short_carry_reduces_return():
    bars = _bars([(101,99,100)] + [(101,99,100)]*10)   # flat, short, no touch
    base = R.first_touch_multiday(P(20,[(20,1.0)], tsb=10, d=-1), bars, 100.0, carry_per_bar=0.0)
    carr = R.first_touch_multiday(P(20,[(20,1.0)], tsb=10, d=-1), bars, 100.0, carry_per_bar=-0.001)
    assert carr["R"] < base["R"] - 1e-6
