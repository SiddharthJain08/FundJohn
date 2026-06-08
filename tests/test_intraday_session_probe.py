# tests/test_intraday_session_probe.py
import math
import pandas as pd
import pytest
from research.exit_timing import intraday_session_probe as p


def test_clustered_t_known_values():
    # 3 day-clusters; per-day means = [0.02, -0.01, 0.05]; mean=0.02
    df = pd.DataFrame({
        "date": ["d1", "d1", "d2", "d3"],
        "intraday_return": [0.01, 0.03, -0.01, 0.05],
    })
    mean, t, n = p.clustered_t(df, "intraday_return", "date")
    assert n == 3
    assert abs(mean - 0.02) < 1e-12
    g = [0.02, -0.01, 0.05]
    sd = pd.Series(g).std(ddof=1)
    assert abs(t - (0.02 / (sd / math.sqrt(3)))) < 1e-9


def test_clustered_t_degenerate_single_cluster():
    df = pd.DataFrame({"date": ["d1", "d1"], "intraday_return": [0.01, 0.03]})
    mean, t, n = p.clustered_t(df, "intraday_return", "date")
    assert n == 1
    assert abs(mean - 0.02) < 1e-12
    assert math.isnan(t)


def test_half_year_bucket():
    assert p.half_year_bucket("2024-03-15") == "2024H1"
    assert p.half_year_bucket("2024-06-30") == "2024H1"
    assert p.half_year_bucket("2024-07-01") == "2024H2"
    assert p.half_year_bucket("2026-12-31") == "2026H2"


def test_verdict_invalid_data():
    assert p.verdict(primary_mean=-0.001, primary_t=-4.0,
                     recent_ts=[-0.5, 0.2], n_clusters=499) == "INVALID-DATA"


def test_verdict_nogo_pooled_positive():
    assert p.verdict(primary_mean=0.002, primary_t=3.5,
                     recent_ts=[0.1, -0.2], n_clusters=2000) == "NO-GO"


def test_verdict_nogo_recent_bucket_positive():
    # pooled benign but a recent half-year is reliably positive
    assert p.verdict(primary_mean=0.0001, primary_t=0.5,
                     recent_ts=[2.4, -0.3], n_clusters=2000) == "NO-GO"


def test_verdict_clear_to_ship():
    assert p.verdict(primary_mean=-0.0008, primary_t=-4.2,
                     recent_ts=[-1.0, -0.5], n_clusters=2000) == "CLEAR-TO-SHIP-GATED"


def test_verdict_clear_with_caution():
    # positive point estimate but not significant, recent benign
    assert p.verdict(primary_mean=0.0003, primary_t=1.1,
                     recent_ts=[0.8, -0.4], n_clusters=2000) == "CLEAR-WITH-CAUTION"
