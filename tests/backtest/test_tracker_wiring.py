"""Wiring tests for the D3b tracker layer inside ``_per_bar_simulate`` /
``run_backtest`` (six-repo Stream D).  Synthetic data only."""
from __future__ import annotations

import itertools
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from backtest.trackers import FinishedBacktest, InconsistentBacktest  # noqa: E402
from tests.backtest.test_intraday_exit import OPT_IN, _aux_frame_for_dataset, _run, _stub_cls  # noqa: E402
from tests.backtest.test_tracker_parity_golden import _dataset, _stub  # noqa: E402

_ENV_KEYS = ('OPENCLAW_BT_MAX_RUNTIME_S', 'OPENCLAW_BT_CONSISTENCY_ABORT')


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _jumping_clock(step=10.0):
    c = itertools.count()
    return lambda: next(c) * step


def _sim(cls=None, bars=None, regimes=None):
    close_wide, b, r, dates = _dataset()
    cls = cls or _stub()
    with patch('strategies.aux_data_loader.load_aux_data', return_value={'options': {}}):
        return ub._per_bar_simulate(cls(), close_wide, bars or b, r if regimes is None else regimes,
                                    dates[0], dates[-1], strategy_id='stub_fm',
                                    fill_model='same_close', max_hold_days=21)


def _run_bt(cls=None, bars=None, sqls=None, clock=None):
    """run_backtest with a spy connection; SQL statements land in ``sqls``."""
    close_wide, b, regimes, dates = _dataset()
    sqls = [] if sqls is None else sqls
    cur = MagicMock()
    cur.execute.side_effect = lambda sql, params=None: sqls.append(str(sql))
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    with ExitStack() as st:
        st.enter_context(patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off',
                                                 'OPENCLAW_BT_SPREAD_COSTS': '0',
                                                 'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}))
        for p in (
            patch('backtest.unified_backtest.load_prices_panels', return_value=(close_wide, bars or b)),
            patch('backtest.unified_backtest.load_regimes', return_value=regimes),
            patch('backtest.unified_backtest.load_strategy_class', return_value=cls or _stub()),
            patch('backtest.unified_backtest.find_strategy_file',
                  return_value=str(ROOT / 'src/strategies/implementations/momentum_12_1.py')),
            patch('backtest.unified_backtest._code_sha', return_value='abc123'),
            patch('backtest.unified_backtest.psycopg2.extras.execute_values'),
            patch('strategies.aux_data_loader.load_aux_data', return_value={'options': {}}),
        ):
            st.enter_context(p)
        if clock is not None:
            st.enter_context(patch('backtest.trackers.time.monotonic', clock))
        ub.run_backtest('stub_fm', conn=conn, commit=False, fill_model='same_close', max_hold_days=21)
    return sqls


def test_sim_returns_info_with_census_and_counters():
    _, sim = _run(_stub_cls(OPT_IN), None)
    info = sim['info']
    assert 'exit_reasons' in info and 'hook_exits' in info
    assert info['intraday_no_bars'] > 0 and sim['intraday_no_bars'] == info['intraday_no_bars']
    for k in ('hook_exits', 'hook_raised', 'first_hook_raise', 'intraday_trades',
              'intraday_missing_entry_bar'):
        assert sim[k] == info[k]


def test_bt_trackers_line_emitted_once(capsys):
    _run_bt()
    out = capsys.readouterr().out
    assert out.count('[bt_trackers]') == 1
    line = next(l for l in out.splitlines() if '[bt_trackers]' in l)
    assert line.startswith('[unified_backtest] [bt_trackers] trackers=exit_census,hook,intraday_bars bars=40 ')
    assert 'max_runtime_s=0 consistency=off' in line


def test_unset_flags_build_only_three_trackers(capsys):
    info = _sim()['info']
    assert 'elapsed_s' not in info and 'consistency_checked' not in info
    assert 'trackers=exit_census,hook,intraday_bars ' in capsys.readouterr().out


def test_max_runtime_abort_persists_nothing_and_cli_returns_1(monkeypatch, capsys):
    monkeypatch.setenv('OPENCLAW_BT_MAX_RUNTIME_S', '1')
    sqls: list = []
    with pytest.raises(FinishedBacktest, match='max_runtime'):
        _run_bt(sqls=sqls, clock=_jumping_clock())
    assert not any('INSERT INTO strategy_backtest_runs' in q for q in sqls)

    capsys.readouterr()
    monkeypatch.setattr(sys, 'argv', ['x', '--strategy-id', 'stub_fm'])
    with patch.object(ub, 'run_backtest', side_effect=FinishedBacktest('max_runtime: elapsed 11.0s')), \
            patch.object(ub, '_resolve_instrument_class', return_value='equity'):
        assert ub.main() == 1
    assert 'FAIL: FinishedBacktest' in capsys.readouterr().out


def test_budget_fires_even_when_every_bar_continues(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_MAX_RUNTIME_S', '1')
    close_wide, b, regimes, dates = _dataset()
    nan_regimes = pd.Series({d: np.nan for d in dates})
    with patch('backtest.trackers.time.monotonic', _jumping_clock()):
        with pytest.raises(FinishedBacktest, match='max_runtime'):
            _sim(regimes=nan_regimes)


def _nan_close_bars():
    close_wide, b, regimes, dates = _dataset()
    df = b['ZZTEST1'].copy()
    df.iloc[12, df.columns.get_loc('close')] = np.nan
    return {'ZZTEST1': df}


def test_consistency_off_by_default_nan_mark_run_completes():
    sim = _sim(cls=_stub(wide=True), bars=_nan_close_bars())
    assert 'consistency_checked' not in sim['info']


def test_consistency_on_aborts_nan_mark_run(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BT_CONSISTENCY_ABORT', '1')
    with pytest.raises(InconsistentBacktest, match='nonfinite'):
        _sim(cls=_stub(wide=True), bars=_nan_close_bars())
