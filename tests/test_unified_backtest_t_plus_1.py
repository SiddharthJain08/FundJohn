"""Tests for the signal[t] -> execute[t+1] fill model in unified_backtest.

Covers the pure bracket re-anchor helper and the _per_bar_simulate fill/exit
behavior (next-bar-close fill overriding strategy entry_price, pct-shape
bracket re-anchor, entry_date=t+1 / entry_regime=t stamping, last-bar skip,
and coupling-override composition).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


class TestReanchorBracket:
    def test_long_preserves_pct_distances(self):
        # ref=100, stop=93 (7% below), target=108 (8% above); new fill=110
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=110.0, direction=1,
            stop_ref=93.0, target_ref=108.0)
        assert abs(stop - 110.0 * 0.93) < 1e-9
        assert abs(target - 110.0 * 1.08) < 1e-9

    def test_short_preserves_pct_distances(self):
        # short: ref=100, stop=107 (7% above), target=92 (8% below); new fill=90
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=90.0, direction=-1,
            stop_ref=107.0, target_ref=92.0)
        assert abs(stop - 90.0 * 1.07) < 1e-9
        assert abs(target - 90.0 * 0.92) < 1e-9

    def test_identity_when_fill_equals_ref(self):
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=100.0, direction=1,
            stop_ref=95.0, target_ref=110.0)
        assert abs(stop - 95.0) < 1e-9
        assert abs(target - 110.0) < 1e-9


def _make_mock_conn():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


def _bars_from_closes(closes_by_ticker, dates):
    """Build bars_by_ticker with explicit OHLC. closes_by_ticker maps
    ticker -> list[ (open, high, low, close) ] aligned to dates."""
    out = {}
    for t, rows in closes_by_ticker.items():
        df = pd.DataFrame(
            {'open':  [r[0] for r in rows],
             'high':  [r[1] for r in rows],
             'low':   [r[2] for r in rows],
             'close': [r[3] for r in rows]},
            index=pd.DatetimeIndex(dates, name='date'),
        )
        out[t] = df
    return out


def _run_capture(strategy_cls, close_wide, bars_by_ticker, regimes,
                 *, param_override=None):
    """Run run_backtest with mocked IO; return the list of trade dicts the
    simulator produced. We capture trades by patching aggregate_metrics to
    stash its input (the trades list) on a mutable holder."""
    captured = {'trades': None}
    real_aggregate = ub.aggregate_metrics

    def capturing_aggregate(trades):
        # aggregate_metrics is called multiple times per run: once with the
        # FULL trade list (run_backtest line ~752) and then once per regime
        # subset inside aggregate_per_regime (line ~438). Only capture the
        # first (full) call so per-regime subsets don't overwrite it.
        if captured['trades'] is None:
            captured['trades'] = list(trades)
        return real_aggregate(trades)

    mock_conn = _make_mock_conn()
    with (
        patch('backtest.unified_backtest.load_prices_panels',
              return_value=(close_wide, bars_by_ticker)),
        patch('backtest.unified_backtest.load_regimes', return_value=regimes),
        patch('backtest.unified_backtest.load_strategy_class',
              return_value=strategy_cls),
        patch('backtest.unified_backtest.find_strategy_file',
              return_value=str(ROOT / 'src/strategies/implementations/momentum_12_1.py')),
        patch('backtest.unified_backtest._code_sha', return_value='abc123'),
        patch('backtest.unified_backtest.aggregate_metrics',
              side_effect=capturing_aggregate),
        patch('backtest.unified_backtest.psycopg2.extras.execute_values'),
    ):
        ub.run_backtest('stub_t1', conn=mock_conn, commit=False,
                        param_override=param_override)
    return captured['trades']


def _stub_cls(entry_offset_pct=0.0, stop_pct=0.07, target_pct=0.08,
              direction='LONG'):
    """A strategy that, once it has >=10 bars, emits one signal on the LAST
    bar of `prices` for the first universe ticker. entry_price is set to the
    signal-day close * (1+entry_offset_pct) to exercise the override path."""
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_t1'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or not universe:
                return []
            ticker = universe[0]
            if ticker not in prices.columns:
                return []
            close = float(prices[ticker].iloc[-1])
            ep = close * (1 + entry_offset_pct)
            if direction == 'LONG':
                return [Signal(ticker=ticker, direction='LONG', entry_price=ep,
                               stop_loss=ep * (1 - stop_pct),
                               target_1=ep * (1 + target_pct),
                               target_2=0.0, target_3=0.0,
                               position_size_pct=0.0, confidence='MED')]
            return [Signal(ticker=ticker, direction='SHORT', entry_price=ep,
                           stop_loss=ep * (1 + stop_pct),
                           target_1=ep * (1 - target_pct),
                           target_2=0.0, target_3=0.0,
                           position_size_pct=0.0, confidence='MED')]

    return Stub


class TestNextBarFill:
    def _trivial_dataset(self):
        # 12 business days; close ramps so every adjacent pair differs.
        dates = pd.date_range('2024-01-01', periods=12, freq='B')
        closes = [100.0 + i for i in range(12)]
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        bars = _bars_from_closes({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        return close_wide, bars, regimes, dates, closes

    def test_fill_is_next_bar_close_overriding_entry_price(self):
        close_wide, bars, regimes, dates, closes = self._trivial_dataset()
        trades = _run_capture(_stub_cls(), close_wide, bars, regimes)
        assert trades, 'stub should have produced at least one trade'
        tr = trades[0]
        entry_dt = pd.Timestamp(tr['entry_date'])
        pos = list(dates).index(entry_dt)
        # entry_price must equal close at the fill bar (t+1), NOT close[t].
        assert abs(tr['entry_price'] - closes[pos]) < 1e-9
        # And it must differ from the signal-bar close (t = pos-1).
        assert abs(tr['entry_price'] - closes[pos - 1]) > 0.5


class TestExitTimingAndLastBar:
    def test_exit_ignores_fill_bar_and_walks_from_t_plus_2(self):
        # Signal fires on bar t (the last bar of prices when len>=10).
        # The TARGET is touchable on the FILL bar (t+1) but must be ignored
        # (no same-bar exit on the fill), then first legitimately reachable on
        # t+2. We assert the exit_date is strictly after the fill bar and lands
        # on t+2.
        dates = pd.date_range('2024-01-01', periods=13, freq='B')
        closes = [100.0] * 13
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = [(100.0, 100.2, 99.8, 100.0) for _ in range(13)]
        # Signal fires when len(prices)>=10 -> current_date = dates[9];
        # fill = dates[10]. Spike the FILL bar's HIGH so a wrong same-bar check
        # would exit there; the target must instead first fire at t+2 = dates[11].
        rows[10] = (100.0, 999.0, 99.8, 100.0)   # fill bar: huge high (must be ignored)
        rows[11] = (100.0, 999.0, 99.8, 100.0)   # t+2: target legitimately hit here
        bars = _bars_from_closes({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        trades = _run_capture(_stub_cls(stop_pct=0.50, target_pct=0.08),
                              close_wide, bars, regimes)
        assert trades
        tr = trades[0]
        fill_dt = pd.Timestamp(tr['entry_date'])     # = dates[10]
        exit_dt = pd.Timestamp(tr['exit_date'])
        assert exit_dt > fill_dt, 'exit must be strictly after the fill bar'
        assert tr['exit_reason'] == 'target'
        assert exit_dt == dates[11]

    def test_signal_on_last_bar_is_skipped(self):
        # A strategy whose signal bar is the FINAL dataset bar -> no t+1 ->
        # the trade is skipped (zero trades).
        from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

        dates = pd.date_range('2024-01-01', periods=12, freq='B')
        closes = [100.0 + i for i in range(12)]
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        bars = _bars_from_closes({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})

        class LastBarStub(BaseStrategy):
            id = 'stub_t1'
            min_lookback = 5
            active_in_regimes = list(CANONICAL_REGIMES)

            def generate_signals(self, prices, regime, universe, aux_data=None):
                # fire ONLY when prices ends on the very last dataset date
                if prices.index[-1] != dates[-1] or not universe:
                    return []
                t = universe[0]
                c = float(prices[t].iloc[-1])
                return [Signal(ticker=t, direction='LONG', entry_price=c,
                               stop_loss=c * 0.93, target_1=c * 1.08,
                               target_2=0.0, target_3=0.0,
                               position_size_pct=0.0, confidence='MED')]

        trades = _run_capture(LastBarStub, close_wide, bars, regimes)
        assert trades == []
