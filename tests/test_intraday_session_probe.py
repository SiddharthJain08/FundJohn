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
