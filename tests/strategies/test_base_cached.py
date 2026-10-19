"""BaseStrategy.cached prefix-slice cache (six-repo D2a). Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.base import BaseStrategy  # noqa: E402


class _S(BaseStrategy):
    id = 'cache_stub'

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return []


def _panel():
    idx = pd.bdate_range('2023-01-02', periods=300)
    rng = np.random.default_rng(7)
    df = pd.DataFrame(100 + rng.normal(0, 1, (300, 5)).cumsum(axis=0),
                      index=idx, columns=[f'ZZTEST{i}' for i in range(1, 6)])
    df.iloc[40:45, 1] = np.nan
    df.iloc[:100, 4] = np.nan          # late listing
    return df


class _Fn:
    def __init__(self, f=lambda p: p.rolling(20).mean()):
        self.f, self.calls = f, 0

    def __call__(self, p):
        self.calls += 1
        return self.f(p)


@pytest.fixture
def panel():
    return _panel()


def _armed(panel):
    s = _S()
    s._cache_source = panel
    return s


def test_no_source_is_plain_call(panel):
    s, fn = _S(), _Fn()
    out = s.cached('sma', fn, panel.iloc[:50])
    pd.testing.assert_frame_equal(out, fn.f(panel.iloc[:50]), check_exact=True)
    assert s._bt_cache_stats['bypass'] == 1 and s._bt_cache_stats['miss'] == 0


def test_prefix_slices_equal_fresh_compute_and_fn_called_once(panel):
    s, fn = _armed(panel), _Fn()
    for n in (30, 60, 101, 150, 300):
        pre = panel.iloc[:n]
        pd.testing.assert_frame_equal(s.cached('sma', fn, pre), fn.f(pre), check_exact=True)
    assert fn.calls == 1
    assert s._bt_cache_stats['miss'] == 1 and s._bt_cache_stats['hit'] == 4


def test_column_subset_key_and_equality(panel):
    s, fn = _armed(panel), _Fn()
    sub = panel.iloc[:80][['ZZTEST1', 'ZZTEST3']]
    pd.testing.assert_frame_equal(s.cached('sma', fn, sub), fn.f(sub), check_exact=True)
    s.cached('sma', fn, panel.iloc[:90][['ZZTEST1', 'ZZTEST3']])
    assert s._bt_cache_stats['miss'] == 1 and s._bt_cache_stats['hit'] == 1
    assert ('sma', ('ZZTEST1', 'ZZTEST3')) in s._bt_cache


@pytest.mark.parametrize('tf', [lambda f: f.ffill(), np.log])
def test_transformed_frame_same_shape_bypasses(panel, tf):
    s, fn = _armed(panel), _Fn()
    frame = tf(panel.loc[:panel.index[120]])
    out = s.cached('sma', fn, frame)
    pd.testing.assert_frame_equal(out, fn.f(frame), check_exact=True)
    assert s._bt_cache_stats['incompatible'] == 1 and s._bt_cache_stats['miss'] == 0


def test_non_prefix_index_bypasses(panel):
    s, fn = _armed(panel), _Fn()
    frame = panel.iloc[50:120]
    pd.testing.assert_frame_equal(s.cached('sma', fn, frame), fn.f(frame), check_exact=True)
    assert s._bt_cache_stats['incompatible'] == 1


def test_mutating_returned_slice_does_not_poison_cache(panel):
    s, fn = _armed(panel), _Fn()
    a = s.cached('sma', fn, panel.iloc[:60])
    a.iloc[:] = -1.0
    b = s.cached('sma', fn, panel.iloc[:60])
    pd.testing.assert_frame_equal(b, fn.f(panel.iloc[:60]), check_exact=True)


def test_ndarray_result_is_uncacheable_and_correct(panel):
    s, fn = _armed(panel), _Fn(lambda p: p.to_numpy())
    out = s.cached('arr', fn, panel.iloc[:40])
    assert isinstance(out, np.ndarray) and out.shape == (40, 5)
    assert s._bt_cache_stats['uncacheable'] == 1 and not s._bt_cache


def test_universe_change_counts_recompute(panel):
    s, fn = _armed(panel), _Fn()
    s.cached('sma', fn, panel.iloc[:60][['ZZTEST1', 'ZZTEST2']])
    s.cached('sma', fn, panel.iloc[:61][['ZZTEST1', 'ZZTEST2', 'ZZTEST3']])
    assert s._bt_cache_stats['recompute'] == 1 and s._bt_cache_stats['miss'] == 2
