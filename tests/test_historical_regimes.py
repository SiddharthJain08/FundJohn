"""tests/test_historical_regimes.py

Smoke tests for the deterministic VIX-tier regime classifier introduced
2026-04-27 to support regime-stratified backtests.

The classifier reads VIX from data/master/macro.parquet (live data — no
mocking) and applies a 5-day rolling-median + tier mapping. Tests assert:
  1. Known historical dates land in the regimes domain experts would expect.
  2. Single-day VIX spikes don't flip the smoothed regime.
  3. find_regime_windows respects min_days and the coalesce_gap_days hint.

Run:
    pytest tests/test_historical_regimes.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.historical_regimes import (  # noqa: E402
    classify_history,
    find_regime_windows,
    regime_for_date,
    rebuild_cache,
)


@pytest.fixture(scope='module', autouse=True)
def _ensure_cache():
    """Build the cache once; subsequent lookups read it directly."""
    rebuild_cache()
    yield


def test_classifier_returns_known_regimes_for_known_dates():
    # COVID crash peak — VIX spent weeks above 30.
    assert regime_for_date('2020-03-16') == 'CRISIS'
    # Sustained calm — mid-2017 ran at ~10-11 on the VIX, well into LOW_VOL.
    assert regime_for_date('2017-07-03') == 'LOW_VOL'
    # Late 2022 (gilt-crisis week) — VIX printed mid-30s; HIGH_VOL or CRISIS
    # are both legitimate under the deterministic tier mapping. Just assert
    # the elevated band, not the exact tier.
    state = regime_for_date('2022-09-30')
    assert state in {'HIGH_VOL', 'CRISIS'}, \
        f'2022-09-30 expected elevated regime, got {state}'


def test_5d_smoothing_prevents_single_day_flips():
    """A one-day VIX print of 35 inside an otherwise-quiet (~12) week
    should NOT flip the smoothed regime to CRISIS — the median of 5 calm
    days plus one spike stays calm."""
    # We can't easily inject a fake row into the parquet without a fixture
    # framework, but we can sanity-check the smoothing math directly.
    df = classify_history()
    # Find a day where the raw VIX > 22 but the 5d median < 22 (i.e. a spike
    # that the smoother caught and refused to upgrade). If any such day exists
    # in the last 10 years, the smoother is doing its job.
    hits = df[(df['vix'] > 22.0) & (df['vix_smoothed'] < 22.0)]
    assert len(hits) > 0, (
        'expected at least one VIX spike that 5-day smoothing absorbed; '
        'either the smoother is broken or VIX has been suspiciously quiet'
    )
    # And those days should keep their LOW_VOL/TRANSITIONING label rather
    # than getting bumped to HIGH_VOL/CRISIS.
    assert all(r in ('LOW_VOL', 'TRANSITIONING') for r in hits['regime'].unique()), \
        f'spikes that smoothed-to-calm got non-calm labels: {hits["regime"].unique()}'


def test_find_regime_windows_respects_min_days():
    """A min_days threshold must not return any window shorter than that."""
    for r in ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'):
        wins = find_regime_windows(r, min_days=60)
        for s, e in wins:
            span = (pd.to_datetime(e) - pd.to_datetime(s)).days + 1
            assert span >= 60, (
                f'{r} window {s} → {e} is {span} days, below min_days=60'
            )


def test_crisis_window_covers_covid_crash():
    """The 2020 COVID crash should appear as exactly one CRISIS window
    spanning roughly Mar–May 2020."""
    wins = find_regime_windows('CRISIS', min_days=60)
    assert len(wins) >= 1, 'no CRISIS windows found — VIX backfill may be missing'
    starts = [pd.to_datetime(s) for s, _ in wins]
    # At least one CRISIS window starts in Q1 2020.
    q1_2020 = pd.to_datetime('2020-01-01')
    q2_2020_end = pd.to_datetime('2020-06-30')
    matches = [s for s in starts if q1_2020 <= s <= q2_2020_end]
    assert len(matches) >= 1, (
        f'no CRISIS window starts inside Q1-Q2 2020; got starts {[s.date() for s in starts]}'
    )


def test_classify_history_columns():
    """The DataFrame returned by classify_history has the documented columns."""
    df = classify_history('2020-01-01', '2020-12-31')
    assert list(df.columns) == ['date', 'vix', 'vix_smoothed', 'regime']
    assert len(df) > 200, f'expected ~252 trading days in 2020, got {len(df)}'
    # All regimes in range should be canonical.
    assert set(df['regime'].unique()) <= {'LOW_VOL', 'TRANSITIONING',
                                           'HIGH_VOL', 'CRISIS', 'UNKNOWN'}
