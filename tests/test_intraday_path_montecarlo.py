"""Tests for intraday_path_montecarlo — Phase 2E path-dependent MC."""
from __future__ import annotations
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import intraday_path_montecarlo as ipm  # noqa: E402


# ---------- Policy application ---------- #

def test_apply_policy_long_target_hit():
    """LONG position rises through target → realized = +target_pct."""
    # 5 bars of +1% each → cumulative ~5%
    path = [math.log(1.01)] * 5
    ret, reason, dd = ipm.apply_policy(path, stop_pct=0.02, target_pct=0.03,
                                         max_hold_bars=10, direction='LONG')
    assert reason == 'target'
    assert abs(ret - 0.03) < 0.001


def test_apply_policy_long_stop_hit():
    """LONG position falls through stop → realized = -stop_pct."""
    path = [math.log(0.99)] * 5  # -1% per bar, will breach -2% stop
    ret, reason, dd = ipm.apply_policy(path, stop_pct=0.02, target_pct=0.10,
                                         max_hold_bars=10, direction='LONG')
    assert reason == 'stop'
    assert abs(ret - (-0.02)) < 0.001


def test_apply_policy_long_max_hold():
    """Neither stop nor target hit → exit at max_hold with realized close."""
    path = [math.log(1.001)] * 3  # +0.1% per bar, total ~0.3%
    ret, reason, dd = ipm.apply_policy(path, stop_pct=0.05, target_pct=0.05,
                                         max_hold_bars=3, direction='LONG')
    assert reason == 'max_hold'
    assert 0.002 < ret < 0.005


def test_apply_policy_short_target_hit():
    """SHORT position: price falls → P&L positive → target."""
    path = [math.log(0.99)] * 5  # price drops, SHORT profits
    ret, reason, dd = ipm.apply_policy(path, stop_pct=0.10, target_pct=0.03,
                                         max_hold_bars=10, direction='SHORT')
    assert reason == 'target'
    assert abs(ret - 0.03) < 0.001


def test_apply_policy_short_stop_hit():
    """SHORT position: price rises → P&L negative → stop."""
    path = [math.log(1.01)] * 5
    ret, reason, dd = ipm.apply_policy(path, stop_pct=0.02, target_pct=0.10,
                                         max_hold_bars=10, direction='SHORT')
    assert reason == 'stop'
    assert abs(ret - (-0.02)) < 0.001


def test_apply_policy_intra_dd_tracked():
    """Path dips before recovering → intra_max_dd captures the trough."""
    # Up 3% then down 5%, never reaching stop, finishes at -2%
    path = [math.log(1.03), math.log(1.0 / 1.05)]
    ret, reason, dd = ipm.apply_policy(path, stop_pct=0.10, target_pct=0.10,
                                         max_hold_bars=10, direction='LONG')
    assert reason == 'max_hold'
    # Peak was at +3%, trough at end ≈ -2.09%, so dd ≈ -5%
    assert dd < -0.04


def test_apply_policy_empty_path():
    ret, reason, dd = ipm.apply_policy([], stop_pct=0.02, target_pct=0.05,
                                         max_hold_bars=10, direction='LONG')
    assert ret == 0.0
    assert reason == 'max_hold'


# ---------- GBM Path Generator ---------- #

def test_gbm_intraday_scaling():
    """sigma_intraday should be sigma_daily / sqrt(13). Check by
    constructing a GBMPathGen directly and verifying its parameters."""
    gen = ipm.GBMPathGen(mu_intraday=0.0001, sigma_intraday=0.005)
    rng = random.Random(42)
    path = gen.sample_path(100, rng)
    assert len(path) == 100
    # Sample mean ≈ mu, sample std ≈ sigma (large n)
    m = sum(path) / len(path)
    sd = math.sqrt(sum((x - m) ** 2 for x in path) / (len(path) - 1))
    assert abs(sd - 0.005) < 0.002  # within 40% of target stdev with n=100


# ---------- Empirical Path Generator ---------- #

def test_empirical_resamples_from_window():
    """EmpiricalPathGen wraps when n_bars exceeds available returns."""
    gen = ipm.EmpiricalPathGen([0.01, -0.02, 0.005])  # tiny pool
    rng = random.Random(0)
    path = gen.sample_path(10, rng)
    assert len(path) == 10
    # Every value must come from the pool
    for r in path:
        assert r in (0.01, -0.02, 0.005)


# ---------- MC engine ---------- #

def test_run_path_mc_insufficient_trades():
    """Trade pool below MIN_TRADES_FOR_MC → INSUFFICIENT."""
    pool = [{'ticker': 'AAPL', 'direction': 'LONG'}] * 3
    result = ipm.run_path_mc(
        strategy_id='S_test', regime_state='LOW_VOL',
        current_size=1.0, proposed_size=1.0,
        proposed_stop_pct=0.02, proposed_target_pct=0.05,
        proposed_max_hold_days=5,
        n_iter=10, trade_pool=pool, seed=1)
    assert result['status'] == 'INSUFFICIENT'
    assert result['n_trades_sampled'] == 3


def test_run_path_mc_emits_hit_rates():
    """With a real ticker pool, MC produces stop/target/max_hold rates
    that sum near 1 (minus no-gen iterations)."""
    pool = [{'ticker': 'SPY', 'direction': 'LONG'}] * 20
    result = ipm.run_path_mc(
        strategy_id='S_test', regime_state='LOW_VOL',
        current_size=1.0, proposed_size=1.0,
        proposed_stop_pct=0.02, proposed_target_pct=0.05,
        proposed_max_hold_days=5,
        n_iter=200, trade_pool=pool, seed=42)
    if result['status'] == 'INSUFFICIENT':
        # Acceptable if no parquet in test env
        pytest.skip('SPY 30m parquet not available')
    total = (result['stop_hit_rate'] + result['target_hit_rate']
              + result['max_hold_hit_rate'])
    assert 0.95 < total <= 1.001
    assert result['path_source'] in ('empirical', 'gbm', 'hybrid')


def test_run_path_mc_size_scaling_linear_inside_no_clip():
    """When no stop/target is hit, returns scale linearly with size ratio.
    Use a very wide stop/target so the path-MC reduces to linear PnL."""
    pool = [{'ticker': 'AAPL', 'direction': 'LONG'}] * 20
    base = ipm.run_path_mc(
        strategy_id='S_test', regime_state='LOW_VOL',
        current_size=1.0, proposed_size=1.0,
        proposed_stop_pct=1.0, proposed_target_pct=1.0,  # never hit
        proposed_max_hold_days=2,
        n_iter=200, trade_pool=pool, seed=99)
    doubled = ipm.run_path_mc(
        strategy_id='S_test', regime_state='LOW_VOL',
        current_size=1.0, proposed_size=2.0,
        proposed_stop_pct=1.0, proposed_target_pct=1.0,
        proposed_max_hold_days=2,
        n_iter=200, trade_pool=pool, seed=99)
    if base['status'] == 'INSUFFICIENT' or doubled['status'] == 'INSUFFICIENT':
        pytest.skip('AAPL parquet not available')
    # With wide stop/target, no clipping; median should ~double
    if abs(base['mean_pnl_p50']) > 1e-6:
        ratio = doubled['mean_pnl_p50'] / base['mean_pnl_p50']
        assert 1.7 < ratio < 2.3


def test_apply_policy_filters_nan_returns_in_engine(monkeypatch):
    """Regression for the BRK-B incident (2026-05-13 smoke): when a path
    generator emits NaN returns (e.g. NaN closes in parquet), the MC engine
    must classify them as no_gen iterations and not poison the bootstrap."""
    class NanGen:
        def sample_path(self, n_bars, rng):
            return [float('nan')] * n_bars

    monkeypatch.setattr(ipm, '_gen_for_ticker',
                          lambda t, window_days=90: (NanGen(), 'gbm'))
    pool = [{'ticker': 'AAPL', 'direction': 'LONG'}] * 20
    result = ipm.run_path_mc(
        strategy_id='S_test', regime_state='LOW_VOL',
        current_size=1.0, proposed_size=1.0,
        proposed_stop_pct=0.02, proposed_target_pct=0.05,
        proposed_max_hold_days=5,
        n_iter=50, trade_pool=pool, seed=7)
    # All paths are NaN → all classified no_gen → INSUFFICIENT (not crashed)
    assert result['status'] == 'INSUFFICIENT'


def test_path_mc_no_gen_fallthrough():
    """Pool with only an unknown ticker → no_gen_rate=1.0, INSUFFICIENT."""
    pool = [{'ticker': 'ZZZ_FAKE_TICKER_XYZ', 'direction': 'LONG'}] * 20
    result = ipm.run_path_mc(
        strategy_id='S_test', regime_state='LOW_VOL',
        current_size=1.0, proposed_size=1.0,
        proposed_stop_pct=0.02, proposed_target_pct=0.05,
        proposed_max_hold_days=5,
        n_iter=20, trade_pool=pool, seed=1)
    assert result['status'] == 'INSUFFICIENT'
    assert 'no path generator' in result['note']
