"""aux_data_loader prices_30m slice — backtest/live parity (2026-10-15).

Live (engine.py) hands the raw prices_30m frame to CohortBaseStrategy, which
turns it into spy_30m_bars / intraday_30m_bars. The backtest loader used to
return no 'prices_30m' key, so S_TR04 / S_TR06 were 0-trade runs. The slice
must be point-in-time (no look-ahead), trailing-windowed, keep the stored
columns/dtypes, and the key must be absent when the slice is empty.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import aux_data_loader as adl  # noqa: E402
from src.strategies.cohort_base import CohortBaseStrategy  # noqa: E402

SESSIONS = ['2026-01-15', '2026-03-02', '2026-03-03', '2026-03-04',
            '2026-03-05', '2026-03-06']
TICKERS = ['SPY', 'AAPL', 'MSFT']
BAR_TIMES = ['14:30', '15:00', '15:30']  # UTC


def _synthetic() -> pd.DataFrame:
    rows = []
    for d in SESSIONS:
        for tk in TICKERS:
            for i, t in enumerate(BAR_TIMES):
                px = 100.0 + i
                rows.append({
                    'date': d,
                    'datetime': pd.Timestamp(f'{d} {t}', tz='UTC'),
                    'ticker': tk,
                    'open': px, 'high': px + 1, 'low': px - 1, 'close': px + 0.5,
                    'volume': 1000.0 + i, 'vwap': px + 0.25,
                    'transactions': 10 + i,
                })
    df = pd.DataFrame(rows)
    df['datetime'] = df['datetime'].astype('datetime64[us, UTC]')
    df['transactions'] = df['transactions'].astype('int64')
    return df


@pytest.fixture()
def p30(monkeypatch, tmp_path):
    path = tmp_path / 'prices_30m.parquet'
    _synthetic().to_parquet(path, index=False)
    monkeypatch.setattr(adl, 'PRICES_30M_PATH', path)
    monkeypatch.setattr(adl, '_P30_DF', None)
    yield path
    monkeypatch.setattr(adl, '_P30_DF', None)


@pytest.fixture()
def only_p30(monkeypatch):
    """Stub every other slice so load_aux_data never reads real master parquets."""
    for name in ('_day_slice', '_vol_indices_slice', '_macro_slice',
                 '_financials_slice', '_insider_slice', '_insider_long_slice',
                 '_sentiment_day_slice'):
        monkeypatch.setattr(adl, name, lambda d: {})


def test_no_lookahead(p30):
    sl = adl._prices_30m_slice('2026-03-04')
    assert not sl.empty
    assert sl['date'].max() == '2026-03-04'
    assert (sl['date'] <= '2026-03-04').all()
    assert (sl['datetime'] <= pd.Timestamp('2026-03-04 23:59', tz='UTC')).all()


def test_trailing_window(p30):
    # 2026-01-15 is 50 days before 03-06 -> excluded; 03-02 is 4 days -> kept.
    sl = adl._prices_30m_slice('2026-03-06')
    assert '2026-01-15' not in set(sl['date'])
    assert set(sl['date']) == {'2026-03-02', '2026-03-03', '2026-03-04',
                               '2026-03-05', '2026-03-06'}
    # Window boundary: lookback is inclusive on the low edge (D - 30 days).
    edge = pd.Timestamp('2026-01-15') + pd.Timedelta(days=adl.PRICES_30M_LOOKBACK_DAYS)
    assert '2026-01-15' in set(adl._prices_30m_slice(edge.strftime('%Y-%m-%d'))['date'])
    day_after = edge + pd.Timedelta(days=1)
    # Nothing else lies in that window, so the slice is empty (no look-back leak).
    assert adl._prices_30m_slice(day_after.strftime('%Y-%m-%d')).empty


def test_key_absent_before_first_session_present_after(p30, only_p30):
    assert 'prices_30m' not in adl.load_aux_data('2026-01-01')
    out = adl.load_aux_data('2026-03-03')
    assert 'prices_30m' in out
    assert not out['prices_30m'].empty
    # A date past every session with nothing inside the window -> absent again.
    assert 'prices_30m' not in adl.load_aux_data('2026-06-30')


def test_stored_columns_and_dtypes_preserved(p30):
    sl = adl._prices_30m_slice('2026-03-06')
    stored = pd.read_parquet(p30)
    assert list(sl.columns) == list(stored.columns)
    assert '_date' not in sl.columns
    assert str(sl['datetime'].dtype) == 'datetime64[us, UTC]'
    assert str(sl['transactions'].dtype) == 'int64'
    assert sl['date'].dtype == stored['date'].dtype  # string stays string
    for c in ('open', 'high', 'low', 'close', 'volume', 'vwap', 'ticker'):
        assert c in sl.columns
    # Returned a copy: mutating it must not corrupt the module cache.
    sl.loc[:, 'close'] = -1.0
    assert (adl._prices_30m_slice('2026-03-06')['close'] != -1.0).all()


def test_missing_file_is_empty_and_key_absent(monkeypatch, tmp_path, only_p30):
    monkeypatch.setattr(adl, 'PRICES_30M_PATH', tmp_path / 'nope.parquet')
    monkeypatch.setattr(adl, '_P30_DF', None)
    assert adl._load_prices_30m().empty
    assert adl._prices_30m_slice('2026-03-03').empty
    assert 'prices_30m' not in adl.load_aux_data('2026-03-03')
    monkeypatch.setattr(adl, '_P30_DF', None)


def test_cohort_base_translates_slice_to_spy_bars(p30, only_p30):
    """Parity: the backtest slice feeds the same translation live uses."""

    class _Probe(CohortBaseStrategy):
        regime_filter = ['LOW_VOL']

        def _generate_signals_cohort(self, market_data, opts_map):
            return []

    aux = adl.load_aux_data('2026-03-06')
    strat = _Probe.__new__(_Probe)
    market_data, _ = strat._build_cohort_dicts(
        pd.DataFrame(), {'state': 'LOW_VOL'}, ['SPY'], aux)

    bars = market_data.get('spy_30m_bars')
    assert bars, 'spy_30m_bars missing — slice did not reach cohort_base'
    assert all(b['ticker'] == 'SPY' for b in bars)
    # 5 in-window sessions x 3 bars (2026-01-15 excluded by the lookback).
    assert len(bars) == 15
    # cohort_base sorts by datetime ascending.
    dts = [b['datetime'] for b in bars]
    assert dts == sorted(dts)
    # intraday_30m_bars = the latest session only, per ticker.
    today = market_data['intraday_30m_bars']
    assert set(today) == set(TICKERS)
    assert len(today['SPY']) == 3
    assert {b['date'] for b in today['SPY']} == {'2026-03-06'}
