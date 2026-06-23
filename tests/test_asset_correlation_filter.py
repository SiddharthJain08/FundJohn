import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
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
