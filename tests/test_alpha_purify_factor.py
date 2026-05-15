"""Phase 1C — AlphaPurify smoke: feed a synthetic alpha column, verify cleaning
(winsorize + zscore) returns finite values and computes a directional IC."""
import numpy as np
import pandas as pd


def test_clean_and_ic_on_synthetic_alpha():
    from src.services.alpha_purify_factor import clean_factor, ic_against_returns
    rng = np.random.default_rng(42)
    n = 500
    alpha = rng.normal(size=n)
    alpha[0] = 1e9   # outlier
    alpha[1] = -1e9  # outlier
    forward_ret = 0.3 * alpha + rng.normal(size=n) * 0.5

    cleaned = clean_factor(pd.Series(alpha))
    assert np.isfinite(cleaned).all()
    assert cleaned.abs().max() < 10

    ic = ic_against_returns(cleaned, pd.Series(forward_ret))
    assert ic > 0.10
