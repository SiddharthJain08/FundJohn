# harness/tests/test_inputs.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import inputs


def test_dsign():
    assert inputs.dsign("LONG") == 1 and inputs.dsign("BUY_VOL") == 1
    assert inputs.dsign("BUY") == 1
    assert inputs.dsign("SHORT") == -1 and inputs.dsign("SELL") == -1
    assert inputs.dsign("SELL_VOL") == -1


def test_carry_tiered_sign_and_scale():
    class Cl: pass
    c = Cl(); c.direction = -1; c.easy_to_borrow = True
    z = inputs.carry_for(c, "zero"); t = inputs.carry_for(c, "tiered")
    assert z == 0.0 and t < 0 and abs(t) < 1e-3      # GC ~0.3%/yr per bar is tiny
    c.easy_to_borrow = False
    assert inputs.carry_for(c, "tiered") < t          # HTB more negative
    c.direction = 1
    assert inputs.carry_for(c, "tiered") == 0.0       # longs: no borrow


def test_C_slice_is_psd_like_and_bounded():
    M = {"a": {"a": 1.0, "b": 0.3}, "b": {"a": 0.3, "b": 1.0}}
    C = inputs.slice_C(["a", "b"], M)
    assert C.shape == (2, 2) and C[0, 1] == 0.3 and C[0, 0] == 1.0
    C2 = inputs.slice_C(["a", "x"], M)                 # missing pair -> default
    assert C2[0, 1] == inputs.DEF_OFFDIAG
    assert C2[0, 0] == 1.0 and C2[1, 1] == 1.0


def test_C_slice_symmetrized():
    # asymmetric input -> symmetrized average
    M = {"a": {"a": 1.0, "b": 0.4}, "b": {"a": 0.2, "b": 1.0}}
    C = inputs.slice_C(["a", "b"], M)
    assert abs(C[0, 1] - C[1, 0]) < 1e-12
    assert abs(C[0, 1] - 0.3) < 1e-12                  # (0.4 + 0.2)/2
