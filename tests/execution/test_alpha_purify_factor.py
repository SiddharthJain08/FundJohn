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


def test_ic_returns_nan_on_small_sample():
    """n=10 is below the 20-threshold; ic should return NaN, not a noisy number."""
    import numpy as np
    import pandas as pd
    from src.services.alpha_purify_factor import ic_against_returns
    rng = np.random.default_rng(0)
    factor = pd.Series(rng.normal(size=10))
    forward_ret = pd.Series(rng.normal(size=10))
    result = ic_against_returns(factor, forward_ret)
    assert pd.isna(result), f"expected NaN for n=10, got {result}"


def test_ic_raises_on_index_mismatch():
    """Mismatched indices should raise loudly, not silently return NaN."""
    import numpy as np
    import pandas as pd
    import pytest
    from src.services.alpha_purify_factor import ic_against_returns
    rng = np.random.default_rng(0)
    factor = pd.Series(rng.normal(size=50), index=pd.RangeIndex(50))
    forward_ret = pd.Series(rng.normal(size=50),
                            index=pd.date_range("2026-01-01", periods=50, freq="D"))
    with pytest.raises(ValueError, match="index mismatch"):
        ic_against_returns(factor, forward_ret)
