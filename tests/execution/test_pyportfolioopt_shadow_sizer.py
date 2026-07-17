"""Phase 1G — shadow-sizer tests.  Pure-function compute path; no DB.

We test only the in-memory compute (allocate -> diff vs live).  DB persistence
is exercised by the script-level smoke test, not unit tests."""
import pandas as pd
import numpy as np


def _synthetic_returns(n_days=252, n_assets=5, seed=7):
    rng = np.random.default_rng(seed)
    cols = [f"S{i}" for i in range(n_assets)]
    return pd.DataFrame(
        rng.normal(loc=0.0005, scale=0.012, size=(n_days, n_assets)),
        columns=cols,
    )


def test_hrp_weights_are_nonneg_and_sum_to_one():
    from src.execution.pyportfolioopt_shadow_sizer import allocate_hrp
    weights = allocate_hrp(_synthetic_returns())
    assert all(w >= -1e-9 for w in weights.values()), weights
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_hrp_is_deterministic_for_fixed_inputs():
    from src.execution.pyportfolioopt_shadow_sizer import allocate_hrp
    a = allocate_hrp(_synthetic_returns(seed=42))
    b = allocate_hrp(_synthetic_returns(seed=42))
    for k in a:
        assert abs(a[k] - b[k]) < 1e-12


def test_diff_vs_live_returns_per_ticker_delta():
    from src.execution.pyportfolioopt_shadow_sizer import compute_diff
    target  = {"AAPL": 5000, "MSFT": 3000, "NVDA": 2000}
    live    = {"AAPL": 4000, "MSFT": 4000, "GOOG": 1000}
    diff = compute_diff(target, live)
    assert diff["AAPL"] == 1000
    assert diff["MSFT"] == -1000
    assert diff["NVDA"] == 2000
    assert diff["GOOG"] == -1000
