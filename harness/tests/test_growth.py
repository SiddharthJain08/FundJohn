# harness/tests/test_growth.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import growth as G

def _t(day, R, tau, s=0.02): return dict(day=day, R_ret=R, tau=tau, sigma_ret=s)

def test_growth_hand_value():
    tr = [_t("d1", 0.02, 5.0), _t("d1", 0.0, 10.0)]   # R = 1.0, 0.0 ; tau 5,10
    # mean ln(1+0.5*[1,0]) = (ln1.5+ln1)/2 = 0.2027; mean tau = 7.5 ; G=0.02703
    assert abs(G.growth_G(tr) - 0.027031) < 1e-4

def test_bootstrap_brackets_point():
    rng = np.random.default_rng(0)
    A = [_t(f"d{i%20}", 0.03+0.001*rng.standard_normal(), 6.0) for i in range(400)]
    B = [_t(f"d{i%20}", 0.01+0.001*rng.standard_normal(), 6.0) for i in range(400)]
    out = G.bootstrap_delta(A, B, n_boot=500, seed=1)
    assert out["lo"] <= out["delta"] <= out["hi"]
    assert out["delta"] > 0 and out["lo"] > 0           # A clearly dominates
    assert 0.0 <= out["p_gt0"] <= 1.0

def test_degenerate_zero_width():
    tr = [_t("d1", 0.02, 5.0)]
    out = G.bootstrap_delta(tr, tr, n_boot=50, seed=0)
    assert abs(out["delta"]) < 1e-12 and abs(out["hi"]-out["lo"]) < 1e-9
