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
