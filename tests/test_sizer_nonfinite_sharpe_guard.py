"""Regression: the sizer must exclude a regime whose effective Sharpe is
non-finite (NaN/Inf), not just <= 0.

Pure function — no DB. Runs standalone with
`python3 tests/test_sizer_nonfinite_sharpe_guard.py`.

Background: 6 live high-frequency strategies produce a NaN per-regime Sharpe
in HIGH_VOL (their HIGH_VOL daily-return series compounds below -100% ->
cumprod(1+r) <= 0). The old guard `eff is None or eff <= 0` does NOT catch
NaN — `nan <= 0` is False in IEEE/Python — so a NaN weight would slip through
and poison the Sigma-effective-sharpe gate + daily_weight if the regime
turned HIGH_VOL. The guard must treat any non-finite Sharpe as un-sizeable.
"""
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from execution.strategy_weights import _is_sizeable_sharpe


def test_finite_positive_is_sizeable():
    assert _is_sizeable_sharpe(1.5) is True
    assert _is_sizeable_sharpe(0.001) is True


def test_nonpositive_excluded():
    assert _is_sizeable_sharpe(0.0) is False
    assert _is_sizeable_sharpe(-1.0) is False


def test_none_excluded():
    assert _is_sizeable_sharpe(None) is False


def test_nonfinite_excluded():
    # The core regression: NaN/Inf must be excluded.
    assert _is_sizeable_sharpe(float('nan')) is False
    assert _is_sizeable_sharpe(float('inf')) is False
    assert _is_sizeable_sharpe(float('-inf')) is False


def test_demonstrates_old_guard_leaked_nan():
    """The replaced inline predicate (`eff is None or eff <= 0`) did NOT
    exclude NaN — this documents exactly the hole the helper closes."""
    nan = float('nan')
    old_would_exclude = (nan is None or nan <= 0)   # -> False (leak!)
    assert old_would_exclude is False
    assert _is_sizeable_sharpe(nan) is False        # new guard closes it


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn(); print(f'ok  {name}')
    print('ALL PASS')
