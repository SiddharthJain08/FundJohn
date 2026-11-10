"""load_regimes(instrument_class): crypto backtests partition on the crypto
tier table (24/7 calendar, weekends present); equity is unchanged."""
import pandas as pd

from backtest import unified_backtest as ub
from strategies import crypto_historical_regimes as chr_mod


def _write_equity(tmp_path):
    p = tmp_path / 'historical_regimes.parquet'
    d = pd.bdate_range('2026-01-05', periods=10)
    pd.DataFrame({'date': d.date, 'vix': 15.0, 'vix_smoothed': 15.0,
                  'regime': ['LOW_VOL'] * 5 + ['HIGH_VOL'] * 5}).to_parquet(p, index=False)
    return p


def _write_crypto(tmp_path):
    p = tmp_path / 'crypto_historical_regimes.parquet'
    d = pd.date_range('2026-01-03', periods=14, freq='D')   # starts on a Saturday
    pd.DataFrame({'date': d.date, 'close': 1.0, 'rv30': 0.5, 'rv30_smoothed': 0.5,
                  'regime': ['CRISIS'] * 7 + ['LOW_VOL'] * 7}).to_parquet(p, index=False)
    return p


def test_equity_default_unchanged(tmp_path, monkeypatch):
    p = _write_equity(tmp_path)
    monkeypatch.setattr(ub, 'REGIMES_PARQUET', p)
    a = ub.load_regimes()
    b = ub.load_regimes('equity')
    r = pd.read_parquet(p)
    r['date'] = pd.to_datetime(r['date'])
    expect = r.sort_values('date').set_index('date')['regime'].astype(str)
    pd.testing.assert_series_equal(a, expect)
    pd.testing.assert_series_equal(b, expect)
    assert not any(ts.weekday() >= 5 for ts in a.index)


def test_crypto_reads_crypto_table_with_weekends(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, 'REGIMES_PARQUET', _write_equity(tmp_path))
    monkeypatch.setattr(chr_mod, 'CACHE', _write_crypto(tmp_path))
    monkeypatch.setattr(chr_mod, '_cache', None)
    s = ub.load_regimes('crypto')
    assert isinstance(s.index, pd.DatetimeIndex) and s.index.name == 'date'
    assert s.name == 'regime' and s.index.is_monotonic_increasing
    assert len(s) == 14
    assert pd.Timestamp('2026-01-03') in s.index          # Saturday
    assert pd.Timestamp('2026-01-04') in s.index          # Sunday
    assert s.loc[pd.Timestamp('2026-01-03')] == 'CRISIS'
    assert s.loc[pd.Timestamp('2026-01-16')] == 'LOW_VOL'
    # equity still reads its own file
    assert set(ub.load_regimes('equity').unique()) == {'LOW_VOL', 'HIGH_VOL'}


def test_crypto_series_is_dense_over_gap_days(tmp_path, monkeypatch):
    """M1: a BTC gap day inside the span carries the previous label."""
    p = tmp_path / 'crypto_historical_regimes.parquet'
    d = pd.to_datetime(['2026-06-04', '2026-06-05', '2026-06-09', '2026-06-10'])
    pd.DataFrame({'date': d.date, 'close': 1.0, 'rv30': 0.5, 'rv30_smoothed': 0.5,
                  'regime': ['LOW_VOL', 'CRISIS', 'HIGH_VOL', 'HIGH_VOL']}).to_parquet(p, index=False)
    monkeypatch.setattr(chr_mod, 'CACHE', p)
    monkeypatch.setattr(chr_mod, '_cache', None)
    s = ub.load_regimes('crypto')
    assert len(s) == 7
    assert list(s.index) == list(pd.date_range('2026-06-04', '2026-06-10', freq='D'))
    for gap in ('2026-06-06', '2026-06-07', '2026-06-08'):
        assert s.loc[pd.Timestamp(gap)] == 'CRISIS'
    assert s.loc[pd.Timestamp('2026-06-09')] == 'HIGH_VOL'
