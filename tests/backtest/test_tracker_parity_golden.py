"""Characterization (golden) test for the D3b tracker refactor (six-repo Stream D).

``_capture(scenario)`` runs ``run_backtest`` on synthetic bars with a spy
connection and records the exact ``config_json`` string, the trade rows handed
to ``execute_values`` and the run-row INSERT params.  The fixture was recorded
on the PRE-refactor engine; the refactor must reproduce it byte for byte.
``RECORD_GOLDEN=1`` rewrites the fixture instead of asserting.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES  # noqa: E402
from tests.backtest.test_intraday_exit import _frame  # noqa: E402

FIXTURE = Path(__file__).parent / 'fixtures' / 'tracker_parity_golden.json'
TICKER = 'ZZTEST1'
N = 40
SCENARIOS = ['plain', 'hook_exits', 'hook_raised', 'intraday_ok', 'intraday_no_bars',
             'intraday_sim_none', 'intraday_missing_entry_bar', 'event_gated',
             'asset_gated', 'options_dispatch']


def _dataset():
    dates = pd.date_range('2024-01-01', periods=N, freq='B')
    closes = [100.0 + 4.0 * ((i % 7) - 3) / 3.0 + 0.2 * i for i in range(N)]
    close_wide = pd.DataFrame({TICKER: closes}, index=dates)
    close_wide.index.name = 'date'
    bars = pd.DataFrame({'open': [c - 0.3 for c in closes], 'high': [c + 1.1 for c in closes],
                         'low': [c - 1.1 for c in closes], 'close': closes},
                        index=pd.DatetimeIndex(dates, name='date'))
    regimes = pd.Series({d: 'LOW_VOL' for d in dates})
    return close_wide, {TICKER: bars}, regimes, dates


def _stub(signal_params=None, hook=None, wide=False):
    class Stub(BaseStrategy):
        id = 'stub_fm'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or len(prices) % 5 or not universe:
                return []
            ep = float(prices[universe[0]].iloc[-1])
            return [Signal(ticker=universe[0], direction='LONG', entry_price=ep,
                           stop_loss=ep * (0.93 if wide else 0.985),
                           target_1=ep * (1.15 if wide else 1.02),
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED', signal_params=dict(signal_params or {}))]

    if hook is not None:
        Stub.exit_hook = True
        Stub.should_exit = lambda self, position, prices, regime, aux_data=None: hook(position)
    return Stub


def _aux30(dates):
    return pd.concat([_frame(session=d.strftime('%Y-%m-%d'), ticker=TICKER,
                             overrides={'11:00': (100.0, 103.0, 99.9, 102.0)}) for d in dates],
                     ignore_index=True)


def _boom(position):
    raise RuntimeError('kaboom')


OPT_IN = {'exit_on': 'session_close', 'entry_bar_et': '10:00'}


def _capture(scenario: str) -> dict:
    close_wide, bars, regimes, dates = _dataset()
    cls = _stub()
    aux = {'options': {}}
    env = {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0',
           'OPENCLAW_BACKTEST_COUPLED_RECS': '0',
           # The golden fixture was recorded without a macro master: pin the
           # rf shadow to the const fallback so the real DGS3MO series on the
           # production checkout cannot change config_json['rf']['macro'].
           'OPENCLAW_RF_SOURCE': 'const',
           'OPENCLAW_MACRO_PARQUET': '/nonexistent/golden-no-macro.parquet'}
    extra = []
    kw = {}
    if scenario == 'hook_exits':
        cls = _stub(hook=lambda p: 'z' if p['days_held'] == 2 else None, wide=True)
    elif scenario == 'hook_raised':
        cls = _stub(hook=_boom, wide=True)
    elif scenario == 'intraday_ok':
        cls = _stub(OPT_IN); aux['prices_30m'] = _aux30(dates)
    elif scenario == 'intraday_no_bars':
        cls = _stub(OPT_IN)
    elif scenario == 'intraday_sim_none':
        cls = _stub(OPT_IN); aux['prices_30m'] = _aux30(dates)
        extra.append(patch('backtest.unified_backtest.simulate_intraday_trade', return_value=None))
    elif scenario == 'intraday_missing_entry_bar':
        cls = _stub({'exit_on': 'session_close'}); aux['prices_30m'] = _aux30(dates)
    elif scenario == 'event_gated':
        env['OPENCLAW_BT_EVENT_GATE'] = '1'
        extra.append(patch('lib.macro_events.gated_sessions',
                           return_value={dates[14].date(): ['CPI']}))
    elif scenario == 'asset_gated':
        env['OPENCLAW_BT_ASSET_GATE'] = 'parity'
        extra.append(patch('backtest.unified_backtest.load_bt_asset_gate',
                           return_value={TICKER: (False, True)}))
    elif scenario == 'options_dispatch':
        kw['instrument_class'] = 'option'
        opt_trades = [{'ticker': TICKER, 'direction': 'long', 'entry_date': dates[10].date(),
                       'entry_price': 5.0, 'exit_date': dates[14].date(), 'exit_price': 5.5,
                       'exit_reason': 'target', 'holding_days': 4, 'pnl_pct': 0.1,
                       'entry_regime': 'LOW_VOL', 'signal_stop': 4.5, 'signal_target': 5.5,
                       'daily_marks': []}]
        extra.append(patch('backtest.unified_backtest.options_backtest.simulate',
                           return_value={'trades': opt_trades, 'universe_sizes': [],
                                         'days_processed': 5, 'days_with_signals': 1,
                                         'static_universe': [TICKER], 'min_lookback': 5}))
    seen = {'run': None, 'trades': None}
    cur = MagicMock()

    def exec_spy(sql, params=None):
        if 'INSERT INTO strategy_backtest_runs' in str(sql):
            seen['run'] = params
    cur.execute.side_effect = exec_spy
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)

    def ev(c, sql, rows, *a, **k):
        if 'strategy_backtest_trades' in str(sql):
            seen['trades'] = [list(r) for r in rows]

    from contextlib import ExitStack
    with ExitStack() as st:
        st.enter_context(patch.dict(os.environ, env))
        for p in [
            patch('backtest.unified_backtest.load_prices_panels', return_value=(close_wide, bars)),
            patch('backtest.unified_backtest.load_regimes', return_value=regimes),
            patch('backtest.unified_backtest.load_strategy_class', return_value=cls),
            patch('backtest.unified_backtest.find_strategy_file',
                  return_value=str(ROOT / 'src/strategies/implementations/momentum_12_1.py')),
            patch('backtest.unified_backtest._code_sha', return_value='abc123'),
            patch('backtest.unified_backtest.psycopg2.extras.execute_values', side_effect=ev),
            patch('strategies.aux_data_loader.load_aux_data', return_value=aux),
        ] + extra:
            st.enter_context(p)
        ub.run_backtest('stub_fm', conn=conn, commit=False, fill_model='same_close',
                        max_hold_days=21, **kw)
    run = seen['run']
    cfg = next(p for p in run if isinstance(p, str) and p.startswith('{') and 'max_hold_days' in p)
    rows = [r[1:] for r in (seen['trades'] or [])]
    return {'config_json': cfg,
            'trade_rows': json.dumps(rows, default=str),
            'run_row': json.dumps([p for i, p in enumerate(run) if i not in (0, 2)], default=str)}


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_golden_matches(scenario):
    got = _capture(scenario)
    if os.environ.get('RECORD_GOLDEN') == '1':
        data = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
        data[scenario] = got
        FIXTURE.write_text(json.dumps(data, indent=1, sort_keys=True))
        return
    want = json.loads(FIXTURE.read_text())[scenario]
    for field in ('config_json', 'trade_rows', 'run_row'):
        assert got[field] == want[field], field


def test_scenarios_exercise_what_they_claim():
    """Guard against a vacuous fixture: counters/trades differ across scenarios."""
    want = json.loads(FIXTURE.read_text())
    cfg = {s: json.loads(want[s]['config_json']) for s in SCENARIOS}
    rows = {s: json.loads(want[s]['trade_rows']) for s in SCENARIOS}
    assert rows['plain']
    assert cfg['hook_exits']['hook_exits'] > 0
    assert cfg['hook_raised']['hook_raised'] > 0
    assert cfg['intraday_ok']['intraday_trades'] > 0
    assert cfg['intraday_no_bars']['intraday_no_bars'] > 0
    assert cfg['intraday_sim_none']['intraday_no_bars'] > 0
    assert cfg['intraday_missing_entry_bar']['intraday_missing_entry_bar'] > 0
    assert cfg['event_gated']['entries_event_gated'] > 0
    assert rows['event_gated'] != rows['plain']
    assert cfg['asset_gated']['asset_gate'] == 'parity' and not rows['asset_gated']
    assert rows['options_dispatch'] and cfg['options_dispatch']['intraday_exit'] == 'off'
