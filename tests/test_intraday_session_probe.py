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


def test_prep_prices_computes_return_and_filters_equity():
    prices = pd.DataFrame({
        "ticker": ["AAA", "AAA", "^VIX", "BTC-USD", "BBB"],
        "date":   ["2024-01-02"] * 5,
        "open":   [100.0, 0.0, 20.0, 50000.0, 10.0],
        "close":  [99.0, 50.0, 19.0, 51000.0, 10.5],
    })
    out = p.prep_prices(prices)
    # ^VIX and BTC-USD dropped (non-equity); AAA open=0.0 row dropped
    assert set(out["ticker"]) == {"AAA", "BBB"}
    aaa = out[out["ticker"] == "AAA"].iloc[0]
    assert abs(aaa["intraday_return"] - (-0.01)) < 1e-12


def test_attach_regime_and_bucket():
    df = pd.DataFrame({"ticker": ["AAA"], "date": ["2024-07-03"],
                       "intraday_return": [-0.005]})
    regimes = pd.DataFrame({"date": ["2024-07-03"], "regime": ["HIGH_VOL"]})
    out = p.attach_regime_bucket(df, regimes)
    assert out.iloc[0]["regime"] == "HIGH_VOL"
    assert out.iloc[0]["bucket"] == "2024H2"


def test_attach_primary_inner_joins_returns():
    primary = pd.DataFrame({"ticker": ["AAA", "ZZZ"], "date": ["2024-01-02", "2024-01-02"]})
    prices_prepped = pd.DataFrame({"ticker": ["AAA"], "date": ["2024-01-02"],
                                   "intraday_return": [-0.01]})
    out = p.attach_primary(primary, prices_prepped)
    # ZZZ has no price row -> dropped
    assert list(out["ticker"]) == ["AAA"]
    assert abs(out.iloc[0]["intraday_return"] - (-0.01)) < 1e-12
