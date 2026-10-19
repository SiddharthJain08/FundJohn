import math, random
from statistics import NormalDist
import pytest
from metrics import intraday_path_montecarlo as ipm
SIG, BARS, STOP = 0.0055, 65, 0.02
P_TRUE = 2 * NormalDist().cdf(math.log(1 - STOP) / (SIG * math.sqrt(BARS)))   # reflection, mu = 0

def _rate(bridge):
    rng, gen, hits = random.Random(12345), ipm.GBMPathGen(0.0, SIG), 0
    for _ in range(20_000):
        _, why, _ = ipm.apply_policy(gen.sample_path(BARS, rng), STOP, None, BARS,
                                     bridge=bridge, sigma_bar=SIG, rng=rng)
        hits += why == "stop"
    return hits / 20_000

def test_bridge_converges_to_reflection_first_passage():
    assert abs(_rate(True) - P_TRUE) < 0.015

def test_without_bridge_is_measurably_below():
    assert _rate(False) < P_TRUE - 0.03

class _U:                                   # fixed uniform
    def __init__(self, u): self.u = u
    def random(self): return self.u

def test_bridge_deterministic_long_stop():
    assert ipm.apply_policy([-0.019], 0.02, None, 1, sigma_bar=0.005, rng=_U(0.0))[:2] == (-0.02, "stop")
    assert ipm.apply_policy([-0.019], 0.02, None, 1, sigma_bar=0.005, rng=_U(0.99))[1] == "max_hold"

def test_bridge_short_stop_and_stop_wins_double_touch():
    assert ipm.apply_policy([0.019], 0.02, None, 1, "SHORT", sigma_bar=0.005, rng=_U(0.0))[1] == "stop"
    assert ipm.apply_policy([0.0], 0.001, 0.001, 1, sigma_bar=0.05, rng=_U(0.0))[1] == "stop"

def test_bridge_skipped_without_bar_sigma(monkeypatch):
    class NoSigma:
        def sample_path(self, n, rng): return [0.0] * n
    monkeypatch.setattr(ipm, "_gen_for_ticker", lambda t, window_days=90: (NoSigma(), "gbm"))
    r = ipm.run_path_mc("S_zz", "LOW_VOL", 1.0, 1.0, 0.02, 0.05, 1, n_iter=20,
                        trade_pool=[{"ticker": "ZZTEST1", "direction": "LONG"}] * 20, seed=1)
    assert r["status"] == "OK" and r["max_hold_hit_rate"] == 1.0
    assert ipm.apply_policy([0.0], 0.02, None, 1, sigma_bar=0.0, rng=_U(0.0))[1] == "max_hold"

def test_kill_switch_and_paths_identical(monkeypatch):
    seen = []
    orig = ipm.apply_policy
    monkeypatch.setattr(ipm, "apply_policy",
                        lambda p, *a, **k: seen.append((tuple(p), k["bridge"])) or orig(p, *a, **k))
    monkeypatch.setattr(ipm, "_gen_for_ticker", lambda t, window_days=90: (ipm.GBMPathGen(0.0, SIG), "gbm"))
    kw = dict(n_iter=30, trade_pool=[{"ticker": "ZZTEST1", "direction": "LONG"}] * 20, seed=5)
    ipm.run_path_mc("S_zz", "LOW_VOL", 1.0, 1.0, 0.02, 0.05, 2, **kw)
    monkeypatch.setenv("OPENCLAW_MC_BRIDGE", "0")
    ipm.run_path_mc("S_zz", "LOW_VOL", 1.0, 1.0, 0.02, 0.05, 2, **kw)
    on, off = seen[:30], seen[30:]
    assert all(b for _, b in on) and not any(b for _, b in off)
    assert [p for p, _ in on] == [p for p, _ in off]

def test_kill_switch_matches_legacy_call():
    gen = ipm.GBMPathGen(0.0, SIG)
    for s in range(50):
        p = gen.sample_path(26, random.Random(s))
        assert ipm.apply_policy(p, 0.02, 0.05, 26, bridge=False, sigma_bar=SIG, rng=random.Random(s)) \
            == ipm.apply_policy(p, 0.02, 0.05, 26)

def test_empirical_bar_sigma():
    assert ipm.EmpiricalPathGen([0.01, -0.01, 0.01, -0.01]).bar_sigma > 0
    assert ipm.EmpiricalPathGen([0.01]).bar_sigma == 0.0
