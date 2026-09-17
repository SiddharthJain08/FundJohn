"""C3 backtest mirror: with OPENCLAW_BT_EVENT_GATE=1 the per-bar loop takes no
ENTRY on a T-1..T macro-event session. Exits are untouched.

Self-contained: synthetic bars, a synthetic trading calendar and a synthetic
macro_events master in tmp_path. aux_data is stubbed so nothing reaches the DB
or data/.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub   # noqa: E402
from lib import macro_events as me            # noqa: E402
from lib import trading_calendar as tc        # noqa: E402

DATES = pd.date_range('2026-09-01', periods=20, freq='B')


@pytest.fixture(autouse=True)
def _no_aux(monkeypatch):
    """load_aux_data is imported INSIDE _per_bar_simulate, so patching the
    source module is what takes effect."""
    import strategies.aux_data_loader as adl
    monkeypatch.setattr(adl, 'load_aux_data',
                        lambda *a, **k: {'options': {}}, raising=False)


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in DATES]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    yield
    tc.clear_cache()


def _events(tmp_path, monkeypatch, sessions):
    rows = [{'event': 'CPI',
             'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
             'session_date': d, 'source': 'test',
             'ingested_at': pd.Timestamp('2026-09-01T00:00:00Z')}
            for d in sessions]
    p = tmp_path / 'macro_events.parquet'
    pd.DataFrame(rows, columns=me.COLUMNS).to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))


def _dataset():
    closes = [100.0 + i for i in range(len(DATES))]
    close_wide = pd.DataFrame({'AAA': closes}, index=DATES)
    close_wide.index.name = 'date'
    bars = {'AAA': pd.DataFrame(
        {'open': [c - 0.5 for c in closes], 'high': [c + 0.2 for c in closes],
         'low': [c - 0.2 for c in closes], 'close': closes},
        index=pd.DatetimeIndex(DATES, name='date'))}
    regimes = pd.Series(['LOW_VOL'] * len(DATES), index=DATES)
    return close_wide, bars, regimes


def _instance():
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_event_gate'
        min_lookback = 1
        MAX_SIGNALS = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            close = float(prices['AAA'].iloc[-1])
            return [Signal(ticker='AAA', direction='LONG', entry_price=close,
                           stop_loss=close * 0.5, target_1=close * 1.5,
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED')]

    return Stub()


def _sim():
    close_wide, bars, regimes = _dataset()
    return ub._per_bar_simulate(_instance(), close_wide, bars, regimes,
                                DATES[0], DATES[-1], strategy_id='stub_event_gate',
                                fill_model='same_close')


def test_flag_unset_is_byte_identical(tmp_path, monkeypatch, calendar):
    _events(tmp_path, monkeypatch, [DATES[10].date()])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    out = _sim()
    assert out['entries_event_gated'] == 0
    assert len(out['trades']) > 0


def test_gate_skips_entries_on_t_minus_one_and_t(tmp_path, monkeypatch, calendar):
    gated = DATES[10].date()
    _events(tmp_path, monkeypatch, [gated])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    base = _sim()
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    out = _sim()
    assert out['entries_event_gated'] == 2          # T-1 and T, one signal each
    assert len(out['trades']) == len(base['trades']) - 2
    entered = {t['entry_date'] for t in out['trades']}
    assert gated not in entered
    assert DATES[9].date() not in entered


def test_a_calendar_with_no_events_gates_nothing(tmp_path, monkeypatch, calendar):
    _events(tmp_path, monkeypatch, [])
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_missing_master_is_inert(tmp_path, monkeypatch, calendar):
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(tmp_path / 'nope.parquet'))
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_the_live_flag_alone_does_not_arm_the_backtest(tmp_path, monkeypatch, calendar):
    """Spec §0: never stack epochs. Arming C3 live must not silently change
    every fleet run."""
    _events(tmp_path, monkeypatch, [DATES[10].date()])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_low_importance_event_does_not_gate(tmp_path, monkeypatch, calendar):
    """gated_sessions() defaults to HIGH_IMPORTANCE = (FOMC_DECISION, CPI, NFP)
    per ruling R3; a PCE release (not in that set) must not gate entries."""
    d = DATES[10].date()
    rows = [{'event': 'PCE',
             'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
             'session_date': d, 'source': 'test',
             'ingested_at': pd.Timestamp('2026-09-01T00:00:00Z')}]
    p = tmp_path / 'macro_events.parquet'
    pd.DataFrame(rows, columns=me.COLUMNS).to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_config_json_literal_carries_the_event_gate_keys():
    src = (ROOT / 'src' / 'backtest' / 'unified_backtest.py').read_text()
    assert "'event_gate':" in src
    assert "'entries_event_gated':" in src
