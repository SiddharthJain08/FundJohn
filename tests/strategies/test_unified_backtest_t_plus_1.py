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

ROOT = Path(__file__).resolve().parents[2]
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


class TestRegimeStampingAndOverride:
    def _dataset_regime_changes_at_fill(self):
        # Regime is LOW_VOL on the signal bar (t) and CRISIS on the fill bar
        # (t+1). entry_regime must remain the SIGNAL-day regime (LOW_VOL).
        dates = pd.date_range('2024-01-01', periods=12, freq='B')
        closes = [100.0 + i for i in range(12)]
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        bars = _bars_from_closes({'AAA': rows}, dates)
        # signal fires at dates[9] (t); fill at dates[10] (t+1).
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        regimes[dates[10]] = 'CRISIS'   # different regime on the fill bar
        return close_wide, bars, regimes, dates

    def test_entry_regime_is_signal_day_not_fill_day(self):
        close_wide, bars, regimes, dates = self._dataset_regime_changes_at_fill()
        trades = _run_capture(_stub_cls(), close_wide, bars, regimes)
        assert trades
        tr = trades[0]
        assert pd.Timestamp(tr['entry_date']) == dates[10]   # fill = t+1
        assert tr['entry_regime'] == 'LOW_VOL'               # signal day = t

    def test_coupling_override_reanchors_to_t_plus_1_fill(self, monkeypatch):
        # With an injected param_override on the signal-day regime (LOW_VOL),
        # the recorded signal_stop/signal_target must sit the override's pct
        # distances from the t+1 fill (entry_price), not from close[t].
        #
        # Real regime_param_override contract (confirmed against source
        # src/execution/regime_param_override.py):
        #   * resolve_override(strategy_id, regime_state, *, injected=...)
        #     returns None UNLESS the gate env OPENCLAW_BACKTEST_COUPLED_RECS=='1'
        #     (gate_on()). With an injected map it ignores the DB and returns
        #     dict(injected.get(regime_state)) (or None if that regime absent).
        #   * The override dict fields are 'stop_pct' / 'target_pct' (flat
        #     fractional distances from entry — ABSOLUTE-REPLACE semantics).
        #   * apply_override(LONG): stop = entry*(1 - stop_pct),
        #                           target = entry*(1 + target_pct).
        # So we (a) flip the gate ON for this test and (b) key the injected
        # map on the SIGNAL-day regime (LOW_VOL), because _per_bar_simulate
        # calls resolve_override(strategy_id, str(regime_state)=signal-day
        # regime). entry_price is the t+1 fill close, proving re-anchoring.
        monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
        close_wide, bars, regimes, dates = self._dataset_regime_changes_at_fill()
        override = {'LOW_VOL': {'stop_pct': 0.10, 'target_pct': 0.15}}
        trades = _run_capture(_stub_cls(), close_wide, bars, regimes,
                              param_override=override)
        assert trades
        tr = trades[0]
        ep = tr['entry_price']
        # Re-anchored to the t+1 FILL price, not the signal-day close[t].
        assert abs(tr['signal_stop'] - ep * (1 - 0.10)) < 1e-6
        assert abs(tr['signal_target'] - ep * (1 + 0.15)) < 1e-6


class TestNaNFillSkip:
    """A signal whose t+1 fill bar has a non-finite price is unfillable and
    must be skipped — never recorded as a trade with a NaN entry_price.

    Regression for the 2026-06-15 incident: one corrupt price bar (BRK-B
    2026-04-07, OHLC=NaN) created a single NaN-entry trade per strategy whose
    NaN pnl_pct then poisoned the entire equal-weighted daily-return series →
    NaN Sharpe/max_dd/return for 6 high-trade-count strategies.
    """

    def test_signal_filling_on_nan_price_bar_is_skipped(self):
        from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

        dates = pd.date_range('2024-01-01', periods=13, freq='B')
        closes = [100.0 + i for i in range(13)]
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        # Corrupt the FILL bar (t+1 of the signal): a row with NaN OHLC.
        # Signal fires on dates[9] (len==10) -> fill = dates[10].
        nan = float('nan')
        rows[10] = (nan, nan, nan, nan)
        bars = _bars_from_closes({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})

        class OnceStub(BaseStrategy):
            id = 'stub_t1'
            min_lookback = 5
            active_in_regimes = list(CANONICAL_REGIMES)

            def generate_signals(self, prices, regime, universe, aux_data=None):
                # Fire exactly once, on the bar BEFORE the corrupt fill bar.
                if prices.index[-1] != dates[9] or not universe:
                    return []
                t = universe[0]
                if t not in prices.columns:
                    return []
                close = float(prices[t].iloc[-1])
                return [Signal(ticker=t, direction='LONG', entry_price=close,
                               stop_loss=close * 0.93, target_1=close * 1.08,
                               target_2=0.0, target_3=0.0,
                               position_size_pct=0.0, confidence='MED')]

        trades = _run_capture(OnceStub, close_wide, bars, regimes)
        assert trades == [], \
            'a signal that can only fill on a NaN price bar must be skipped'


class TestAggregateMetricsNaNGuard:
    """A single non-finite pnl_pct trade must not poison the aggregate metrics.
    One bad bar in a 100k-trade strategy should drop only that trade, not null
    out the strategy's Sharpe / max_dd / return.
    """

    @staticmethod
    def _mk(pnl, hold, day):
        import datetime as dt
        return {'pnl_pct': pnl, 'holding_days': hold,
                'entry_date': dt.date(2024, 1, day)}

    def _finite_trades(self):
        return [self._mk(0.05, 3, 2), self._mk(-0.02, 2, 5),
                self._mk(0.03, 4, 8), self._mk(-0.015, 1, 11),
                self._mk(0.04, 2, 15)]

    def test_single_nan_pnl_does_not_poison_metrics(self):
        trades = self._finite_trades()
        baseline = ub.aggregate_metrics(trades)
        assert baseline['sharpe'] is not None and np.isfinite(baseline['sharpe'])

        with_nan = trades + [self._mk(float('nan'), 3, 18)]
        out = ub.aggregate_metrics(with_nan)
        assert out['sharpe'] is not None and np.isfinite(out['sharpe'])
        assert np.isfinite(out['max_dd_pct'])
        assert np.isfinite(out['return_pct'])
        # Dropping the corrupt trade recovers exactly the clean-data metrics.
        assert abs(out['sharpe'] - baseline['sharpe']) < 1e-9
        assert abs(out['return_pct'] - baseline['return_pct']) < 1e-9

    def test_all_nan_pnl_returns_none_sharpe(self):
        out = ub.aggregate_metrics([self._mk(float('nan'), 2, 3)])
        assert out['sharpe'] is None
        assert np.isfinite(out['max_dd_pct'])
        assert np.isfinite(out['return_pct'])
