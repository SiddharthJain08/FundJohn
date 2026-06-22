# harness/tests/test_smoke.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import run_tdom


def test_smoke_strided_slice_completes():
    # A representative tiny slice: strided draw across the full window so it
    # spans days/regimes/sides (a same-day head of 30 is leg-thin after the
    # eligible-prune). Design doc Sec.8.7: "~50-cluster dry run completes".
    out = run_tdom.run(window_start="2026-05-04", half_life_mode="cadence",
                       carry_mode="zero", sample=60, seed=0, n_boot=300)
    assert "combined" in out and "min_stop_cumulative" in out["combined"]
    d = out["combined"]["min_stop_cumulative"]
    assert set(["delta", "lo", "hi"]).issubset(d)
    assert out["n_trades"] >= 20
