"""Intraday session simulator for exit_on == 'session_close' signals (TR04/TR06).

Pure-function tests (a-e) on synthetic 30-minute frames, plus wiring tests
(f-h) through run_backtest with mocked IO. No parquet is read.

  a. long, target on the 11:00 bar -> 'target', fill = level less slippage
  b. short, double-touch -> 'stop' under the default priority
  c. no bracket -> 'session_close' at the 15:30 close
  d. half day -> 'session_close' at the LAST bar present (12:30 bar, 13:00 close)
  e. missing entry bar -> None
  f. OPENCLAW_BT_INTRADAY_EXIT=0 -> legacy daily simulate_trade is called
  g. signal WITHOUT exit_on -> byte-identical to the legacy path
  h. opted-in signal with no 30m bars -> NO trade, intraday_no_bars counted
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from tests.backtest.test_backtest_fill_model import (  # noqa: E402
    _bars_from_rows, _make_mock_conn, _trivial_dataset)

SESSION = '2024-01-16'


def _frame(session=SESSION, last_start='15:30', ticker='SPY', overrides=None, drop=()):
    """One ticker, one session, 30-minute RTH bars 09:30..last_start ET.
    Flat 100 bars (H 100.2 / L 99.8) unless overridden:
    overrides = {'HH:MM': (open, high, low, close)}."""
    overrides = overrides or {}
    starts = pd.date_range(f'{session} 09:30', f'{session} {last_start}', freq='30min',
                           tz='America/New_York')
    rows = []
    for ts in starts:
        hhmm = ts.strftime('%H:%M')
        if hhmm in drop:
            continue
        o, h, l, c = overrides.get(hhmm, (100.0, 100.2, 99.8, 100.0))
        rows.append({'date': session, 'datetime': ts.tz_convert('UTC'), 'ticker': ticker,
                     'open': o, 'high': h, 'low': l, 'close': c, 'volume': 1000.0,
                     'vwap': c, 'transactions': 10})
    return pd.DataFrame(rows)


def _session(df, ticker='SPY', session=SESSION):
    return ub.intraday_session_bars(df, ticker, session)


def _sim(df, **kw):
    kw.setdefault('direction', 1)
    kw.setdefault('entry_bar_et', '10:00')
    kw.setdefault('stop_loss', 99.0)
    kw.setdefault('target_1', 101.0)
    kw.setdefault('slippage_bps', 0.0)
    kw.setdefault('dt_priority', 'stop')
    return ub.simulate_intraday_trade(_session(df), **kw)


@pytest.fixture(autouse=True)
def _env():
    keys = ('OPENCLAW_BT_GAP_FILL', 'OPENCLAW_BT_INTRADAY_EXIT', 'OPENCLAW_BT_DOUBLE_TOUCH')
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


# ---------------------------------------------------------------- pure fn

def test_a_long_target_on_1100_bar():
    df = _frame(overrides={'11:00': (100.1, 101.5, 100.0, 101.0)})
    r = _sim(df, slippage_bps=10.0)
    s = 10.0 / 1e4
    assert r['exit_reason'] == 'target'
    assert r['exit_price'] == pytest.approx(101.0 * (1 - s))
    entry_fill = 100.0 * (1 + s)            # 10:00 bar open = 100.0
    assert r['pnl_pct'] == pytest.approx((101.0 * (1 - s) - entry_fill) / entry_fill)
    assert r['holding_days'] == 1
    assert r['exit_date'] == pd.Timestamp(SESSION)
    assert r['daily_marks'] == [(r['exit_date'], r['pnl_pct'])]
    assert len(r['daily_marks']) == r['holding_days']


def test_b_short_double_touch_is_stop_by_default():
    df = _frame(overrides={'11:00': (100.0, 101.5, 98.5, 100.0)})
    r = _sim(df, direction=-1, stop_loss=101.0, target_1=99.0)
    assert r['exit_reason'] == 'stop'
    assert r['exit_price'] == pytest.approx(101.0)
    assert r['pnl_pct'] == pytest.approx(-0.01)
    r_t = _sim(df, direction=-1, stop_loss=101.0, target_1=99.0, dt_priority='target')
    assert r_t['exit_reason'] == 'target'


def test_c_no_bracket_hit_exits_at_1530_close():
    df = _frame(overrides={'15:30': (100.0, 100.3, 99.9, 100.25)})
    r = _sim(df)
    assert r['exit_reason'] == 'session_close'
    assert r['exit_price'] == pytest.approx(100.25)
    assert r['pnl_pct'] == pytest.approx(0.0025)
    assert r['holding_days'] == 1


def test_d_half_day_exits_at_last_bar_present():
    df = _frame(last_start='12:30', overrides={'12:30': (100.0, 100.3, 99.9, 100.4)})
    r = _sim(df)
    assert r['exit_reason'] == 'session_close'
    assert r['exit_price'] == pytest.approx(100.4)      # the 12:30 bar (closes 13:00)


def test_e_missing_entry_bar_or_thin_session_returns_none():
    assert _sim(_frame(drop=('10:00',))) is None
    assert ub.simulate_intraday_trade(None, direction=1, entry_bar_et='10:00',
                                      stop_loss=99, target_1=101, slippage_bps=0,
                                      dt_priority='stop') is None
    one_bar = _frame(last_start='09:30')
    assert ub.simulate_intraday_trade(_session(one_bar), direction=1, entry_bar_et='09:30',
                                      stop_loss=99, target_1=101, slippage_bps=0,
                                      dt_priority='stop') is None


def test_entry_bar_own_range_counts_and_prior_bars_do_not():
    # a spike on the 09:30 information bar must NOT trigger a 10:00 entry's bracket
    df = _frame(overrides={'09:30': (100.0, 105.0, 95.0, 100.0)})
    assert _sim(df)['exit_reason'] == 'session_close'
    # the entry bar's own low does count (as with fill_model='open')
    df2 = _frame(overrides={'10:00': (100.0, 100.2, 98.5, 99.5)})
    assert _sim(df2)['exit_reason'] == 'stop'


def test_session_bars_helper_naive_utc_and_wrong_session():
    df = _frame()
    naive = df.copy()
    naive['datetime'] = naive['datetime'].dt.tz_localize(None)
    assert len(_session(naive)) == 13
    assert ub.intraday_session_bars(df, 'SPY', '2024-01-17') is None
    assert ub.intraday_session_bars(df, 'QQQ', SESSION) is None


# ---------------------------------------------------------------- wiring

def _stub_cls(signal_params, ticker='AAA'):
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_fm'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or not universe:
                return []
            ep = float(prices[universe[0]].iloc[-1])
            return [Signal(ticker=universe[0], direction='LONG', entry_price=ep,
                           stop_loss=ep * 0.99, target_1=ep * 1.01,
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED', signal_params=dict(signal_params))]
    return Stub


def _run(strategy_cls, aux_30m, env=None):
    """run_backtest with mocked IO; returns (trades, sim_result)."""
    close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
    cap = {'trades': None, 'sim': None}
    real_agg = ub.aggregate_metrics
    real_sim = ub._per_bar_simulate

    def agg(trades):
        if cap['trades'] is None:
            cap['trades'] = list(trades)
        return real_agg(trades)

    def sim_wrap(*a, **k):
        cap['sim'] = real_sim(*a, **k)
        return cap['sim']

    aux = {'options': {}}
    if aux_30m is not None:
        aux['prices_30m'] = aux_30m
    e = {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0',
         'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}
    e.update(env or {})
    with (
        patch.dict(os.environ, e),
        patch('backtest.unified_backtest.load_prices_panels', return_value=(close_wide, bars)),
        patch('backtest.unified_backtest.load_regimes', return_value=regimes),
        patch('backtest.unified_backtest.load_strategy_class', return_value=strategy_cls),
        patch('backtest.unified_backtest.find_strategy_file',
              return_value=str(ROOT / 'src/strategies/implementations/momentum_12_1.py')),
        patch('backtest.unified_backtest._code_sha', return_value='abc123'),
        patch('backtest.unified_backtest.aggregate_metrics', side_effect=agg),
        patch('backtest.unified_backtest.psycopg2.extras.execute_values'),
        patch('backtest.unified_backtest._per_bar_simulate', side_effect=sim_wrap),
        patch('strategies.aux_data_loader.load_aux_data', return_value=aux),
    ):
        ub.run_backtest('stub_fm', conn=_make_mock_conn(), commit=False)
    return cap['trades'], cap['sim']


def _aux_frame_for_dataset():
    _, _, _, dates, _, _ = _trivial_dataset()
    frames = [_frame(session=d.strftime('%Y-%m-%d'), ticker='AAA',
                     overrides={'11:00': (100.0, 103.0, 99.9, 102.0)}) for d in dates]
    return pd.concat(frames, ignore_index=True)


OPT_IN = {'exit_on': 'session_close', 'entry_bar_et': '10:00'}


def test_wiring_opted_in_signal_uses_intraday_path():
    trades, sim = _run(_stub_cls(OPT_IN), _aux_frame_for_dataset())
    assert trades and sim['intraday_trades'] == len(trades) > 0
    for t in trades:
        assert t['holding_days'] == 1 and t['entry_date'] == t['exit_date']
        assert t['exit_reason'] == 'target' and len(t['daily_marks']) == 1
        assert t['entry_price'] == pytest.approx(100.0)       # 10:00 bar open, not the daily close
        assert t['signal_target'] == pytest.approx(101.0)     # 1% bracket re-anchored to 100.0


def test_f_flag_off_takes_legacy_daily_path():
    real = ub.simulate_trade
    with patch('backtest.unified_backtest.simulate_trade', side_effect=real) as m:
        trades, sim = _run(_stub_cls(OPT_IN), _aux_frame_for_dataset(),
                           env={'OPENCLAW_BT_INTRADAY_EXIT': '0'})
    assert m.called
    assert sim['intraday_trades'] == 0
    assert all(t['exit_reason'] != 'session_close' or t['holding_days'] >= 1 for t in trades)


def test_g_signal_without_exit_on_is_untouched():
    plain, _ = _run(_stub_cls({}), None)
    flagged_off, _ = _run(_stub_cls({}), _aux_frame_for_dataset())      # 30m data present, no opt-in
    legacy_env, _ = _run(_stub_cls(OPT_IN), None, env={'OPENCLAW_BT_INTRADAY_EXIT': '0'})
    assert plain and plain == flagged_off == legacy_env


def test_h_no_30m_bars_means_no_trade_and_is_counted():
    trades, sim = _run(_stub_cls(OPT_IN), None)
    assert not trades
    assert sim['intraday_no_bars'] > 0 and sim['intraday_trades'] == 0
    # a session frame for a DIFFERENT ticker is equally "no bars"
    other = _aux_frame_for_dataset().assign(ticker='ZZZ')
    trades2, sim2 = _run(_stub_cls(OPT_IN), other)
    assert not trades2 and sim2['intraday_no_bars'] > 0


def test_missing_entry_bar_et_is_skipped_and_counted():
    trades, sim = _run(_stub_cls({'exit_on': 'session_close'}), _aux_frame_for_dataset())
    assert not trades
    assert sim['intraday_missing_entry_bar'] > 0 and sim['intraday_no_bars'] == 0


def test_exit_hook_strategy_opting_in_raises():
    cls = _stub_cls(OPT_IN)
    cls.exit_hook = True
    cls.should_exit = lambda self, *a, **k: None
    with pytest.raises(ValueError, match='session_close'):
        _run(cls, _aux_frame_for_dataset())
