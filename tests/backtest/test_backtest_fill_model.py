"""Tests for the fill_model kwarg added to unified_backtest (SP-6).

Test plan:
  a. REGRESSION: default kwarg == explicit fill_model="close" — trades identical.
  b. Open-fill entry: fill_model="open" → entry price = open[t+1]; bracket re-anchored.
  c. Fill-bar exit eligibility: low breaches re-anchored stop on fill bar under 'open';
     under 'close' exits later.
  d. ValueError on fill_model="banana".
  e. Driver unit: --single monkeypatched; --summarize gates + verdict strings.
     e1. summarize on a synthetic results.jsonl missing 2 of a fake 5-strategy book
         → INCOMPLETE-COVERAGE verdict, bar not evaluated.
     e2. same with --allow-partial → verdict computed + PARTIAL BOOK headline.
     e3. timeout row schema countable + retried-on-resume logic (a sid with
         status:timeout is NOT treated as done).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


# ── Fixtures shared with test_unified_backtest_t_plus_1.py patterns ──────────

def _make_mock_conn():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


def _bars_from_rows(rows_by_ticker: dict, dates):
    """Build bars_by_ticker with explicit OHLC.
    rows_by_ticker: {ticker: [(open, high, low, close), ...]} aligned to dates.
    """
    out = {}
    for ticker, rows in rows_by_ticker.items():
        df = pd.DataFrame(
            {'open':  [r[0] for r in rows],
             'high':  [r[1] for r in rows],
             'low':   [r[2] for r in rows],
             'close': [r[3] for r in rows]},
            index=pd.DatetimeIndex(dates, name='date'),
        )
        out[ticker] = df
    return out


def _make_stub_cls(entry_offset_pct=0.0, stop_pct=0.07, target_pct=0.08,
                   direction='LONG'):
    """Strategy that emits one signal when len(prices) >= 10, on the last bar."""
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_fm'
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


def _run_capture(strategy_cls, close_wide, bars_by_ticker, regimes,
                 fill_model='close', param_override=None):
    """Run run_backtest with mocked IO and return list of trade dicts."""
    captured = {'trades': None}
    real_aggregate = ub.aggregate_metrics

    def capturing_aggregate(trades):
        if captured['trades'] is None:
            captured['trades'] = list(trades)
        return real_aggregate(trades)

    mock_conn = _make_mock_conn()
    # Honest-cost gates OFF (2026-07-27): these tests assert fill GEOMETRY on
    # synthetic bars; the fixture ticker 'AAA' is also a real ETF whose ADV sits
    # below the production liquidity floor, and per-ticker spread costs would
    # perturb the expected fill arithmetic. Gate behavior has its own tests.
    with (
        patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off',
                                'OPENCLAW_BT_SPREAD_COSTS': '0'}),
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
        ub.run_backtest('stub_fm', conn=mock_conn, commit=False,
                        fill_model=fill_model, param_override=param_override)
    return captured['trades']


def _trivial_dataset(n=12, open_delta=5.0):
    """12 business days; close ramps; open is close-open_delta (distinct)."""
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    closes = [100.0 + i for i in range(n)]
    opens = [c - open_delta for c in closes]
    close_wide = pd.DataFrame({'AAA': closes}, index=dates)
    close_wide.index.name = 'date'
    rows = [(o, c + 0.2, c - 0.2, c) for o, c in zip(opens, closes)]
    bars = _bars_from_rows({'AAA': rows}, dates)
    regimes = pd.Series({d: 'LOW_VOL' for d in dates})
    return close_wide, bars, regimes, dates, closes, opens


# ── (a) DEFAULT RESOLUTION: default == same_close; env pins legacy ───────────

class TestRegression:
    """2026-07-29 same-day pivot: the unset default resolves to 'same_close';
    OPENCLAW_BT_FILL_MODEL restores the legacy t+1 models exactly."""

    @staticmethod
    def _assert_identical(a, b, label):
        assert a is not None and b is not None
        assert len(a) == len(b), f'trade count must match ({label})'
        for td, te in zip(a, b):
            assert td['entry_price'] == te['entry_price'], label
            assert td['exit_date'] == te['exit_date'], label
            assert td['exit_reason'] == te['exit_reason'], label
            assert td['pnl_pct'] == te['pnl_pct'], label

    def test_default_equals_explicit_same_close(self):
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCLAW_BT_FILL_MODEL', None)
            trades_default = _run_capture(stub, close_wide, bars, regimes,
                                          fill_model=None)
        trades_explicit = _run_capture(stub, close_wide, bars, regimes,
                                       fill_model='same_close')
        self._assert_identical(trades_default, trades_explicit,
                               'default vs explicit same_close')

    def test_env_pins_legacy_close(self):
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        with patch.dict(os.environ, {'OPENCLAW_BT_FILL_MODEL': 'close'}):
            trades_default = _run_capture(stub, close_wide, bars, regimes,
                                          fill_model=None)
        trades_explicit = _run_capture(stub, close_wide, bars, regimes,
                                       fill_model='close')
        self._assert_identical(trades_default, trades_explicit,
                               'env close vs explicit close')

    def test_close_fill_is_next_bar_close(self):
        """Entry price under 'close' must equal close[t+1], not open[t+1]."""
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset(open_delta=5.0)
        stub = _make_stub_cls()
        trades = _run_capture(stub, close_wide, bars, regimes, fill_model='close')
        assert trades
        tr = trades[0]
        entry_dt = pd.Timestamp(tr['entry_date'])
        pos = list(dates).index(entry_dt)
        # Must equal close[pos], not open[pos].
        assert abs(tr['entry_price'] - closes[pos]) < 1e-9, \
            f"close fill: entry_price={tr['entry_price']} != close[{pos}]={closes[pos]}"
        assert abs(tr['entry_price'] - opens[pos]) > 1.0, \
            'close fill must differ from open[t+1]'


# ── (b) Open-fill entry ───────────────────────────────────────────────────────

class TestOpenFillEntry:
    def test_open_fill_entry_price_is_next_bar_open(self):
        """Under fill_model='open', entry_price must equal open[t+1]."""
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset(open_delta=5.0)
        stub = _make_stub_cls()
        trades = _run_capture(stub, close_wide, bars, regimes, fill_model='open')
        assert trades
        tr = trades[0]
        entry_dt = pd.Timestamp(tr['entry_date'])
        pos = list(dates).index(entry_dt)
        # Must equal open[pos], not close[pos].
        assert abs(tr['entry_price'] - opens[pos]) < 1e-9, \
            f"open fill: entry_price={tr['entry_price']} != open[{pos}]={opens[pos]}"
        assert abs(tr['entry_price'] - closes[pos]) > 1.0, \
            'open fill must differ from close[t+1]'

    def test_open_fill_bracket_reanchored_off_open(self):
        """Bracket distances must be preserved from the open fill price."""
        stop_pct, target_pct = 0.07, 0.08
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset(open_delta=5.0)
        stub = _make_stub_cls(stop_pct=stop_pct, target_pct=target_pct)
        trades = _run_capture(stub, close_wide, bars, regimes, fill_model='open')
        assert trades
        tr = trades[0]
        ep = tr['entry_price']
        expected_stop = ep * (1 - stop_pct)
        expected_target = ep * (1 + target_pct)
        assert abs(tr['signal_stop'] - expected_stop) < 1e-4, \
            f"stop not reanchored: {tr['signal_stop']} != {expected_stop}"
        assert abs(tr['signal_target'] - expected_target) < 1e-4, \
            f"target not reanchored: {tr['signal_target']} != {expected_target}"


# ── (c) Fill-bar exit eligibility ────────────────────────────────────────────

class TestFillBarExitEligibility:
    """Under 'open', the fill bar's H/L are eligible for bracket exits.
    Under 'close', they are not (walk starts strictly AFTER fill bar).
    """

    def _dataset_with_stop_on_fill_bar(self):
        """13-bar dataset where:
          - Signal fires at bar 9 (dates[9]) when len(prices)==10.
          - Fill bar = dates[10].
          - fill_model='open': fill at open[10] = 90.0 (low).
            Stop re-anchored to open[10] * (1 - 0.05) = 85.5.
            Fill bar LOW = 80.0 → breaches stop → exits ON fill bar.
          - fill_model='close': fill at close[10] = 100.0.
            Stop re-anchored to close[10] * (1 - 0.05) = 95.0.
            Fill bar LOW = 80.0 would breach, but fill bar is excluded.
            Walk starts at bar 11; bar 11 LOW = 100.0 (above stop) → no breach;
            bar 12 close-exit (max_hold or end_of_data).
        """
        dates = pd.date_range('2024-01-01', periods=13, freq='B')
        # Constant close=100 to keep signals predictable.
        closes = [100.0] * 13
        # open[10] = 90 (well below close so bracket is clearly reanchored differently)
        opens = [100.0] * 13
        opens[10] = 90.0
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = list(zip(opens,
                        [c + 0.2 for c in closes],   # high
                        [c - 0.2 for c in closes],   # low (safe by default)
                        closes))
        # Fill bar (bar 10): low = 80.0 → triggers stop for both fill models in theory,
        # but 'close' excludes this bar from the walk.
        rows[10] = (90.0, 100.2, 80.0, 100.0)
        # bar 11: safe (low=99.8 — won't trigger stop at 95, won't reach target)
        rows[11] = (100.0, 100.2, 99.8, 100.0)
        # bar 12: safe too
        rows[12] = (100.0, 100.2, 99.8, 100.0)
        bars = _bars_from_rows({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        return close_wide, bars, regimes, dates

    def test_open_fill_exits_on_fill_bar(self):
        """Under fill_model='open', fill bar low breaches stop → exit on fill bar."""
        close_wide, bars, regimes, dates = self._dataset_with_stop_on_fill_bar()
        # stop_pct=0.05; fill at open=90 → stop=85.5; fill bar LOW=80.0 < 85.5 → STOP
        stub = _make_stub_cls(stop_pct=0.05, target_pct=0.50)
        trades = _run_capture(stub, close_wide, bars, regimes, fill_model='open')
        assert trades, 'should produce a trade'
        tr = trades[0]
        fill_dt = pd.Timestamp(tr['entry_date'])   # = dates[10]
        exit_dt = pd.Timestamp(tr['exit_date'])
        assert fill_dt == dates[10], f'fill bar should be dates[10], got {fill_dt}'
        assert exit_dt == dates[10], \
            f"under 'open', fill-bar stop should fire on fill bar, got exit={exit_dt}"
        assert tr['exit_reason'] == 'stop', f"expected stop, got {tr['exit_reason']}"

    def test_close_fill_does_not_exit_on_fill_bar(self):
        """Under fill_model='close', fill bar is excluded → exit is NOT on fill bar."""
        close_wide, bars, regimes, dates = self._dataset_with_stop_on_fill_bar()
        # stop_pct=0.05; fill at close=100 → stop=95; fill bar LOW=80.0 would breach
        # but fill bar is excluded. Bars 11,12 have low=99.8 > 95 → no stop.
        # max_hold=2 → exits at bar 12 (close) with reason max_hold or end_of_data.
        stub = _make_stub_cls(stop_pct=0.05, target_pct=0.50)
        trades = _run_capture(stub, close_wide, bars, regimes, fill_model='close')
        assert trades, 'should produce a trade'
        tr = trades[0]
        fill_dt = pd.Timestamp(tr['entry_date'])
        exit_dt = pd.Timestamp(tr['exit_date'])
        assert fill_dt == dates[10], f'fill bar should be dates[10], got {fill_dt}'
        assert exit_dt > fill_dt, \
            f"under 'close', exit must be strictly after fill bar, got {exit_dt}"


# ── (d) ValueError on invalid fill_model ─────────────────────────────────────

class TestSameCloseFill:
    """'same_close' (2026-07-29 same-day pivot): signal[t] fills at close[t],
    exit walk starts strictly at t+1, final-bar signals cannot fill."""

    def test_entry_is_signal_bar_close(self):
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        trades = _run_capture(stub, close_wide, bars, regimes,
                              fill_model='same_close')
        assert trades
        tr = sorted(trades, key=lambda t: t['entry_date'])[0]
        # First signal fires at index 9 (len(prices) >= 10) and fills THAT bar.
        assert pd.Timestamp(tr['entry_date']) == dates[9]
        assert abs(tr['entry_price'] - closes[9]) < 1e-9, \
            f"same_close fill: entry_price={tr['entry_price']} != close[9]={closes[9]}"

    def test_one_day_earlier_than_legacy_close(self):
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        same = sorted(_run_capture(stub, close_wide, bars, regimes,
                                   fill_model='same_close'),
                      key=lambda t: t['entry_date'])
        legacy = sorted(_run_capture(stub, close_wide, bars, regimes,
                                     fill_model='close'),
                        key=lambda t: t['entry_date'])
        assert same and legacy
        assert pd.Timestamp(same[0]['entry_date']) < pd.Timestamp(legacy[0]['entry_date'])

    def test_last_bar_signal_does_not_fill(self):
        """A signal on the final bar has no exit walk — must be skipped, so no
        trade may carry entry_date == last bar."""
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        trades = _run_capture(stub, close_wide, bars, regimes,
                              fill_model='same_close')
        assert trades
        assert max(pd.Timestamp(t['entry_date']) for t in trades) < dates[-1]

    def test_signal_bar_low_not_exit_eligible(self):
        """The fill bar's own H/L happened BEFORE the close fill — a deep low
        on the signal bar must not stop the trade out same-bar; the stop hits
        on the NEXT bar."""
        n = 13
        dates = pd.date_range('2024-01-01', periods=n, freq='B')
        rows = [(100.0, 100.3, 80.0, 100.0)] * n   # every low far below a 7% stop
        close_wide = pd.DataFrame({'AAA': [r[3] for r in rows]}, index=dates)
        close_wide.index.name = 'date'
        bars = _bars_from_rows({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        stub = _make_stub_cls()
        trades = _run_capture(stub, close_wide, bars, regimes,
                              fill_model='same_close')
        assert trades
        tr = sorted(trades, key=lambda t: t['entry_date'])[0]
        assert pd.Timestamp(tr['entry_date']) == dates[9]
        assert pd.Timestamp(tr['exit_date']) == dates[10], \
            'stop must trigger on the bar AFTER the same-close fill, not the fill bar'


class TestFillModelValidation:
    def test_invalid_fill_model_raises(self):
        with pytest.raises(ValueError, match='fill_model'):
            ub._per_bar_simulate(
                MagicMock(), pd.DataFrame(), {}, pd.Series(),
                pd.Timestamp('2024-01-01'), pd.Timestamp('2024-12-31'),
                fill_model='banana',
            )

    def test_run_backtest_invalid_fill_model_raises(self):
        """ValueError must propagate through run_backtest."""
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        mock_conn = _make_mock_conn()
        with (
            patch('backtest.unified_backtest.load_prices_panels',
                  return_value=(close_wide, bars)),
            patch('backtest.unified_backtest.load_regimes', return_value=regimes),
            patch('backtest.unified_backtest.load_strategy_class',
                  return_value=stub),
            patch('backtest.unified_backtest.find_strategy_file',
                  return_value=str(ROOT / 'src/strategies/implementations/momentum_12_1.py')),
            patch('backtest.unified_backtest._code_sha', return_value='abc123'),
            patch('backtest.unified_backtest.psycopg2.extras.execute_values'),
        ):
            with pytest.raises(ValueError, match='fill_model'):
                ub.run_backtest('stub_fm', conn=mock_conn, commit=False,
                                fill_model='banana')


# ── (e) Driver unit tests ─────────────────────────────────────────────────────

class TestDriverUnit:
    """Unit tests for backtest_fill_model_study.py --single and --summarize."""

    def _import_driver(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'backtest_fill_model_study',
            str(ROOT / 'scripts' / 'backtest_fill_model_study.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_single_emits_valid_json_with_required_fields(self):
        """--single mode with a faked run_backtest must emit valid JSON containing
        delta_sharpe, trades_parity_pct, and both model dicts."""
        driver = self._import_driver()

        fake_metrics_close = {
            'sharpe': 1.2, 'total_trades': 50, 'hit_rate': 0.55,
            'max_dd_pct': 10.0, 'return_pct': 42.0, 'avg_holding_days': 5.0,
            'avg_pnl_pct': 0.8,
        }
        fake_metrics_open = {
            'sharpe': 1.5, 'total_trades': 51, 'hit_rate': 0.58,
            'max_dd_pct': 9.0, 'return_pct': 50.0, 'avg_holding_days': 4.5,
            'avg_pnl_pct': 1.0,
        }
        call_count = [0]

        def fake_run_backtest(sid, *, fill_model='close', commit=False,
                              return_metrics=False, instrument_class='equity', conn=None,
                              **kwargs):
            call_count[0] += 1
            if fill_model == 'close':
                m = fake_metrics_close
            else:
                m = fake_metrics_open
            return 'fake-run-id', m

        with patch('backtest.unified_backtest.run_backtest', side_effect=fake_run_backtest):
            # run_backtest is fully mocked, so no real DB connection is opened
            # (the driver itself passes conn=None and relies on commit=False).
            row = driver._run_single('S_test')

        assert 'sid' in row, 'sid field missing'
        assert row['sid'] == 'S_test'
        assert 'close' in row and 'open' in row
        assert 'delta_sharpe' in row
        assert row['delta_sharpe'] is not None
        assert abs(row['delta_sharpe'] - (1.5 - 1.2)) < 1e-6
        assert 'trades_parity_pct' in row
        assert 'ts' in row
        # Parity: |51-50|/50 = 2% = exactly at threshold → NOT suspect (strictly >).
        assert not row['sim_suspect'], \
            'parity exactly at 2% boundary should not be SIM-SUSPECT (gate is >2%)'

    def test_single_sim_suspect_when_parity_exceeds_threshold(self):
        """Parity > 2% → sim_suspect=True."""
        driver = self._import_driver()

        def fake_run_backtest(sid, *, fill_model='close', **kwargs):
            if fill_model == 'close':
                m = {'sharpe': 1.0, 'total_trades': 100}
            else:
                m = {'sharpe': 1.1, 'total_trades': 104}  # 4% parity breach
            return 'fake-id', m

        with patch('backtest.unified_backtest.run_backtest', side_effect=fake_run_backtest):
            row = driver._run_single('S_test')

        assert row['sim_suspect'] is True
        assert row['trades_parity_pct'] > 0.02

    def test_summarize_consideration_bar_met(self):
        """With sufficient median ΔSharpe + majority positive → CONSIDERATION-BAR-MET."""
        driver = self._import_driver()

        sids = [f'S_{i:03d}' for i in range(10)]
        fake_rows = []
        for sid in sids:
            fake_rows.append({
                'sid': sid,
                'close': {'sharpe': 1.0, 'total_trades': 100},
                'open': {'sharpe': 1.2, 'total_trades': 100},
                'delta_sharpe': 0.2,
                'trades_parity_pct': 0.0,
                'sim_suspect': False,
                'ts': '2026-06-07T00:00:00',
            })

        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / 'results.jsonl'
            rf.write_text('\n'.join(json.dumps(r) for r in fake_rows))
            rep_f = Path(td) / 'report.md'
            # Patch both the file paths AND _live_strategies so coverage == completed.
            with patch.object(driver, 'RESULTS_FILE', rf), \
                 patch.object(driver, 'REPORT_FILE', rep_f), \
                 patch.object(driver, 'RESULTS_DIR', Path(td)), \
                 patch.object(driver, '_live_strategies', return_value=sids):
                verdict = driver._summarize()
        assert verdict == 'CONSIDERATION-BAR-MET'

    def test_summarize_close_fill_stands_low_delta(self):
        """With ΔSharpe < threshold → CLOSE-FILL-STANDS."""
        driver = self._import_driver()

        sids = [f'S_{i:03d}' for i in range(10)]
        fake_rows = []
        for sid in sids:
            fake_rows.append({
                'sid': sid,
                'close': {'sharpe': 1.0, 'total_trades': 100},
                'open': {'sharpe': 1.05, 'total_trades': 100},
                'delta_sharpe': 0.05,
                'trades_parity_pct': 0.0,
                'sim_suspect': False,
                'ts': '2026-06-07T00:00:00',
            })

        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / 'results.jsonl'
            rf.write_text('\n'.join(json.dumps(r) for r in fake_rows))
            rep_f = Path(td) / 'report.md'
            with patch.object(driver, 'RESULTS_FILE', rf), \
                 patch.object(driver, 'REPORT_FILE', rep_f), \
                 patch.object(driver, 'RESULTS_DIR', Path(td)), \
                 patch.object(driver, '_live_strategies', return_value=sids):
                verdict = driver._summarize()
        assert verdict == 'CLOSE-FILL-STANDS'

    def test_summarize_invalid_sim_too_many_suspects(self):
        """More than 5 SIM-SUSPECT strategies → INVALID-SIM."""
        driver = self._import_driver()

        sids = [f'S_{i:03d}' for i in range(8)]
        fake_rows = []
        for sid in sids:
            fake_rows.append({
                'sid': sid,
                'close': {'sharpe': 1.0, 'total_trades': 100},
                'open': {'sharpe': 1.2, 'total_trades': 120},  # 20% breach
                'delta_sharpe': 0.2,
                'trades_parity_pct': 0.20,
                'sim_suspect': True,
                'ts': '2026-06-07T00:00:00',
            })

        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / 'results.jsonl'
            rf.write_text('\n'.join(json.dumps(r) for r in fake_rows))
            rep_f = Path(td) / 'report.md'
            with patch.object(driver, 'RESULTS_FILE', rf), \
                 patch.object(driver, 'REPORT_FILE', rep_f), \
                 patch.object(driver, 'RESULTS_DIR', Path(td)), \
                 patch.object(driver, '_live_strategies', return_value=sids):
                verdict = driver._summarize()
        assert verdict == 'INVALID-SIM'

    # ── (e1) INCOMPLETE-COVERAGE: 3 of 5 complete, bar not evaluated ─────────

    def test_summarize_incomplete_coverage_no_allow_partial(self):
        """Synthetic 5-strategy book with only 3 complete rows (2 missing) →
        INCOMPLETE-COVERAGE verdict; the consideration bar is NOT evaluated
        (even though the 3 complete rows would clear it)."""
        driver = self._import_driver()

        all_sids = [f'S_{i:03d}' for i in range(5)]
        complete_sids = all_sids[:3]   # 3 complete
        # 3 complete rows — all high ΔSharpe (would pass consideration bar).
        fake_rows = []
        for sid in complete_sids:
            fake_rows.append({
                'sid': sid,
                'close': {'sharpe': 1.0, 'total_trades': 100},
                'open': {'sharpe': 1.2, 'total_trades': 100},
                'delta_sharpe': 0.2,
                'trades_parity_pct': 0.0,
                'sim_suspect': False,
                'ts': '2026-06-07T00:00:00',
            })

        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / 'results.jsonl'
            rf.write_text('\n'.join(json.dumps(r) for r in fake_rows))
            rep_f = Path(td) / 'report.md'
            with patch.object(driver, 'RESULTS_FILE', rf), \
                 patch.object(driver, 'REPORT_FILE', rep_f), \
                 patch.object(driver, 'RESULTS_DIR', Path(td)), \
                 patch.object(driver, '_live_strategies', return_value=all_sids):
                verdict = driver._summarize(allow_partial=False)

            # Must be INCOMPLETE-COVERAGE, NOT CONSIDERATION-BAR-MET.
            assert verdict.startswith('INCOMPLETE-COVERAGE'), \
                f'expected INCOMPLETE-COVERAGE, got: {verdict}'
            assert '3/5' in verdict, f'expected 3/5 in verdict, got: {verdict}'
            # Report must exist and have the Coverage section.
            report_text = rep_f.read_text()
            assert 'Coverage' in report_text
            # The missing sids should appear.
            for sid in all_sids[3:]:
                assert sid in report_text, f'missing sid {sid} not listed in report'

    # ── (e2) allow-partial: verdict computed, headline prefixed ──────────────

    def test_summarize_allow_partial_computes_verdict_and_prefixes(self):
        """Same 3-of-5 book but with --allow-partial → verdict is computed
        (CONSIDERATION-BAR-MET for high-ΔSharpe rows) AND the verdict string
        is prefixed with 'PARTIAL BOOK — 3/5'."""
        driver = self._import_driver()

        all_sids = [f'S_{i:03d}' for i in range(5)]
        complete_sids = all_sids[:3]
        fake_rows = []
        for sid in complete_sids:
            fake_rows.append({
                'sid': sid,
                'close': {'sharpe': 1.0, 'total_trades': 100},
                'open': {'sharpe': 1.2, 'total_trades': 100},
                'delta_sharpe': 0.2,
                'trades_parity_pct': 0.0,
                'sim_suspect': False,
                'ts': '2026-06-07T00:00:00',
            })

        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / 'results.jsonl'
            rf.write_text('\n'.join(json.dumps(r) for r in fake_rows))
            rep_f = Path(td) / 'report.md'
            with patch.object(driver, 'RESULTS_FILE', rf), \
                 patch.object(driver, 'REPORT_FILE', rep_f), \
                 patch.object(driver, 'RESULTS_DIR', Path(td)), \
                 patch.object(driver, '_live_strategies', return_value=all_sids):
                verdict = driver._summarize(allow_partial=True)

        assert 'PARTIAL BOOK' in verdict, \
            f'expected PARTIAL BOOK prefix, got: {verdict}'
        assert '3/5' in verdict, f'expected 3/5 in verdict, got: {verdict}'
        assert 'CONSIDERATION-BAR-MET' in verdict, \
            f'expected CONSIDERATION-BAR-MET embedded in verdict, got: {verdict}'

    # ── (e3) timeout row schema + retry-on-resume logic ─────────────────────

    def test_timeout_row_not_treated_as_done(self):
        """A row with status='timeout' must NOT be counted as done by _already_done();
        a subsequent complete row for the same sid IS done."""
        driver = self._import_driver()

        timeout_row = {
            'sid': 'S_slow',
            'status': 'timeout',
            'ts': '2026-06-07T00:00:00',
        }
        complete_row = {
            'sid': 'S_fast',
            'close': {'sharpe': 1.0, 'total_trades': 50},
            'open': {'sharpe': 1.1, 'total_trades': 50},
            'delta_sharpe': 0.1,
            'trades_parity_pct': 0.0,
            'sim_suspect': False,
            'ts': '2026-06-07T00:00:00',
        }

        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / 'results.jsonl'
            rf.write_text(
                json.dumps(timeout_row) + '\n' + json.dumps(complete_row) + '\n'
            )
            with patch.object(driver, 'RESULTS_FILE', rf):
                done = driver._already_done()

        # S_slow (timeout) must NOT be in done — it will be retried.
        assert 'S_slow' not in done, \
            'timeout row should not mark sid as done (must be retried)'
        # S_fast (complete) MUST be in done — it will be skipped.
        assert 'S_fast' in done, \
            'complete row should mark sid as done (skip on resume)'

    def test_timeout_row_counted_in_coverage_errored(self):
        """A timeout row's sid must appear in the missing_sids list and n_errored
        count in the coverage section of the report."""
        driver = self._import_driver()

        all_sids = ['S_fast', 'S_slow']
        complete_row = {
            'sid': 'S_fast',
            'close': {'sharpe': 1.0, 'total_trades': 50},
            'open': {'sharpe': 1.1, 'total_trades': 50},
            'delta_sharpe': 0.1,
            'trades_parity_pct': 0.0,
            'sim_suspect': False,
            'ts': '2026-06-07T00:00:00',
        }
        timeout_row = {
            'sid': 'S_slow',
            'status': 'timeout',
            'ts': '2026-06-07T00:00:00',
        }

        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / 'results.jsonl'
            rf.write_text(
                json.dumps(complete_row) + '\n' + json.dumps(timeout_row) + '\n'
            )
            rep_f = Path(td) / 'report.md'
            with patch.object(driver, 'RESULTS_FILE', rf), \
                 patch.object(driver, 'REPORT_FILE', rep_f), \
                 patch.object(driver, 'RESULTS_DIR', Path(td)), \
                 patch.object(driver, '_live_strategies', return_value=all_sids):
                verdict = driver._summarize(allow_partial=False)

            # 1 of 2 complete → INCOMPLETE-COVERAGE.
            assert 'INCOMPLETE-COVERAGE' in verdict
            report_text = rep_f.read_text()
            # S_slow must be called out as missing.
            assert 'S_slow' in report_text
            # Coverage section must show 1 timeout.
            assert 'timeout' in report_text


# ── evening-aux lag under the same-day fill model (2026-07-29) ───────────────

class TestAuxEveningLag:
    """Under same_close, evening-collected aux (options/vol/macro/financials/
    insider) must be served as-of t-1 — at 15:00 ET the day-t rows don't exist
    yet. Sentiment is ingested in-chain before signals, so it stays day-t."""

    _EVENING = ['_day_slice', '_vol_indices_slice', '_macro_slice',
                '_financials_slice', '_insider_slice', '_insider_long_slice']

    def _capture(self, monkeypatch):
        from strategies import aux_data_loader as adl
        calls = {}
        for name in self._EVENING + ['_sentiment_day_slice']:
            def _rec(d, _n=name):
                calls[_n] = d
                return {}
            monkeypatch.setattr(adl, name, _rec)
        return adl, calls

    def test_same_day_model_lags_evening_categories(self, monkeypatch):
        adl, calls = self._capture(monkeypatch)
        monkeypatch.delenv('OPENCLAW_BT_FILL_MODEL', raising=False)
        adl.load_aux_data('2026-07-28')
        for name in self._EVENING:
            assert calls[name] == '2026-07-27', f'{name} must lag to t-1'
        assert calls['_sentiment_day_slice'] == '2026-07-28'

    def test_legacy_model_keeps_same_day_aux(self, monkeypatch):
        adl, calls = self._capture(monkeypatch)
        monkeypatch.setenv('OPENCLAW_BT_FILL_MODEL', 'close')
        adl.load_aux_data('2026-07-28')
        for name in self._EVENING + ['_sentiment_day_slice']:
            assert calls[name] == '2026-07-28'

    def test_default_literal_matches_engine_default(self, monkeypatch):
        """aux_data_loader duplicates the default string to avoid importing
        the engine module — pin the pair so they can never drift."""
        monkeypatch.delenv('OPENCLAW_BT_FILL_MODEL', raising=False)
        assert ub._default_fill_model() == 'same_close'
