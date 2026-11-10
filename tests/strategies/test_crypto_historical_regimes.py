"""Crypto regime tier table (Phase 3 Task 1, fixes D-2).

Every test redirects CACHE to tmp_path and resets the module-level memo:
the real CACHE resolves through data/master (production) and must never be
written from a test."""
import numpy as np
import pandas as pd
import pytest

from strategies import crypto_historical_regimes as mod

TIERS = ((0.30, 'LOW_VOL'), (0.60, 'TRANSITIONING'), (0.90, 'HIGH_VOL'))


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'CACHE', tmp_path / 'crypto_historical_regimes.parquet')
    monkeypatch.setattr(mod, '_cache', None)

    def _no_prices(*a, **k):
        raise AssertionError('test must not read the real prices master')
    monkeypatch.setattr(mod, 'load_btc_closes', _no_prices)


def _two_segment_closes(n_low=200, n_high=200, vol_low=0.20, vol_high=1.50, seed=7):
    rng = np.random.default_rng(seed)
    d_low = vol_low / np.sqrt(365)
    d_high = vol_high / np.sqrt(365)
    rets = np.concatenate([rng.normal(0, d_low, n_low), rng.normal(0, d_high, n_high)])
    closes = 30_000 * np.exp(np.cumsum(rets))
    idx = pd.date_range('2024-01-01', periods=len(closes), freq='D')
    return pd.Series(closes, index=idx, name='close')


def test_classify_history_unknown_warmup_then_canonical():
    closes = _two_segment_closes()
    df = mod.classify_history(closes=closes, tiers=TIERS)
    assert list(df.columns) == ['date', 'close', 'rv30', 'rv30_smoothed', 'regime']
    assert len(df) == len(closes)
    assert (df['regime'].iloc[:29] == 'UNKNOWN').all()
    assert df['regime'].iloc[29:].isin(mod.CANONICAL_REGIMES).all()


def test_higher_vol_segment_maps_to_higher_tiers():
    closes = _two_segment_closes()
    df = mod.classify_history(closes=closes, tiers=TIERS)
    rank = {r: i for i, r in enumerate(mod.CANONICAL_REGIMES)}
    low = df.iloc[60:200]['regime'].map(rank)
    high = df.iloc[260:]['regime'].map(rank)
    assert low.mean() < high.mean()
    assert (low == rank['LOW_VOL']).mean() > 0.9
    assert (high == rank['CRISIS']).mean() > 0.9


def test_classify_history_date_window_uses_full_history_for_warmup():
    closes = _two_segment_closes()
    df = mod.classify_history('2024-03-01', '2024-04-01', closes=closes, tiers=TIERS)
    assert df['date'].min() == pd.Timestamp('2024-03-01').date()
    assert df['date'].max() == pd.Timestamp('2024-04-01').date()
    assert (df['regime'] != 'UNKNOWN').all()


def test_tier_boundaries_are_inclusive_upper():
    assert mod._classify_rv(0.30, TIERS) == 'LOW_VOL'
    assert mod._classify_rv(0.3000001, TIERS) == 'TRANSITIONING'
    assert mod._classify_rv(0.90, TIERS) == 'HIGH_VOL'
    assert mod._classify_rv(0.95, TIERS) == 'CRISIS'
    assert mod._classify_rv(float('nan'), TIERS) == 'UNKNOWN'


def test_calibrate_tiers_matches_target_within_2pp():
    rng = np.random.default_rng(3)
    idx = pd.date_range('2026-05-26', periods=169, freq='D')
    rv = pd.Series(rng.lognormal(-0.8, 0.4, len(idx)), index=idx)
    target = {'LOW_VOL': 0.414, 'TRANSITIONING': 0.308, 'HIGH_VOL': 0.118, 'CRISIS': 0.160}
    t1, t2, t3 = mod.calibrate_tiers(rv, target)
    assert t1 < t2 < t3
    tiers = ((t1, 'LOW_VOL'), (t2, 'TRANSITIONING'), (t3, 'HIGH_VOL'))
    got = rv.map(lambda v: mod._classify_rv(v, tiers)).value_counts(normalize=True)
    for k, f in target.items():
        assert abs(got.get(k, 0.0) - f) <= 0.02, (k, got.get(k, 0.0), f)


def test_calibrate_tiers_rejects_bad_targets():
    rv = pd.Series(np.linspace(0.1, 1.0, 50))
    with pytest.raises(ValueError):
        mod.calibrate_tiers(rv, {'LOW_VOL': 0.5, 'TRANSITIONING': 0.5})
    with pytest.raises(ValueError):
        mod.calibrate_tiers(rv, {'LOW_VOL': 0.6, 'TRANSITIONING': 0.6,
                                 'HIGH_VOL': 0.1, 'CRISIS': 0.1})


def test_rebuild_cache_idempotent_and_append_only(monkeypatch):
    closes = _two_segment_closes()
    monkeypatch.setattr(mod, 'load_btc_closes', lambda start=None, end=None: closes)
    p1 = mod.rebuild_cache()
    assert p1 == mod.CACHE and p1.exists()
    n1 = len(pd.read_parquet(p1))
    mod.rebuild_cache()
    n2 = len(pd.read_parquet(p1))
    assert n1 == n2 == len(closes)
    # a rebuild over a shorter window never truncates the date axis
    mod.rebuild_cache('2024-06-01')
    assert len(pd.read_parquet(p1)) == n1


def test_regime_series_and_lookup_over_cache(monkeypatch):
    closes = _two_segment_closes()
    monkeypatch.setattr(mod, 'load_btc_closes', lambda start=None, end=None: closes)
    monkeypatch.setattr(mod, 'CRYPTO_RV_TIERS', TIERS)
    mod.rebuild_cache()
    monkeypatch.setattr(mod, '_cache', None)
    s = mod.regime_series(['2024-01-05', '2024-12-01', '2030-01-01'])
    assert s.iloc[0] == 'UNKNOWN'
    assert s.iloc[1] in mod.CANONICAL_REGIMES
    assert s.iloc[2] == 'UNKNOWN'
    assert mod.regime_for_date('2024-12-01') == s.iloc[1]


def test_calibration_script_rewrites_only_the_marked_block():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / 'scripts' / 'calibrate_crypto_regime_tiers.py'
    spec = importlib.util.spec_from_file_location('calib_script', p)
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    src = Path(mod.__file__).read_text()
    block = cs.render_block((0.1, 0.2, 0.3), '2026-05-26', '2026-11-10', '2026-11-10',
                            {'LOW_VOL': .4, 'TRANSITIONING': .3, 'HIGH_VOL': .1, 'CRISIS': .2})
    new = cs.rewrite_module(src, block)
    ns = {}
    start = new.index(cs.BEGIN)
    exec(new[start:new.index(cs.END)], ns)
    assert ns['CRYPTO_RV_TIERS'] == ((0.1, 'LOW_VOL'), (0.2, 'TRANSITIONING'), (0.3, 'HIGH_VOL'))
    assert new[:start] == src[:src.index(cs.BEGIN)]
    assert new.split(cs.END, 1)[1] == src.split(cs.END, 1)[1]
    assert '# calibrated 2026-11-10 against crypto_regime_states 2026-05-26..2026-11-10 (daily mode)' in new
    with pytest.raises(SystemExit):
        cs.rewrite_module(src + '\n' + cs.BEGIN + '\n', block)


def test_master_registered_with_ledger_and_freshness():
    from src.strategies import sync_data_ledger as ledger
    from system_checks.checks import master_freshness as mf
    assert ledger.PARQUET_MAP['crypto_historical_regimes'] == 'crypto_historical_regimes'
    assert ledger.PROVIDERS['crypto_historical_regimes'] == 'computed'
    assert mf._CADENCES['crypto_historical_regimes.parquet'] == ('date', 5)


def test_no_rebuild_when_source_has_not_advanced(monkeypatch):
    closes = _two_segment_closes()
    monkeypatch.setattr(mod, 'load_btc_closes', lambda start=None, end=None: closes)
    mod.rebuild_cache()
    last = closes.index.max().date()
    calls = []
    monkeypatch.setattr(mod, 'rebuild_cache', lambda *a, **k: calls.append(1))
    monkeypatch.setattr(mod, '_source_max_date', lambda: last)
    monkeypatch.setattr(mod, '_cache', None)
    mod.load_table()
    assert calls == []
    monkeypatch.setattr(mod, '_source_max_date', lambda: last + pd.Timedelta(days=1))
    monkeypatch.setattr(mod, '_cache', None)
    mod.load_table()
    assert calls == [1]


def test_calibrate_tiers_tie_aware():
    # heavy ties: 10 copies of 1.0 straddle the 40 % cut
    rv = pd.Series([0.5] * 37 + [1.0] * 10 + [2.0] * 53)
    t1, _, _ = mod.calibrate_tiers(rv, {'LOW_VOL': 0.40, 'TRANSITIONING': 0.10,
                                        'HIGH_VOL': 0.10, 'CRISIS': 0.40})
    assert t1 == 0.5     # sorted[39] == 1.0 would admit 47 %; 0.5 admits 37 % (closer to 40 %)
