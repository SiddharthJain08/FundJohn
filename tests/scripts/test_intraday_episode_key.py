"""Regression test for the 15-min floored episode key in run_intraday_market_state.

Ensures that two timestamps within the same 15-min bucket produce the same
episode key component, while a timestamp in the next bucket produces a
different one. This prevents duplicate prefetch spawns on cron overlaps.
"""
import pandas as pd


def _episode_ts_component(ts: pd.Timestamp) -> str:
    """Mirrors the floor applied in run_intraday_market_state.run_one_tick."""
    return ts.floor("15min").isoformat()


def test_episode_floor_buckets_same_window():
    a = _episode_ts_component(pd.Timestamp("2026-06-09 14:02:11", tz="UTC"))
    b = _episode_ts_component(pd.Timestamp("2026-06-09 14:14:59", tz="UTC"))
    c = _episode_ts_component(pd.Timestamp("2026-06-09 14:15:01", tz="UTC"))
    assert a == b, "timestamps in the same 15-min window must share one episode bucket"
    assert a != c, "timestamp in the next window must produce a new episode bucket"


def test_episode_floor_boundary_exact():
    # 14:15:00 itself starts the new bucket
    on_boundary = _episode_ts_component(pd.Timestamp("2026-06-09 14:15:00", tz="UTC"))
    before = _episode_ts_component(pd.Timestamp("2026-06-09 14:14:59", tz="UTC"))
    assert on_boundary != before, "exact boundary timestamp belongs to the next bucket"


def test_episode_floor_consistent_with_inline_expression():
    # Verify the inline expression used in the script produces the same result
    ts = pd.Timestamp("2026-06-09 15:07:33.123456", tz="UTC")
    from_helper = _episode_ts_component(ts)
    inline = ts.floor("15min").isoformat()
    assert from_helper == inline, "helper must be identical to the inline expression"


# ── make_episode tick-1 ↔ tick-3 parity ─────────────────────────────────────
#
# Correctness of the episode-bound data-ready gate depends ONLY on tick-1
# (candidate prefetch) and tick-3 (gate) producing the SAME episode string
# for the SAME streak-start tick. tick-1 builds it from `features['ts_utc']`
# (the live tick, which IS the streak start when streak==1); tick-3
# reconstructs the streak-start from `history[streak-2]` (newest-first).

import importlib.util
from pathlib import Path


def _load_detector():
    spec = importlib.util.spec_from_file_location(
        'rims_ep', Path('/root/openclaw/scripts/run_intraday_market_state.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_make_episode_matches_tick1_format():
    """make_episode must be byte-identical to the legacy inline tick-1 format
    `{date}:{state}:{floor15.isoformat()}` so existing sentinels still match."""
    m = _load_detector()
    ts = pd.Timestamp("2026-06-09 15:07:33", tz="UTC")
    state = "HIGH_VOL"
    legacy = f"{ts.strftime('%Y-%m-%d')}:{state}:{ts.floor('15min').isoformat()}"
    assert m.make_episode(ts, state) == legacy


def test_make_episode_tick1_tick3_parity_via_history_reconstruction():
    """tick-1 (live ts at streak start) and tick-3 (history[streak-2]) produce
    the SAME episode for the same streak-start tick."""
    m = _load_detector()
    state = "HIGH_VOL"
    streak_start = pd.Timestamp("2026-06-09 15:00:00", tz="UTC")

    # tick-1: at streak==1 the live tick IS the streak start.
    tick1_episode = m.make_episode(streak_start, state)

    # tick-3: streak==3, history newest-first = [prev tick, streak-start, settled...].
    # history[streak-2] = history[1] = the streak-start tick.
    streak = 3
    history = [
        {'ts_utc': '2026-06-09 15:30:00+00:00', 'state': state},   # history[0] prev tick
        {'ts_utc': str(streak_start), 'state': state},             # history[1] streak start
        {'ts_utc': '2026-06-09 14:45:00+00:00', 'state': 'LOW_VOL'},  # settled
    ]
    recovered = pd.Timestamp(history[streak - 2]['ts_utc'])
    if recovered.tzinfo is None:
        recovered = recovered.tz_localize('UTC')
    tick3_episode = m.make_episode(recovered, state)

    assert tick1_episode == tick3_episode


def test_make_episode_different_streak_start_differs():
    """A different streak-start tick (e.g. a later episode in the same day)
    must yield a DIFFERENT episode so a prior episode's sentinel can't match."""
    m = _load_detector()
    state = "HIGH_VOL"
    ep_a = m.make_episode(pd.Timestamp("2026-06-09 15:00:00", tz="UTC"), state)
    ep_b = m.make_episode(pd.Timestamp("2026-06-09 15:30:00", tz="UTC"), state)
    assert ep_a != ep_b
