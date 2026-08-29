"""Amendment 1 D-B1..B3: S_beta_spy exits on any regime change via the exit hook."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import CANONICAL_REGIMES
from strategies.implementations.S_beta_spy import BetaSpy, HOLD_DAYS
from backtest.open_book import OpenTrade, advance_open_book


def _pos(entry_regime):
    return {'ticker': 'SPY', 'direction': 'LONG', 'entry_price': 500.0,
            'entry_date': pd.Timestamp('2026-01-05'), 'days_held': 3,
            'stop_loss': 300.0, 'target_1': 2500.0,
            'signal_params': {'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': entry_regime}}


def test_flag_and_hold_unchanged():
    assert BetaSpy.exit_hook is True
    assert HOLD_DAYS == 21            # promotion hold-cap parity guard (D-B2)


@pytest.mark.parametrize('entry', CANONICAL_REGIMES)
def test_same_regime_holds_other_regime_exits(entry):
    s = BetaSpy()
    for state in CANONICAL_REGIMES:
        got = s.should_exit(_pos(entry), pd.DataFrame(), {'state': state})
        assert got == (None if state == entry else 'regime_exit')


def test_missing_or_unknown_state_holds():
    s = BetaSpy()
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), {}) is None
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), {'state': 'UNKNOWN'}) is None
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), None) is None
    p = _pos('LOW_VOL'); p['signal_params'] = {}
    assert s.should_exit(p, pd.DataFrame(), {'state': 'CRISIS'}) is None
    p['signal_params'] = None
    assert s.should_exit(p, pd.DataFrame(), {'state': 'CRISIS'}) is None


def test_signal_records_entry_regime():
    dates = pd.date_range('2026-01-05', periods=5, freq='B')
    prices = pd.DataFrame({'SPY': [500.0, 501.0, 502.0, 503.0, 504.0]}, index=dates)
    sig = BetaSpy().generate_signals(prices, {'state': 'HIGH_VOL'}, ['SPY'])
    assert len(sig) == 1 and sig[0].signal_params['regime'] == 'HIGH_VOL'


def test_open_book_closes_on_the_flip_bar():
    dates = pd.date_range('2026-01-05', periods=12, freq='B')
    closes = [500.0 + i for i in range(12)]
    bars = pd.DataFrame({'open': closes, 'high': [c + 0.5 for c in closes],
                         'low': [c - 0.5 for c in closes], 'close': closes},
                        index=pd.DatetimeIndex(dates, name='date'))
    panel = pd.DataFrame({'SPY': closes}, index=dates)
    regimes = ['LOW_VOL'] * 6 + ['HIGH_VOL'] * 6          # flip on dates[6]
    strat = BetaSpy()
    trade = OpenTrade(ticker='SPY', direction=1, entry_date=dates[0], entry_price=500.0,
                      entry_fill=500.0, stop_loss=300.0, target_1=2500.0, hold_cap=HOLD_DAYS,
                      entry_regime='LOW_VOL',
                      signal_params={'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': 'LOW_VOL'},
                      slippage=0.0, prev_mark=500.0)
    book, closed_all, counters = [trade], [], {}
    for i, d in enumerate(dates[1:], start=1):
        closed = advance_open_book(book, d, {'SPY': bars}, panel.loc[:d],
                                   {'state': regimes[i], 'date': d.date().isoformat()},
                                   {'options': {}}, strat, dt_priority='stop', counters=counters)
        closed_all.extend(closed)
        if not book:
            break
    assert len(closed_all) == 1 and book == []
    t = closed_all[0]
    assert t['exit_reason'] == 'strategy_exit:regime_exit'
    assert t['exit_date'] == dates[6].date()
    assert t['holding_days'] == 6
    assert counters['hook_exits'] == 1
