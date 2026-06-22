import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import exit_sim as e
from generator import generate, Policy_keys  # Policy_keys: tuple of required keys

CFG = e.Config(mc_paths=20_000, mc_dt=0.5, seed=7, a_grid=(0.5, 5.0, 0.5))

def _strats(direction):
    return [e.Strategy(f"s{i}", sharpe=1.0, half_life=h, direction=direction)
            for i, h in enumerate([6, 12, 24])]

def test_long_dispatch_shape():
    s = _strats(+1)
    ctx = e.Context(C=np.eye(3), sigma_underlying=1.0, hurdle_g_star=0.03, entry_price=100.0)
    p = generate(s, ctx, CFG)
    assert set(Policy_keys) <= set(p)
    assert p["direction"] == 1
    assert p["stop_dist"] > 0 and len(p["takes"]) == 3

def test_short_is_carryzero_mirror_of_long():
    # T-SYM at the harness level: carry=0 short == long mirror (distances equal)
    sL, sS = _strats(+1), _strats(-1)
    ctxL = e.Context(C=np.eye(3), sigma_underlying=1.0, hurdle_g_star=0.03, entry_price=100.0)
    ctxS = e.Context(C=np.eye(3), sigma_underlying=1.0, hurdle_g_star=0.03, entry_price=100.0)
    setattr(ctxS, "carry_per_bar", 0.0)
    pL, pS = generate(sL, ctxL, CFG), generate(sS, ctxS, CFG)
    assert abs(pL["stop_dist"] - pS["stop_dist"]) < 1e-9
    assert pS["direction"] == -1

def test_mixed_sign_rejected():
    s = _strats(+1); s[1].direction = -1
    ctx = e.Context(C=np.eye(3), sigma_underlying=1.0)
    try:
        generate(s, ctx, CFG); assert False, "should reject"
    except ValueError as ex:
        assert "mixed-sign" in str(ex)
