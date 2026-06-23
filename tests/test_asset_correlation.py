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


def test_price_return_corr_failopen(monkeypatch):
    # force the loader to raise -> fail-open empty dict (never blocks a cycle)
    def boom(*a, **k):
        raise RuntimeError("parquet unavailable")
    monkeypatch.setattr(ac, "_load_returns", boom)
    assert ac.price_return_corr(["MU", "WDC"], window=63) == {}


def test_price_return_corr_real_semis_are_correlated():
    # integration: MU and WDC (memory complex) should be positively correlated
    # over the last ~63d; XLF (financials) should be far less correlated to MU.
    m = ac.price_return_corr(["MU", "WDC", "XLF"], window=63)
    if not m:                                  # data unavailable in this env -> skip
        import pytest; pytest.skip("prices.parquet slice unavailable")
    assert m["MU"]["WDC"] > m["MU"]["XLF"]
    assert -1.0 <= m["MU"]["WDC"] <= 1.0
