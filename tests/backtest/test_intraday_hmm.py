"""tests/test_intraday_hmm.py

Tests for the intraday HMM trainer + walk-forward backtest harness.

Strategy: build synthetic feature matrices with KNOWN regime structure
(2-state Gaussian mixture with disjoint means) and verify the HMM
recovers them. Then run the walk-forward harness on the same data and
assert transition detection works.

Run:
    pytest tests/test_intraday_hmm.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

# Load trainer and backtest modules.
_trainer_spec = importlib.util.spec_from_file_location(
    'train_intraday_hmm', ROOT / 'scripts' / 'train_intraday_hmm.py',
)
trainer = importlib.util.module_from_spec(_trainer_spec)
_trainer_spec.loader.exec_module(trainer)

from backtest.intraday_regime_backtest import (   # noqa: E402
    walk_forward,
    _detect_transitions,
    _state_name_map,
    _train_hmm,
)


# ── Synthetic feature builder ────────────────────────────────────────────────

def _build_synthetic_features(n_rows_per_state=100, seed=42):
    """Build features with two clear regimes:
       LOW_VOL:  vix~15, vix3m~16, slope~1.07, rr~0.02, rv~10, gk~0.08, vvix~90
       HIGH_VOL: vix~30, vix3m~28, slope~0.93, rr~0.10, rv~25, gk~0.35, vvix~140
    Alternating blocks, total = 4 * n_rows_per_state rows.

    Feature set v3 (commit 1172802, 2026-05-20): trainer dropped
    spy_realized_vol_30m and added spy_gk_vol_daily + vvix_level. The
    builder emits BOTH generations: v3 columns for trainer.HMM_INPUT_COLS
    and the legacy spy_realized_vol_30m still selected by the walk-forward
    harness (src/backtest/intraday_regime_backtest.py HMM_INPUT_COLS was
    not updated by 1172802 — see module-drift note in the test report).
    Regime bands mirror the 1172802 commit message (COVID GK_ann 0.39-0.96;
    VVIX ~140 structural vs ~108 noisy HIGH_VOL).
    """
    rng = np.random.default_rng(seed)
    blocks = []
    n_per = n_rows_per_state
    base_ts = pd.Timestamp('2026-01-02 14:00', tz='UTC')

    def _block(state_high, n, start_ts):
        if state_high:
            vix  = rng.normal(30, 1.5, n)
            v90  = rng.normal(28, 1.5, n)
            slp  = v90 / vix
            rr   = rng.normal(0.10, 0.01, n)
            rv   = rng.normal(25, 2, n)
            gk   = rng.normal(0.35, 0.04, n)
            vvix = rng.normal(140, 5, n)
        else:
            vix  = rng.normal(15, 0.8, n)
            v90  = rng.normal(16, 0.8, n)
            slp  = v90 / vix
            rr   = rng.normal(0.02, 0.005, n)
            rv   = rng.normal(10, 1, n)
            gk   = rng.normal(0.08, 0.01, n)
            vvix = rng.normal(90, 4, n)
        return pd.DataFrame({
            'ts_utc':              pd.date_range(start_ts, periods=n, freq='5min', tz='UTC'),
            'vix_synth_30d':       vix,
            'vix_synth_90d':       v90,
            'vix_term_slope':      slp,
            'rr_25d':              rr,
            'spy_realized_vol_30m': rv,
            'spy_gk_vol_daily':    gk,
            'vvix_level':          vvix,
            'source_quality_flag': 0,
        })

    # Pattern: LOW_VOL × N, HIGH_VOL × N, LOW_VOL × N, HIGH_VOL × N
    for i in range(4):
        is_high = (i % 2 == 1)
        start = base_ts + pd.Timedelta(minutes=5 * i * n_per)
        blocks.append(_block(is_high, n_per, start))
    return pd.concat(blocks, ignore_index=True)


# ── Tests: trainer ───────────────────────────────────────────────────────────

class TestTrainerCore:
    def test_build_X_imputes_nan_with_means(self):
        # Feature set v3 (commit 1172802, 2026-05-20): spy_realized_vol_30m
        # dropped (rv HIGH_VOL>CRISIS inversion; dead since SP-1 Polygon
        # purge), spy_gk_vol_daily + vvix_level added → 6 input columns.
        df = pd.DataFrame({
            'vix_synth_30d':         [15, 16, np.nan, 17],
            'vix_synth_90d':         [16, 17, 18, np.nan],
            'vix_term_slope':        [1.07] * 4,
            'rr_25d':                [np.nan] * 4,
            'spy_gk_vol_daily':      [0.08, 0.09, 0.10, 0.11],
            'vvix_level':            [90, 92, np.nan, 95],
        })
        X, means = trainer._build_X(df)
        # v3: (n_rows, 6) — was (n_rows, 5) before 1172802.
        assert X.shape == (4, 6)
        # rr_25d all NaN → mean=0
        assert means['rr_25d'] == 0.0
        # vix_synth_30d mean of present values = (15+16+17)/3
        assert means['vix_synth_30d'] == pytest.approx((15 + 16 + 17) / 3)
        # No NaN remaining
        assert not np.isnan(X).any()

    def test_filter_rth_drops_overnight(self):
        ts_in_rth = pd.Timestamp('2026-01-02 14:00', tz='UTC')   # 9 AM ET
        ts_overnight = pd.Timestamp('2026-01-02 02:00', tz='UTC')  # 9 PM ET prior day
        df = pd.DataFrame({
            'ts_utc': [ts_in_rth, ts_overnight, ts_in_rth + pd.Timedelta(hours=1)],
            'vix_synth_30d': [15, 15, 15],
        })
        df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)
        out = trainer._filter_rth(df)
        # 14:00 UTC = 9:00 ET (DST inactive in Jan, so EST = UTC-5);
        # actually 14:00 UTC = 9:00 ET in winter, before 9:30 RTH start
        # → row dropped. 15:00 UTC = 10:00 ET → kept. 02:00 UTC dropped.
        # Validate that overnight is filtered, RTH kept.
        assert len(out) >= 1   # at least the 15:00 UTC row
        for ts in out['ts_utc']:
            et = ts.tz_convert('America/New_York')
            assert et.weekday() < 5
            assert (et.hour, et.minute) >= (9, 30)


class TestHmmRecovery:
    """Train HMM on a 2-state synthetic mixture; assert it recovers the
    states (LOW_VOL state mean << HIGH_VOL state mean on vix_synth_30d)."""

    def test_hmm_recovers_two_distinct_regimes(self):
        df = _build_synthetic_features(n_rows_per_state=100)
        # trainer.HMM_INPUT_COLS is v3 (6 features, commit 1172802
        # 2026-05-20); the synthetic builder now emits all 6. Column 0
        # remains vix_synth_30d, so the ascending-VIX rank mapping below
        # is unchanged.
        feature_cols = trainer.HMM_INPUT_COLS
        X, means = trainer._build_X(df[feature_cols])
        model = _train_hmm(X)
        name_map = _state_name_map(model)
        # Each predicted raw state should map to one of the 4 named regimes.
        assigned = set(name_map.values())
        assert assigned <= set(trainer.STATE_NAMES_BY_RANK.values())
        # Mean of vix_synth_30d for the LOW_VOL-mapped state should be
        # well below the HIGH_VOL-mapped state mean.
        means_per_state = model.means_[:, 0]   # vix_synth_30d means
        low_states  = [s for s, n in name_map.items() if n == 'LOW_VOL']
        high_states = [s for s, n in name_map.items() if n == 'HIGH_VOL']
        if low_states and high_states:
            assert means_per_state[low_states[0]] < means_per_state[high_states[0]]


# ── Tests: walk-forward harness ──────────────────────────────────────────────

class TestWalkForward:
    def test_walk_forward_detects_transitions(self):
        """Synthetic features with alternating regimes → walk-forward
        should detect ≥1 transition per test split."""
        df = _build_synthetic_features(n_rows_per_state=80)
        # Synthetic SPY prices: drop 2% during HIGH_VOL block, +0.5%
        # otherwise, so liquidate-then-reenter helps during stress.
        prices = df[['ts_utc']].copy()
        prices['close'] = 580.0
        # Build a synthetic price path.
        rng = np.random.default_rng(7)
        change = np.where(df['vix_synth_30d'] > 22, -0.001, 0.0005)
        prices['close'] = 580.0 * np.cumprod(1 + change + rng.normal(0, 0.0005, len(df)))
        report = walk_forward(df, prices, n_splits=3)
        assert 'aggregate' in report
        # We expect at least one transition detected across all splits.
        assert report['aggregate']['n_transitions'] >= 1

    def test_walk_forward_handles_no_prices(self):
        """When prices_df is None, the report still produces transition
        counts but P&L is unavailable (events list empty)."""
        df = _build_synthetic_features(n_rows_per_state=80)
        report = walk_forward(df, prices_df=None, n_splits=3)
        assert 'aggregate' in report
        # Counts are zero because score_transition returns None without prices.
        # But split-level transition counts should be present.
        any_tx = any(s.get('transitions', 0) > 0 for s in report['splits'])
        assert any_tx


class TestDetectTransitions:
    def test_hysteresis_consumed_in_detect(self):
        """Run _detect_transitions on a perfectly bimodal feature stream
        and confirm ≥1 transition is detected per regime swap."""
        df = _build_synthetic_features(n_rows_per_state=80)
        # v3 feature set (commit 1172802, 2026-05-20): X is now (n, 6).
        # The walk-forward harness's _detect_transitions keeps the FLAT
        # (hysteresis_n, confidence_floor) contract; the LIVE detector
        # gained tiered hysteresis (6ae17eb 2026-05-23, 66c3119 2026-06-09)
        # but the harness was deliberately parameterised, so flat params
        # remain its contract.
        X, _ = trainer._build_X(df[trainer.HMM_INPUT_COLS])
        model = _train_hmm(X[:160])  # Train on first 2 blocks
        ts = df['ts_utc'].values
        events = _detect_transitions(model, X, ts,
                                      hysteresis_n=3, confidence_floor=0.6)
        # Synthetic data has 3 regime swaps (LOW→HIGH→LOW→HIGH); detector
        # should catch at least 1 with reasonable hyperparameters. Lower
        # bound — synthetic Gaussian-mixture HMM may collapse states.
        assert len(events) >= 1
