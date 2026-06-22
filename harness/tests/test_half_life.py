# harness/tests/test_half_life.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import half_life as hl

def _ar1(rho, n=4000, seed=0):
    rng = np.random.default_rng(seed); x = np.zeros(n)
    for t in range(1, n):
        x[t] = rho * x[t-1] + rng.standard_normal()
    return x

def test_recovers_known_half_life():
    rho = 0.5 ** (1/10.0)             # true half-life 10
    est = hl.autocorr_half_life(_ar1(rho))
    assert 7.0 <= est <= 14.0, est

def test_meanreverting_returns_floor():
    assert hl.autocorr_half_life(_ar1(-0.3)) == 1.0

def test_too_short_returns_nan():
    import math
    assert math.isnan(hl.autocorr_half_life(np.array([0.1, -0.2, 0.05])))
