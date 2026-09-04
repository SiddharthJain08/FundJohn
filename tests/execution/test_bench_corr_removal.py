"""Benchmark-correlation removal (operator directive 2026-09-04).

With the beta budget holding the market through SPY, an alpha name highly
correlated with the benchmark is redundant beta, not diversification — and
benchmark tickers are excluded from the correlation clustering (spec
2026-08-29 D6), so the 20% cluster budget can no longer see that redundancy.
The sizer removes such names outright: held names orphan-close downstream,
conviction is NOT redirected. Pure unit tests — no DB, no price panel.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import asset_correlation as ac  # noqa: E402
from execution import benchmark_sizing as bsz  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


# --- pure removal ------------------------------------------------------------

def test_removes_only_names_at_or_above_threshold():
    targets = {'SPY': 70000.0, 'AAPL': 5000.0, 'GLD': 4000.0, 'XLK': -3000.0}
    corr = {'AAPL': 0.82, 'GLD': 0.10, 'XLK': -0.95}
    kept, removed = bsz.remove_benchmark_correlated(targets, 'SPY', corr, 0.60)
    assert set(removed) == {'AAPL', 'XLK'}          # |rho| >= thr, sign-blind
    assert kept == {'SPY': 70000.0, 'GLD': 4000.0}


def test_benchmark_itself_and_unknown_corr_never_removed():
    targets = {'SPY': 70000.0, 'NEWIPO': 2000.0}
    kept, removed = bsz.remove_benchmark_correlated(targets, 'SPY', {}, 0.60)
    assert removed == {} and kept == targets        # absent from corr = kept


def test_zero_target_is_not_a_removal_subject():
    kept, removed = bsz.remove_benchmark_correlated(
        {'AAPL': 0.0}, 'SPY', {'AAPL': 0.99}, 0.60)
    assert removed == {} and kept == {'AAPL': 0.0}


def test_inputs_not_mutated():
    targets = {'SPY': 1.0, 'AAPL': 2.0}
    bsz.remove_benchmark_correlated(targets, 'SPY', {'AAPL': 0.9}, 0.60)
    assert targets == {'SPY': 1.0, 'AAPL': 2.0}


# --- benchmark_corr: one row, thin-evidence floor ----------------------------

def test_benchmark_corr_is_one_row_with_min_obs_floor(monkeypatch):
    dates = [f'2026-08-{d:02d}' for d in range(1, 29)]
    up = {d: 0.01 * ((i % 3) - 1) for i, d in enumerate(dates)}
    inv = {d: -v for d, v in up.items()}
    thin = {dates[0]: 0.01, dates[1]: -0.01}        # < MIN_OBS overlap -> 0.0
    monkeypatch.setattr(ac, '_load_returns', lambda tickers, window, as_of=None: {
        'SPY': dict(up), 'CLONE': dict(up), 'INV': inv, 'THIN': thin})
    rho = ac.benchmark_corr(['CLONE', 'INV', 'THIN', 'SPY'], 'SPY')
    assert rho['CLONE'] > 0.99 and rho['INV'] < -0.99
    assert rho['THIN'] == 0.0
    assert 'SPY' not in rho


def test_benchmark_corr_fails_open_empty(monkeypatch):
    monkeypatch.setattr(ac, '_load_returns',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('panel down')))
    assert ac.benchmark_corr(['AAPL'], 'SPY') == {}
    monkeypatch.setattr(ac, '_load_returns', lambda *a, **k: {'AAPL': {'d': 0.1}})
    assert ac.benchmark_corr(['AAPL'], 'SPY') == {}   # bench has no returns


# --- sizer wiring ------------------------------------------------------------

def _wire(monkeypatch, *, corr, thr=0.60, vetoed=frozenset()):
    monkeypatch.delenv('OPENCLAW_BENCH_CORR_REMOVAL', raising=False)
    monkeypatch.setattr(rbs, '_load_premarket_vetoes', lambda: set(vetoed))
    import execution.benchmark_sizing as bmod
    import execution.asset_correlation as amod
    monkeypatch.setattr(bmod, 'bench_corr_removal_thr', lambda default=0.60, conn=None: thr)
    monkeypatch.setattr(amod, 'benchmark_corr',
                        lambda tickers, bench, window=63, as_of=None: dict(corr))


def test_wiring_removes_correlated_alpha(monkeypatch):
    _wire(monkeypatch, corr={'AAPL': 0.85, 'GLD': 0.05})
    out = rbs._apply_benchmark_corr_removal(
        {'SPY': 70000.0, 'AAPL': 5000.0, 'GLD': 4000.0}, {'SPY'}, 100000.0)
    assert out == {'SPY': 70000.0, 'GLD': 4000.0}


def test_wiring_noop_without_benchmark_target(monkeypatch):
    _wire(monkeypatch, corr={'AAPL': 0.99})
    targets = {'AAPL': 5000.0}
    assert rbs._apply_benchmark_corr_removal(targets, {'SPY'}, 100000.0) == targets
    assert rbs._apply_benchmark_corr_removal(targets, set(), 100000.0) == targets


def test_wiring_kept_on_premarket_veto_day(monkeypatch):
    _wire(monkeypatch, corr={'AAPL': 0.99}, vetoed={'SPY'})
    targets = {'SPY': 70000.0, 'AAPL': 5000.0}
    assert rbs._apply_benchmark_corr_removal(targets, {'SPY'}, 100000.0) == targets


def test_wiring_thr_outside_unit_interval_disables(monkeypatch):
    _wire(monkeypatch, corr={'AAPL': 0.99}, thr=0.0)
    targets = {'SPY': 70000.0, 'AAPL': 5000.0}
    assert rbs._apply_benchmark_corr_removal(targets, {'SPY'}, 100000.0) == targets


def test_wiring_env_kill_switch(monkeypatch):
    _wire(monkeypatch, corr={'AAPL': 0.99})
    monkeypatch.setenv('OPENCLAW_BENCH_CORR_REMOVAL', '0')
    targets = {'SPY': 70000.0, 'AAPL': 5000.0}
    assert rbs._apply_benchmark_corr_removal(targets, {'SPY'}, 100000.0) == targets


def test_wiring_fails_open_on_error(monkeypatch):
    _wire(monkeypatch, corr={})
    import execution.asset_correlation as amod
    monkeypatch.setattr(amod, 'benchmark_corr',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    targets = {'SPY': 70000.0, 'AAPL': 5000.0}
    assert rbs._apply_benchmark_corr_removal(targets, {'SPY'}, 100000.0) == targets
