"""A2: windowed ADV / latest-close reads must equal the legacy full-read pandas path."""
import importlib
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.data import parquet_store
from src.pipeline.market_cap_lookup import build_market_cap_lookup

AS_OF = date(2026, 6, 30)


def _sessions(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _rows(tkr, days, seed):
    rng = np.random.default_rng(seed)
    return [{"ticker": tkr, "date": d.isoformat(),
             "close": float(10 + rng.random() * 5), "volume": float(1000 + rng.integers(0, 5000))}
            for d in days]


def _frame():
    rows = _rows("ZZTEST1", _sessions(AS_OF, 50), 1) + _rows("ZZTEST2", _sessions(AS_OF, 50), 2)
    stale_end = AS_OF - timedelta(days=90)
    rows += _rows("ZZTEST3", _sessions(stale_end, 40), 3)
    rows += _rows("ZZTEST4", _sessions(AS_OF - timedelta(days=70), 100), 4)
    rows += _rows("ZZTEST4", _sessions(AS_OF, 5), 5)
    return pd.DataFrame(rows)


@pytest.fixture
def prices(tmp_path):
    p = tmp_path / "prices.parquet"
    _frame().to_parquet(p, index=False)
    return p


@pytest.fixture
def mod(monkeypatch, prices):
    monkeypatch.setenv("POSTGRES_URI", "postgresql://stub")
    m = importlib.import_module("src.pipeline.run_ticker_metadata_step")
    monkeypatch.setattr(m, "PRICES_PARQUET", str(prices))
    return m


def _legacy_adv(path, symbols):
    df = pd.read_parquet(path, columns=["ticker", "date", "close", "volume"])
    df = df[df["ticker"].isin(symbols)]
    df["dollar_volume"] = df["close"] * df["volume"]
    last_20 = (df.sort_values("date").groupby("ticker")
                 .tail(20).groupby("ticker")["dollar_volume"].mean())
    return {s: {"adv_usd_20d": float(last_20.get(s, 0.0))} for s in symbols}


def _same(new, old):
    assert new.keys() == old.keys()
    for s in old:
        a, b = new[s]["adv_usd_20d"], old[s]["adv_usd_20d"]
        if np.isnan(b):
            assert np.isnan(a)
        else:
            assert a == b  # exact: same pandas math, same summation order


SYMS = ["ZZTEST1", "ZZTEST2", "ZZTEST3", "ZZTEST4", "ZZTEST_NONE"]


def test_adv_matches_legacy_tail20_on_fresh_tickers(mod, prices):
    syms = ["ZZTEST1", "ZZTEST2"]
    _same(mod.adv_usd_from_parquet(syms, AS_OF), _legacy_adv(prices, syms))


def test_adv_true_nan_skipped_like_pandas(mod, tmp_path, monkeypatch):
    df = _frame()
    df.loc[df.index[(df.ticker == "ZZTEST1")][-3], "close"] = np.nan
    p = tmp_path / "nan.parquet"
    tbl = pa.table({c: pa.array(df[c].to_numpy(), from_pandas=False) for c in df.columns})
    pq.write_table(tbl, p)
    assert pq.read_table(p).column("close").null_count == 0  # real NaN, not null
    assert pq.read_table(p).column("close").to_pandas().isna().sum() == 1
    monkeypatch.setattr(mod, "PRICES_PARQUET", str(p))
    _same(mod.adv_usd_from_parquet(SYMS, AS_OF), _legacy_adv(p, SYMS))


def test_adv_stale_ticker_matches_legacy(mod, prices, tmp_path):
    out = mod.adv_usd_from_parquet(["ZZTEST3"], AS_OF)
    old = _legacy_adv(prices, ["ZZTEST3"])
    assert old["ZZTEST3"]["adv_usd_20d"] > 0
    _same(out, old)
    shares = tmp_path / "sh.parquet"
    pd.DataFrame([{"ticker": "ZZTEST3", "asof_date": "2026-01-01", "shares": 1e6}]).to_parquet(shares, index=False)
    assert build_market_cap_lookup(["ZZTEST3"], AS_OF, shares_path=shares, prices_path=prices) == {"ZZTEST3": None}


def test_adv_sparse_in_window_ticker_matches_legacy(mod, prices):
    _same(mod.adv_usd_from_parquet(["ZZTEST4"], AS_OF), _legacy_adv(prices, ["ZZTEST4"]))


def test_adv_log_line(mod, capsys):
    mod.adv_usd_from_parquet(["ZZTEST1", "ZZTEST2", "ZZTEST3", "ZZTEST4"], AS_OF)
    lines = [l for l in capsys.readouterr().err.splitlines() if "[adv_window]" in l]
    assert lines == ["[adv_window] adv_window_hits=2 adv_stale_fallback=2"]


def test_market_cap_identical_with_nan_latest_close(tmp_path):
    df = _frame()
    last = df.index[df.ticker == "ZZTEST1"][-1]
    df.loc[last, "close"] = None
    p = tmp_path / "px.parquet"
    df.to_parquet(p, index=False)
    shares = tmp_path / "sh.parquet"
    pd.DataFrame([{"ticker": t, "asof_date": "2026-01-01", "shares": 1e6}
                  for t in ("ZZTEST1", "ZZTEST2")]).to_parquet(shares, index=False)
    syms = ["ZZTEST1", "ZZTEST2"]
    iso, floor = AS_OF.isoformat(), (AS_OF - timedelta(days=10)).isoformat()
    px = pd.read_parquet(p, columns=["ticker", "date", "close"])
    px = px[px.ticker.isin(syms) & (px.date.astype(str) <= iso) & (px.date.astype(str) >= floor)]
    legacy = px.sort_values("date").groupby("ticker")["close"].last()
    out = build_market_cap_lookup(syms, AS_OF, shares_path=shares, prices_path=p)
    for s in syms:
        assert out[s] == pytest.approx(1e6 * float(legacy[s]), rel=1e-12)
    prev = df[df.ticker == "ZZTEST1"]["close"].dropna().iloc[-1]
    assert out["ZZTEST1"] == pytest.approx(1e6 * prev)


def test_ticker_window_stats_rejects_non_iso_cutoff(prices):
    with pytest.raises(ValueError):
        parquet_store.ticker_window_stats(prices, cutoff="2026-01-01' OR 1=1")
    with pytest.raises(ValueError):
        parquet_store.ticker_window_stats(prices, cutoff="2026-01-01", upto="x")


def test_ticker_window_stats_missing_file(tmp_path):
    df = parquet_store.ticker_window_stats(tmp_path / "nope.parquet", cutoff="2026-01-01")
    assert df.empty and list(df.columns) == ["ticker", "last_close", "last_date", "adv_usd", "n_rows"]


def test_no_full_read(mod, prices, tmp_path, monkeypatch):
    real_pd = pd.read_parquet

    def guard(path, *a, **k):
        assert str(path) != str(prices), "full pandas read of prices"
        return real_pd(path, *a, **k)

    monkeypatch.setattr(pd, "read_parquet", guard)
    real_rt = pq.read_table
    calls = []

    def wrapped(*a, **k):
        if str(a[0]) != str(prices):
            return real_rt(*a, **k)
        flt = k.get("filters")
        assert flt and any(f[0] == "ticker" and f[1] == "in" for f in flt)
        calls.append(1)
        return real_rt(*a, **k)

    monkeypatch.setattr(pq, "read_table", wrapped)
    out = mod.adv_usd_from_parquet(SYMS, AS_OF)
    assert calls and out["ZZTEST3"]["adv_usd_20d"] > 0
    shares = tmp_path / "sh.parquet"
    pd.DataFrame([{"ticker": "ZZTEST1", "asof_date": "2026-01-01", "shares": 1e6}]).to_parquet(shares, index=False)
    assert build_market_cap_lookup(["ZZTEST1"], AS_OF, shares_path=shares, prices_path=prices)["ZZTEST1"] > 0


def test_adv_all_nan_last20_matches_legacy(mod, tmp_path, monkeypatch):
    df = _frame()
    idx = df.index[df.ticker == "ZZTEST1"][-20:]
    df.loc[idx, "close"] = np.nan
    p = tmp_path / "allnan.parquet"
    pq.write_table(pa.table({c: pa.array(df[c].to_numpy(), from_pandas=False) for c in df.columns}), p)
    monkeypatch.setattr(mod, "PRICES_PARQUET", str(p))
    old = _legacy_adv(p, SYMS)
    assert np.isnan(old["ZZTEST1"]["adv_usd_20d"]) and old["ZZTEST_NONE"]["adv_usd_20d"] == 0.0
    _same(mod.adv_usd_from_parquet(SYMS, AS_OF), old)


def test_need_volume_branches(tmp_path):
    p = tmp_path / "novol.parquet"
    pd.DataFrame([{"ticker": "ZZTEST1", "date": "2026-06-01", "close": 5.0}]).to_parquet(p, index=False)
    with pytest.raises(KeyError):
        parquet_store.ticker_window_stats(p, cutoff="2026-05-01")
    with pytest.raises(KeyError):
        parquet_store.ticker_window_tail_rows(p, cutoff="2026-05-01")
    out = parquet_store.ticker_window_stats(p, cutoff="2026-05-01", need_volume=False)
    assert out["last_close"].iloc[0] == 5.0 and pd.isna(out["adv_usd"].iloc[0])


@pytest.mark.parametrize("bad", [None, 20260101, "2026-01-01\n", "2026-1-1"])
def test_cutoff_must_be_iso_string(prices, bad):
    with pytest.raises(ValueError):
        parquet_store.ticker_window_stats(prices, cutoff=bad)


def test_missing_file_logs_one_line(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "PRICES_PARQUET", str(tmp_path / "nope.parquet"))
    out = mod.adv_usd_from_parquet(["ZZTEST1"], AS_OF)
    assert out == {"ZZTEST1": {"adv_usd_20d": None}}
    lines = [l for l in capsys.readouterr().err.splitlines() if "[adv_window]" in l]
    assert lines == ["[adv_window] adv_window_hits=0 adv_stale_fallback=0 missing_file=1"]


def test_market_cap_accepts_datetime(tmp_path):
    from datetime import datetime
    p = tmp_path / "px.parquet"
    _frame().to_parquet(p, index=False)
    shares = tmp_path / "sh.parquet"
    pd.DataFrame([{"ticker": "ZZTEST1", "asof_date": "2026-01-01", "shares": 1e6}]).to_parquet(shares, index=False)
    a = build_market_cap_lookup(["ZZTEST1"], AS_OF, shares_path=shares, prices_path=p)
    b = build_market_cap_lookup(["ZZTEST1"], datetime(AS_OF.year, AS_OF.month, AS_OF.day, 13, 5),
                                shares_path=shares, prices_path=p)
    assert a == b
