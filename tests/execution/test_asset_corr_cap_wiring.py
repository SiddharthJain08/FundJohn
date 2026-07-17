import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
from execution import regime_blended_sizer as rbs
from execution import asset_correlation as ac


def _fake_corr(monkeypatch):
    # MU/WDC correlated longs; XLF separate. Deterministic, no parquet read.
    def fake(tickers, window=63, as_of=None):
        m = {a: {b: (1.0 if a == b else 0.0) for b in tickers} for a in tickers}
        if 'MU' in tickers and 'WDC' in tickers:
            m['MU']['WDC'] = m['WDC']['MU'] = 0.9
        return m
    monkeypatch.setattr(ac, 'price_return_corr', fake)


def test_gate_off_is_identity(monkeypatch):
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP', raising=False)
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP_SHADOW', raising=False)
    tgt = {'MU': 40.0, 'WDC': 40.0, 'XLF': 20.0}
    out = rbs._apply_asset_corr_cap(dict(tgt), {'MU': 2, 'WDC': 1, 'XLF': 3}, nav=100.0)
    assert out == tgt


def test_shadow_logs_but_does_not_change(monkeypatch):
    _fake_corr(monkeypatch)
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP', raising=False)
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_SHADOW', '1')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_PCT', '0.22')
    tgt = {'MU': 40.0, 'WDC': 40.0, 'XLF': 20.0}
    out = rbs._apply_asset_corr_cap(dict(tgt), {'MU': 2, 'WDC': 1, 'XLF': 3}, nav=100.0)
    assert out == tgt                              # shadow never changes targets


def test_apply_caps_correlated_cluster(monkeypatch):
    _fake_corr(monkeypatch)
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP', '1')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_PCT', '0.22')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_THR', '0.70')
    tgt = {'MU': 40.0, 'WDC': 40.0, 'XLF': 20.0}   # MU+WDC cluster $80 -> cap $22
    out = rbs._apply_asset_corr_cap(dict(tgt), {'MU': 2, 'WDC': 1, 'XLF': 3}, nav=100.0)
    assert abs(out['MU'] - 22.0) < 1e-6            # top conviction trimmed to cap
    assert out['WDC'] == 0.0                       # released
    assert out['XLF'] == 20.0                      # uncorrelated, untouched
    assert sum(abs(v) for v in out.values()) < sum(abs(v) for v in tgt.values())
