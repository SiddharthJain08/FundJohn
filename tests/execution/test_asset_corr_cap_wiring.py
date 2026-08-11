import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
import pytest

from execution import regime_blended_sizer as rbs
from execution import asset_correlation as ac


@pytest.fixture(autouse=True)
def _env_only_corr_cfg(monkeypatch):
    """pipeline_config wins over the env these tests set, and on this box the
    REAL DB has asset_corr_cap_enabled=1 (operator slider, 2026-06-26) — so
    every fixture here was silently judged by production state (failing since
    then whenever run against the prod DB). Standing rule: stub prod-state
    gates in fixtures. This resolves env → default only."""
    def env_only(default_thr=rbs._ASSET_CORR_THR_DEFAULT,
                 default_cap_pct=rbs._ASSET_CORR_CAP_PCT_DEFAULT):
        enabled = os.environ.get('OPENCLAW_ASSET_CORR_CAP') == '1'
        try:
            thr = float(os.environ.get('OPENCLAW_ASSET_CORR_THR', default_thr))
        except (TypeError, ValueError):
            thr = default_thr
        try:
            cap_pct = float(os.environ.get('OPENCLAW_ASSET_CORR_CAP_PCT',
                                           default_cap_pct))
        except (TypeError, ValueError):
            cap_pct = default_cap_pct
        return enabled, thr, cap_pct
    monkeypatch.setattr(rbs, '_load_asset_corr_cfg', env_only)


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


def test_apply_cluster_cap_scales_with_lambda(monkeypatch):
    """Operator directive 2026-08-11: cluster cap = cap_pct·λ·NAV. Same book
    as above at λ=2 → cap $44: MU keeps its full $40, WDC trimmed to $4."""
    _fake_corr(monkeypatch)
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP', '1')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_PCT', '0.22')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_THR', '0.70')
    tgt = {'MU': 40.0, 'WDC': 40.0, 'XLF': 20.0}
    out = rbs._apply_asset_corr_cap(dict(tgt), {'MU': 2, 'WDC': 1, 'XLF': 3},
                                    nav=100.0, lam=2.0)
    assert out['MU'] == 40.0                       # fits inside the λ-scaled cap
    assert abs(out['WDC'] - 4.0) < 1e-6            # boundary-trimmed to fill $44
    assert out['XLF'] == 20.0
