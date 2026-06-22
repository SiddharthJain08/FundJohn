import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import baselines as B

class Cl:  # minimal cluster stub
    def __init__(s):
        s.entry=100.0; s.direction=1
        s.legs=[dict(strategy_id="a", stop_loss=98.0, target_1=104.0),
                dict(strategy_id="b", stop_loss=95.0, target_1=110.0)]

W = {"a":0.6, "b":0.4}; SH = {"a":2.0, "b":1.0}

def test_min_stop_cumulative():
    p = B.min_stop_cumulative(Cl(), W, H_max=30)
    assert abs(p["stop_dist"] - 2.0) < 1e-9          # min(|100-98|,|100-95|)
    assert {round(t["distance"],3) for t in p["takes"]} == {4.0, 10.0}
    assert all(abs(t["fraction"]-0.5)<1e-9 for t in p["takes"])

def test_conf_weighted_atr():
    p = B.conf_weighted_atr(Cl(), W, SH, atr=2.5, H_max=30)
    # stop = (0.6*(2/2.5)+0.4*(5/2.5))*2.5 = (0.6*0.8+0.4*2.0)*2.5 = (0.48+0.8)*2.5 = 3.2
    assert abs(p["stop_dist"] - 3.2) < 1e-9
    # take = (2/3)*4 + (1/3)*10 = 2.667+3.333 = 6.0
    assert abs(p["takes"][0]["distance"] - 6.0) < 1e-9

def test_v2_uncapped_sum():
    p = B.current_live_v2(Cl(), W, H_max=30)
    assert abs(p["takes"][0]["distance"] - 14.0) < 1e-9   # 4 + 10
    assert abs(p["stop_dist"] - 2.0) < 1e-9
