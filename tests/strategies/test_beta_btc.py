"""S_beta_btc — crypto benchmark sleeve (crypto engine Phase 3, Task 3).

Clones tests/strategies/test_beta_spy.py + test_beta_spy_exit_hook.py for
BTC-USD, should_exit reads the regime it is handed: the engine routes the crypto
regime to crypto-class hooks (tests/execution/test_update_pnl_exit_hook_crypto.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.base import CANONICAL_REGIMES  # noqa: E402
from strategies.implementations import S_beta_btc as mod  # noqa: E402
from strategies.implementations.S_beta_btc import BetaBtc, STRATEGY_ID, HOLD_DAYS  # noqa: E402
from backtest.open_book import OpenTrade, advance_open_book  # noqa: E402


def _panel(n=30, btc=True):
    idx = pd.date_range('2026-01-02', periods=n, freq='D')
    cols = {'ETH-USD': np.linspace(3000, 3100, n)}
    if btc:
        cols['BTC-USD'] = np.linspace(90000, 100000, n)
    return pd.DataFrame(cols, index=idx)


def _pos(entry_regime):
    return {'ticker': 'BTC-USD', 'direction': 'LONG', 'entry_price': 90000.0,
            'entry_date': pd.Timestamp('2026-01-05'), 'days_held': 3,
            'stop_loss': 54000.0, 'target_1': 450000.0,
            'signal_params': {'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': entry_regime}}


def _backtest_payload(state):
    """Exact key shape of unified_backtest._regime_payload_full."""
    return {'state': state, 'date': '2026-01-08',
            'one_hot': {r: (1.0 if r == state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                                 for r1 in CANONICAL_REGIMES}}


# ── contract ────────────────────────────────────────────────────────────────
def test_contract():
    assert BetaBtc.id == STRATEGY_ID == 'S_beta_btc'
    assert BetaBtc.instrument_class == 'crypto'
    assert BetaBtc.benchmark_sleeve is True
    assert BetaBtc.exit_hook is True
    assert BetaBtc.tier == 2
    assert BetaBtc.signal_frequency == 'daily'
    assert BetaBtc.min_lookback == 2
    assert BetaBtc.MAX_SIGNALS == 1
    assert BetaBtc.BENCHMARK == mod.BENCHMARK == 'BTC-USD'
    assert set(BetaBtc.active_in_regimes) == {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert HOLD_DAYS == 21 and mod.STOP_FRAC == 0.60 and mod.TARGET_FRACS == (5.0, 6.0, 7.0)


def test_registry_and_manifest_wiring():
    from strategies.registry import _IMPL_MAP, load_strategy_class
    from strategies.instrument_class import instrument_class_for
    assert _IMPL_MAP['S_beta_btc'] == ('strategies.implementations.S_beta_btc', 'BetaBtc')
    assert load_strategy_class('S_beta_btc') is BetaBtc
    # the engine routes the crypto regime into generate_signals off the MANIFEST class
    assert instrument_class_for('S_beta_btc') == 'crypto'
    import json
    m = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
    e = m['strategies']['S_beta_btc']
    assert e['state'] == 'candidate'
    assert e['instrument_class'] == 'crypto'
    md = e['metadata']
    assert md['benchmark_sleeve'] is True
    assert md['backtest_tickers'] == ['BTC-USD']
    assert set(md['eligible_regimes']) == set(CANONICAL_REGIMES)
    assert md['canonical_file'] == 'S_beta_btc.py' and md['class'] == 'BetaBtc'


# ── signals ─────────────────────────────────────────────────────────────────
def test_one_long_btc_per_bar_every_regime():
    s = BetaBtc()
    for R in CANONICAL_REGIMES:
        out = s.generate_signals(_panel(), {'state': R}, ['BTC-USD', 'ETH-USD'])
        assert len(out) == 1
        sig = out[0]
        assert (sig.ticker, sig.direction) == ('BTC-USD', 'LONG')
        assert sig.entry_price == 100000.0
        assert sig.signal_params == {'hold_days': 21, 'benchmark_sleeve': True, 'regime': R}
        assert sig.stop_loss < 0.7 * sig.entry_price
        assert sig.target_1 > 3.0 * sig.entry_price


def test_no_btc_column_or_bad_price_is_silent():
    s = BetaBtc()
    assert s.generate_signals(_panel(btc=False), {'state': 'LOW_VOL'}, ['ETH-USD']) == []
    p = _panel(); p.loc[p.index[-1], 'BTC-USD'] = np.nan
    assert s.generate_signals(p, {'state': 'LOW_VOL'}, ['BTC-USD']) == []
    assert s.generate_signals(pd.DataFrame(), {'state': 'LOW_VOL'}, ['BTC-USD']) == []
    assert s.generate_signals(None, {'state': 'LOW_VOL'}, ['BTC-USD']) == []


def test_non_numeric_btc_close_logs_debug_message(capsys):
    idx = pd.date_range('2026-01-02', periods=30, freq='D')
    p = pd.DataFrame({'BTC-USD': np.linspace(90000, 100000, 29).tolist() + ['n/a']}, index=idx)
    assert BetaBtc().generate_signals(p, {'state': 'LOW_VOL'}, ['BTC-USD']) == []
    assert 'not numeric' in capsys.readouterr().err


# ── exit hook: plain regime dicts (backtest shape) ─────────────────────────
@pytest.mark.parametrize('entry', CANONICAL_REGIMES)
def test_same_regime_holds_other_regime_exits(entry):
    s = BetaBtc()
    for state in CANONICAL_REGIMES:
        got = s.should_exit(_pos(entry), pd.DataFrame(), {'state': state})
        assert got == (None if state == entry else 'regime_exit')


def test_backtest_payload_state_is_used():
    s = BetaBtc()
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), _backtest_payload('HIGH_VOL')) == 'regime_exit'
    assert s.should_exit(_pos('HIGH_VOL'), pd.DataFrame(), _backtest_payload('HIGH_VOL')) is None


def test_missing_or_unknown_state_holds():
    s = BetaBtc()
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), {}) is None
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), {'state': 'UNKNOWN'}) is None
    assert s.should_exit(_pos('LOW_VOL'), pd.DataFrame(), None) is None
    p = _pos('LOW_VOL'); p['signal_params'] = {}
    assert s.should_exit(p, pd.DataFrame(), {'state': 'CRISIS'}) is None
    p['signal_params'] = None
    assert s.should_exit(p, pd.DataFrame(), {'state': 'CRISIS'}) is None


# ── backtest open book closes on the crypto flip bar ───────────────────────
def test_open_book_closes_on_the_flip_bar():
    dates = pd.date_range('2026-01-05', periods=12, freq='D')
    closes = [90000.0 + 100 * i for i in range(12)]
    bars = pd.DataFrame({'open': closes, 'high': [c + 50 for c in closes],
                         'low': [c - 50 for c in closes], 'close': closes},
                        index=pd.DatetimeIndex(dates, name='date'))
    panel = pd.DataFrame({'BTC-USD': closes}, index=dates)
    regimes = ['LOW_VOL'] * 6 + ['HIGH_VOL'] * 6
    strat = BetaBtc()
    trade = OpenTrade(ticker='BTC-USD', direction=1, entry_date=dates[0], entry_price=90000.0,
                      entry_fill=90000.0, stop_loss=54000.0, target_1=450000.0, hold_cap=HOLD_DAYS,
                      entry_regime='LOW_VOL',
                      signal_params={'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': 'LOW_VOL'},
                      slippage=0.0, prev_mark=90000.0)
    book, closed_all, counters = [trade], [], {}
    for i, d in enumerate(dates[1:], start=1):
        closed = advance_open_book(book, d, {'BTC-USD': bars}, panel.loc[:d],
                                   _backtest_payload(regimes[i]),
                                   {'options': {}}, strat, dt_priority='stop', counters=counters)
        closed_all.extend(closed)
        if not book:
            break
    assert len(closed_all) == 1 and book == []
    t = closed_all[0]
    assert t['exit_reason'] == 'strategy_exit:regime_exit'
    assert t['exit_date'] == dates[6].date()
    assert counters['hook_exits'] == 1


def test_requirements_declares_prices_only():
    # without it the engine's aux planner fails toward "load every lazy aux kind"
    import json
    p = ROOT / 'src' / 'strategies' / 'implementations' / 'S_beta_btc.requirements.json'
    req = json.loads(p.read_text())
    assert req == {'strategy_id': 'S_beta_btc', 'required': ['prices'], 'optional': []}
    import execution.engine as eng
    assert eng._strategy_aux_needs(BetaBtc()) == set()
