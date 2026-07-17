import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
from execution import asset_correlation_filter as acf


def _corr(pairs, tickers):
    m = {a: {b: (1.0 if a == b else 0.0) for b in tickers} for a in tickers}
    for (a, b), v in pairs.items():
        m[a][b] = m[b][a] = v
    return m


def test_clusters_group_same_direction_correlated():
    tickers = ['MU', 'WDC', 'XLF']
    sign = {'MU': 1, 'WDC': 1, 'XLF': 1}
    corr = _corr({('MU', 'WDC'): 0.9, ('MU', 'XLF'): 0.1, ('WDC', 'XLF'): 0.1}, tickers)
    clusters = acf._cluster_same_direction(tickers, sign, corr, 0.70)
    sets = sorted([sorted(c) for c in clusters])
    assert ['MU', 'WDC'] in sets and ['XLF'] in sets


def test_opposite_direction_never_coclustered():
    # MU long + WDC short, highly correlated -> a hedge -> separate clusters
    tickers = ['MU', 'WDC']
    sign = {'MU': 1, 'WDC': -1}
    corr = _corr({('MU', 'WDC'): 0.95}, tickers)
    clusters = acf._cluster_same_direction(tickers, sign, corr, 0.70)
    assert sorted([sorted(c) for c in clusters]) == [['MU'], ['WDC']]


def test_cap_keeps_top_conviction_trims_boundary_releases_rest():
    # Three correlated longs, each $40 target, NAV=$100, cap=22% -> cluster cap $22.
    # Conviction A>B>C. Keep A ($40? no - cap is $22) -> A trimmed to $22, B and C released.
    tgt = {'A': 40.0, 'B': 40.0, 'C': 40.0}
    conv = {'A': 3.0, 'B': 2.0, 'C': 1.0}
    corr = _corr({('A', 'B'): 0.9, ('A', 'C'): 0.9, ('B', 'C'): 0.9}, list(tgt))
    out, audit = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert abs(out['A'] - 22.0) < 1e-6          # top conviction trimmed to fill cap
    assert out['B'] == 0.0 and out['C'] == 0.0  # released, not redistributed
    assert audit['total_gross_after'] <= audit['total_gross_before']  # INV-1
    assert abs(audit['released_usd'] - 98.0) < 1e-6  # 120 -> 22


def test_cap_keeps_multiple_until_cap():
    # Two correlated longs $10 each, NAV 100, cap 22% = $22 -> both fit ($20 < $22).
    tgt = {'A': 10.0, 'B': 10.0}
    conv = {'A': 2.0, 'B': 1.0}
    corr = _corr({('A', 'B'): 0.9}, list(tgt))
    out, _ = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert out['A'] == 10.0 and out['B'] == 10.0  # under cap -> untouched


def test_uncorrelated_untouched():
    tgt = {'A': 40.0, 'B': 40.0}
    conv = {'A': 2.0, 'B': 1.0}
    corr = _corr({('A', 'B'): 0.1}, list(tgt))   # not correlated -> separate singletons
    out, _ = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert out == tgt                              # singletons, no single_name cap -> unchanged


def test_failopen_empty_corr_is_noop():
    tgt = {'A': 40.0, 'B': 40.0}
    out, _ = acf.cap_correlated_clusters(tgt, {}, {}, nav=100.0, cap_pct=0.22)
    assert out == tgt                              # INV-5


def test_gross_never_increases_and_no_redistribution():
    tgt = {'A': 50.0, 'B': 30.0, 'C': 30.0}
    conv = {'A': 3.0, 'B': 2.0, 'C': 1.0}
    corr = _corr({('A', 'B'): 0.9, ('A', 'C'): 0.9, ('B', 'C'): 0.9}, list(tgt))
    out, _ = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert sum(abs(v) for v in out.values()) <= sum(abs(v) for v in tgt.values())  # INV-1
    for t in tgt:
        assert abs(out[t]) <= abs(tgt[t]) + 1e-9   # INV-2 no survivor grows
