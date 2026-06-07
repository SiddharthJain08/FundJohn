"""Tests for the fill_model kwarg added to unified_backtest (SP-6).

Test plan:
  a. REGRESSION: default kwarg == explicit fill_model="close" — trades identical.
  b. Open-fill entry: fill_model="open" → entry price = open[t+1]; bracket re-anchored.
  c. Fill-bar exit eligibility: low breaches re-anchored stop on fill bar under 'open';
     under 'close' exits later.
  d. ValueError on fill_model="banana".
  e. Driver unit: --single monkeypatched; --summarize gates + verdict strings.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
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


# ── (a) REGRESSION: default == explicit "close" ───────────────────────────────

class TestRegression:
    """Default call must produce identical trades to explicit fill_model='close'."""

    def test_default_equals_explicit_close(self):
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        trades_default = _run_capture(stub, close_wide, bars, regimes)
        trades_explicit = _run_capture(stub, close_wide, bars, regimes,
                                       fill_model='close')
        assert trades_default is not None
        assert trades_explicit is not None
        assert len(trades_default) == len(trades_explicit), \
            'trade count must match between default and explicit close'
        for td, te in zip(trades_default, trades_explicit):
            assert td['entry_price'] == te['entry_price'], \
                'entry_price must be identical'
            assert td['exit_date'] == te['exit_date'], \
                'exit_date must be identical'
            assert td['exit_reason'] == te['exit_reason'], \
                'exit_reason must be identical'
            assert td['pnl_pct'] == te['pnl_pct'], \
                'pnl_pct must be identical'

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
            # Patch within driver module namespace.
            with patch.object(
                sys.modules.get('backtest_fill_model_study', driver),
                '_make_mock_conn', return_value=_make_mock_conn(),
            ):
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

        # Build a synthetic results.jsonl where all strategies have high ΔSharpe.
        fake_rows = []
        for i in range(10):
            fake_rows.append({
                'sid': f'S_{i:03d}',
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
            # Patch the module-level paths.
            with patch.object(driver, 'RESULTS_FILE', rf), \
                 patch.object(driver, 'REPORT_FILE', rep_f), \
                 patch.object(driver, 'RESULTS_DIR', Path(td)):
                verdict = driver._summarize()
        assert verdict == 'CONSIDERATION-BAR-MET'

    def test_summarize_close_fill_stands_low_delta(self):
        """With ΔSharpe < threshold → CLOSE-FILL-STANDS."""
        driver = self._import_driver()

        fake_rows = []
        for i in range(10):
            fake_rows.append({
                'sid': f'S_{i:03d}',
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
                 patch.object(driver, 'RESULTS_DIR', Path(td)):
                verdict = driver._summarize()
        assert verdict == 'CLOSE-FILL-STANDS'

    def test_summarize_invalid_sim_too_many_suspects(self):
        """More than 5 SIM-SUSPECT strategies → INVALID-SIM."""
        driver = self._import_driver()

        fake_rows = []
        for i in range(8):
            fake_rows.append({
                'sid': f'S_{i:03d}',
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
                 patch.object(driver, 'RESULTS_DIR', Path(td)):
                verdict = driver._summarize()
        assert verdict == 'INVALID-SIM'
