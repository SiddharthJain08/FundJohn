import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import asset_correlation as ac


def test_corr_perfect_and_anti():
    # 30 obs: b == a (corr +1), c == -a (corr -1)
    a = {f'2026-01-{i:02d}': (i % 7) - 3 + 0.1 * i for i in range(1, 31)}
    b = dict(a)
    c = {d: -v for d, v in a.items()}
    m = ac.corr_from_returns({'A': a, 'B': b, 'C': c})
    assert abs(m['A']['A'] - 1.0) < 1e-9
    assert abs(m['A']['B'] - 1.0) < 1e-9
    assert abs(m['A']['C'] + 1.0) < 1e-9
    assert m['A']['B'] == m['B']['A']


def test_thin_pair_is_zero():
    # only 5 overlapping obs (< MIN_OBS=20) -> 0.0, never cluster on thin evidence
    a = {f'2026-02-{i:02d}': float(i) for i in range(1, 6)}
    b = {f'2026-02-{i:02d}': float(i) for i in range(1, 6)}
    m = ac.corr_from_returns({'A': a, 'B': b})
    assert m['A']['B'] == 0.0


def test_zero_variance_is_zero():
    a = {f'2026-03-{i:02d}': 0.01 for i in range(1, 25)}   # flat
    b = {f'2026-03-{i:02d}': float(i) for i in range(1, 25)}
    m = ac.corr_from_returns({'A': a, 'B': b})
    assert m['A']['B'] == 0.0
