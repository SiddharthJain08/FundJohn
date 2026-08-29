import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.implementations.S_beta_spy import BetaSpy, STRATEGY_ID, HOLD_DAYS  # noqa: E402


def _panel(n=30, spy=True):
    idx = pd.bdate_range('2026-01-02', periods=n)
    cols = {'ZZTA': np.linspace(50, 60, n)}
    if spy:
        cols['SPY'] = np.linspace(500, 520, n)
    return pd.DataFrame(cols, index=idx)


def test_contract():
    assert BetaSpy.id == STRATEGY_ID == 'S_beta_spy'
    assert BetaSpy.benchmark_sleeve is True
    assert BetaSpy.signal_frequency == 'daily'
    assert set(BetaSpy.active_in_regimes) == {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}


def test_one_long_spy_per_bar_every_regime():
    s = BetaSpy()
    for R in ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'):
        out = s.generate_signals(_panel(), {'state': R}, ['SPY', 'ZZTA'])
        assert len(out) == 1
        sig = out[0]
        assert (sig.ticker, sig.direction) == ('SPY', 'LONG')
        assert sig.entry_price == 520.0
        assert sig.signal_params['hold_days'] == HOLD_DAYS == 21
        assert sig.signal_params['benchmark_sleeve'] is True
        # levels never bind: stop far below, targets far above
        assert sig.stop_loss < 0.7 * sig.entry_price
        assert sig.target_1 > 3.0 * sig.entry_price


def test_no_spy_column_or_bad_price_is_silent():
    s = BetaSpy()
    assert s.generate_signals(_panel(spy=False), {'state': 'LOW_VOL'}, ['ZZTA']) == []
    p = _panel(); p.loc[p.index[-1], 'SPY'] = np.nan
    assert s.generate_signals(p, {'state': 'LOW_VOL'}, ['SPY']) == []
    assert s.generate_signals(pd.DataFrame(), {'state': 'LOW_VOL'}, ['SPY']) == []


def test_non_numeric_spy_close_logs_debug_message(capsys):
    s = BetaSpy()
    idx = pd.bdate_range('2026-01-02', periods=30)
    cols = {'ZZTA': np.linspace(50, 60, 30), 'SPY': np.linspace(500, 520, 29).tolist() + ['n/a']}
    p = pd.DataFrame(cols, index=idx)
    out = s.generate_signals(p, {'state': 'LOW_VOL'}, ['SPY', 'ZZTA'])
    assert out == []
    captured = capsys.readouterr()
    assert 'not numeric' in captured.err
