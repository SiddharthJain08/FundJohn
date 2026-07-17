import math
from execution.b1_planner import expected_volume_profile, plan, RTH_BUCKETS, U_SHAPE

def test_profile_fallback_u_shape_when_no_history():
    assert expected_volume_profile([]) == U_SHAPE

def test_profile_is_normalized_and_flat_for_flat_days():
    prof = expected_volume_profile([[10.0]*RTH_BUCKETS, [20.0]*RTH_BUCKETS])
    assert abs(sum(prof) - 1.0) < 1e-9
    assert all(abs(p - 1.0/RTH_BUCKETS) < 1e-9 for p in prof)

def test_plan_slices_sum_to_qty():
    prof = expected_volume_profile([])
    assert abs(sum(q for _, q in plan(1000.0, 0.8, prof, 2.0)) - 1000.0) < 1e-6
    assert abs(sum(q for _, q in plan(-500.0, 0.8, prof, 2.0)) + 500.0) < 1e-6

def test_lam_zero_is_pure_base():
    prof = expected_volume_profile([])
    for t, q in plan(1000.0, 1.0, prof, 0.0):
        assert abs(q - 1000.0 * prof[t]) < 1e-6

def test_si_zero_is_pure_base():
    prof = expected_volume_profile([])
    for t, q in plan(1000.0, 0.0, prof, 5.0):
        assert abs(q - 1000.0 * prof[t]) < 1e-6

def test_higher_si_front_loads():
    prof = expected_volume_profile([])
    early = lambda sl: sum(q for t, q in sl if t < 3)
    assert early(plan(1000.0, 0.9, prof, 3.0)) > early(plan(1000.0, 0.1, prof, 3.0))
