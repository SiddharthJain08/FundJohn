"""IndicatorCacheTracker wiring in _per_bar_simulate (six-repo D2a)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES  # noqa: E402
from tests.backtest.test_tracker_parity_golden import _dataset  # noqa: E402


def _cls():
    class CachedStub(BaseStrategy):
        id = 'stub_fm'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or len(prices) % 5 or not universe:
                return []
            sma = self.cached('sma5', lambda p: p.rolling(5).mean(), prices)
            t = universe[0]
            ep = float(prices[t].iloc[-1])
            lift = 1.0 if float(sma[t].iloc[-1]) < ep else 0.0
            return [Signal(ticker=t, direction='LONG', entry_price=ep,
                           stop_loss=ep * 0.985, target_1=ep * (1.02 + 0.001 * lift),
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED', signal_params={})]
    return CachedStub


def _sim(monkeypatch, flag):
    if flag:
        monkeypatch.setenv('OPENCLAW_BT_INDICATOR_CACHE', '1')
    else:
        monkeypatch.delenv('OPENCLAW_BT_INDICATOR_CACHE', raising=False)
    close_wide, bars, regimes, dates = _dataset()
    inst = _cls()()
    with patch('strategies.aux_data_loader.load_aux_data', return_value={'options': {}}):
        out = ub._per_bar_simulate(inst, close_wide, bars, regimes, dates[0], dates[-1],
                                   strategy_id='stub_fm', fill_model='same_close',
                                   max_hold_days=21)
    return inst, out


def _trades(out):
    return out['trades'] if isinstance(out, dict) else out[0]


def test_flag_off_no_source_and_no_line(monkeypatch, capsys):
    inst, out = _sim(monkeypatch, False)
    assert '[bt_cache]' not in capsys.readouterr().out
    assert getattr(inst, '_cache_source', None) is None
    st = inst._bt_cache_stats
    assert st['bypass'] > 0 and st['hit'] == st['miss'] == 0


def test_flag_on_sets_source_trades_identical_and_line_once(monkeypatch, capsys):
    _, off = _sim(monkeypatch, False)
    capsys.readouterr()
    inst, on = _sim(monkeypatch, True)
    txt = capsys.readouterr().out
    assert txt.count('[bt_cache] on hits=') == 1
    assert _trades(on) and _trades(on) == _trades(off)
    assert inst._bt_cache_stats['miss'] == 1 and inst._bt_cache_stats['hit'] > 0
    assert on['info']['indicator_cache']['miss'] == 1


def test_source_cleared_after_run(monkeypatch):
    inst, _ = _sim(monkeypatch, True)
    assert inst._cache_source is None and not inst.__dict__.get('_bt_cache')
