"""Tests for src/research/bflow/minbar_cache.py — Alpaca 1-minute bar fetch +
session parquet cache (§4 of the Phase-1 oracle kill-test design).

NO network, NO disk outside tmp_path: every HTTP path is exercised through an
injected ``http_get`` (fetch layer) or ``fetcher`` (cache layer) stub. The two
injection layers are deliberately separate and never crossed.

The 6 required behaviours (spec):
  1. session_rth_utc DST-safe for June (EDT) and January (EST).
  2. fetch pagination concatenates next_page_token pages per symbol.
  3. chunking: 45 symbols @ chunk=20 -> 3 HTTP calls.
  4. retry: 429 twice then 200 -> ok; 4x 500 -> raises.
  5. normalize_rth: 09:30 -> minute 0, 15:59 -> minute 389, 13:29 and 16:00
     excluded, on an EDT (June) session.
  6. cache round-trip in tmp_path: fetch once; second call with a poisoned
     fetcher returns identical bars; empty-ticker sentinel prevents refetch;
     a second session writes a separate file.
"""
import os

import pytest

from src.research.bflow import minbar_cache


# --------------------------------------------------------------------------
# stub HTTP response (matches the requests.Response surface we use:
# .status_code + .json())
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _bar(t, o=10.0, h=10.1, l=9.9, c=10.0, v=100.0, n=5, vw=10.0):
    """Build a raw Alpaca bar dict (the on-the-wire shape: t,o,h,l,c,v,n,vw)."""
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "n": n, "vw": vw}


# ==========================================================================
# 1. session_rth_utc — DST-safe
# ==========================================================================
def test_session_rth_utc_june_is_edt():
    # 2026-06-05 is EDT (UTC-4): 09:30 ET = 13:30 UTC, 16:00 ET = 20:00 UTC.
    start, end = minbar_cache.session_rth_utc("2026-06-05")
    assert start.startswith("2026-06-05T13:30")
    assert end.startswith("2026-06-05T20:00")


def test_session_rth_utc_january_is_est():
    # 2026-01-15 is EST (UTC-5): 09:30 ET = 14:30 UTC, 16:00 ET = 21:00 UTC.
    start, end = minbar_cache.session_rth_utc("2026-01-15")
    assert start.startswith("2026-01-15T14:30")
    assert end.startswith("2026-01-15T21:00")


# ==========================================================================
# 2. fetch pagination
# ==========================================================================
def test_fetch_session_bars_paginates():
    # page 1 has a next_page_token, page 2 is null -> bars concatenated per symbol.
    calls = {"n": 0}

    def http_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if not params.get("page_token"):
            return _Resp(200, {
                "bars": {"AAA": [_bar("2026-06-05T13:30:00Z", vw=10.0)]},
                "next_page_token": "TOK2",
            })
        # page 2
        return _Resp(200, {
            "bars": {"AAA": [_bar("2026-06-05T13:31:00Z", vw=11.0)]},
            "next_page_token": None,
        })

    os.environ["ALPACA_API_KEY"] = "k"
    os.environ["ALPACA_SECRET_KEY"] = "s"
    out = minbar_cache.fetch_session_bars(
        ["AAA"], "2026-06-05", http_get=http_get, sleep_s=0)
    assert calls["n"] == 2
    assert [b["vw"] for b in out["AAA"]] == [10.0, 11.0]


# ==========================================================================
# 3. chunking
# ==========================================================================
def test_fetch_session_bars_chunks_symbols():
    # 45 symbols, chunk=20 -> ceil(45/20)=3 HTTP calls (single page each).
    seen_symbol_params = []

    def http_get(url, headers=None, params=None, timeout=None):
        seen_symbol_params.append(params["symbols"])
        return _Resp(200, {"bars": {}, "next_page_token": None})

    os.environ["ALPACA_API_KEY"] = "k"
    os.environ["ALPACA_SECRET_KEY"] = "s"
    symbols = [f"S{i:03d}" for i in range(45)]
    out = minbar_cache.fetch_session_bars(
        symbols, "2026-06-05", http_get=http_get, chunk=20, sleep_s=0)
    assert len(seen_symbol_params) == 3
    # chunk sizes 20,20,5
    sizes = [len(s.split(",")) for s in seen_symbol_params]
    assert sizes == [20, 20, 5]
    # symbols absent from the response map to []
    assert out["S000"] == []
    assert out["S044"] == []


# ==========================================================================
# 4. retry
# ==========================================================================
def test_fetch_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(minbar_cache.time, "sleep", lambda *_a, **_k: None)
    seq = [_Resp(429, {}), _Resp(429, {}),
           _Resp(200, {"bars": {"AAA": [_bar("2026-06-05T13:30:00Z")]},
                       "next_page_token": None})]
    calls = {"n": 0}

    def http_get(url, headers=None, params=None, timeout=None):
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    os.environ["ALPACA_API_KEY"] = "k"
    os.environ["ALPACA_SECRET_KEY"] = "s"
    out = minbar_cache.fetch_session_bars(
        ["AAA"], "2026-06-05", http_get=http_get, sleep_s=0)
    # 1 initial + 2 retries = 3 attempts, third is the 200
    assert calls["n"] == 3
    assert len(out["AAA"]) == 1


def test_fetch_raises_after_four_500s(monkeypatch):
    monkeypatch.setattr(minbar_cache.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def http_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        return _Resp(500, {})

    os.environ["ALPACA_API_KEY"] = "k"
    os.environ["ALPACA_SECRET_KEY"] = "s"
    with pytest.raises(Exception):
        minbar_cache.fetch_session_bars(
            ["AAA"], "2026-06-05", http_get=http_get, sleep_s=0)
    # 1 initial + 3 retries = 4 attempts, then raise
    assert calls["n"] == 4


# ==========================================================================
# 5. normalize_rth
# ==========================================================================
def test_normalize_rth_windows_and_minute_arithmetic():
    # EDT session: 09:30 ET = 13:30 UTC.
    session = "2026-06-05"
    raw = [
        _bar("2026-06-05T13:29:00Z", vw=1.0),   # 09:29 ET -> EXCLUDED (< open)
        _bar("2026-06-05T13:30:00Z", vw=2.0),   # 09:30 ET -> minute 0
        _bar("2026-06-05T13:31:00Z", vw=3.0),   # 09:31 ET -> minute 1
        _bar("2026-06-05T19:59:00Z", vw=4.0),   # 15:59 ET -> minute 389
        _bar("2026-06-05T20:00:00Z", vw=5.0),   # 16:00 ET -> EXCLUDED (>= close)
    ]
    bars = minbar_cache.normalize_rth(raw, session)
    minutes = [b["minute"] for b in bars]
    assert minutes == [0, 1, 389]            # sorted, 13:29 and 16:00 dropped
    vws = [b["vw"] for b in bars]
    assert vws == [2.0, 3.0, 4.0]
    # contract fields present
    first = bars[0]
    for k in ("minute", "o", "h", "l", "c", "v", "vw"):
        assert k in first


def test_normalize_rth_tolerates_missing_vw():
    session = "2026-06-05"
    raw = [{"t": "2026-06-05T13:30:00Z", "o": 1.0, "h": 1.0,
            "l": 1.0, "c": 1.0, "v": 50.0}]   # NO 'vw'
    bars = minbar_cache.normalize_rth(raw, session)
    assert len(bars) == 1
    assert bars[0]["minute"] == 0
    assert bars[0]["vw"] is None


def test_normalize_rth_sorts_out_of_order():
    session = "2026-06-05"
    raw = [
        _bar("2026-06-05T13:32:00Z", vw=3.0),   # minute 2
        _bar("2026-06-05T13:30:00Z", vw=1.0),   # minute 0
        _bar("2026-06-05T13:31:00Z", vw=2.0),   # minute 1
    ]
    bars = minbar_cache.normalize_rth(raw, session)
    assert [b["minute"] for b in bars] == [0, 1, 2]


# ==========================================================================
# 6. cache round-trip
# ==========================================================================
def _finite_raw(t, vw):
    """A FULLY-POPULATED finite raw bar so dict == survives the parquet
    round-trip (parquet turns None->NaN, NaN!=NaN; finite python<->numpy
    scalars compare equal once we coerce back to native on read)."""
    return _bar(t, o=10.0, h=10.2, l=9.8, c=10.05, v=123.0, n=7, vw=vw)


def test_cache_round_trip_and_sentinel(tmp_path, monkeypatch):
    session = "2026-06-05"
    cache_dir = str(tmp_path)

    # First fetcher: AAA has bars, BBB is genuinely empty (-> sentinel).
    fetch_calls = {"n": 0, "symbols": None}

    def fetcher(symbols, sess, **kw):   # real (symbols, session) order
        fetch_calls["n"] += 1
        assert sess == session          # date arrives in 2nd slot, not 1st
        fetch_calls["symbols"] = sorted(symbols)
        return {
            "AAA": [_finite_raw("2026-06-05T13:30:00Z", 10.0),
                    _finite_raw("2026-06-05T13:31:00Z", 11.0)],
            "BBB": [],   # empty -> must be remembered as fetched
        }

    out1 = minbar_cache.get_session_bars(
        session, ["AAA", "BBB"], cache_dir=cache_dir, fetcher=fetcher)
    assert fetch_calls["n"] == 1
    assert fetch_calls["symbols"] == ["AAA", "BBB"]
    assert len(out1["AAA"]) == 2
    assert out1["BBB"] == []                    # empty list returned, not sentinel row

    # the session parquet exists
    pq = tmp_path / "min_bars_2026-06-05.parquet"
    assert pq.exists()

    # Second call: poisoned fetcher (raises if called). Both tickers are cached
    # (BBB via its sentinel), so the fetcher must NEVER run.
    def poisoned(symbols, sess, **kw):
        raise AssertionError(f"fetcher must not be called; got {symbols}")

    out2 = minbar_cache.get_session_bars(
        session, ["AAA", "BBB"], cache_dir=cache_dir, fetcher=poisoned)
    assert out2["BBB"] == []
    # AAA round-trips identically (finite -> dict == holds)
    assert out2["AAA"] == out1["AAA"]
    # bar contract keys after read
    for k in ("minute", "o", "h", "l", "c", "v", "vw"):
        assert k in out2["AAA"][0]


def test_cache_separate_file_per_session(tmp_path):
    cache_dir = str(tmp_path)

    def fetcher(symbols, sess, **kw):   # real (symbols, session) order
        # encode the session into vw so we can tell files apart
        vw = 1.0 if sess == "2026-06-05" else 2.0
        return {t: [_finite_raw(f"{sess}T13:30:00Z", vw)] for t in symbols}

    out_a = minbar_cache.get_session_bars(
        "2026-06-05", ["AAA"], cache_dir=cache_dir, fetcher=fetcher)
    out_b = minbar_cache.get_session_bars(
        "2026-06-08", ["AAA"], cache_dir=cache_dir, fetcher=fetcher)

    assert (tmp_path / "min_bars_2026-06-05.parquet").exists()
    assert (tmp_path / "min_bars_2026-06-08.parquet").exists()
    assert out_a["AAA"][0]["vw"] == 1.0
    assert out_b["AAA"][0]["vw"] == 2.0


def test_cache_only_fetches_missing_tickers(tmp_path):
    cache_dir = str(tmp_path)
    seen = []

    def fetcher(symbols, sess, **kw):   # real (symbols, session) order
        seen.append(sorted(symbols))
        return {t: [_finite_raw("2026-06-05T13:30:00Z", 10.0)] for t in symbols}

    minbar_cache.get_session_bars(
        "2026-06-05", ["AAA"], cache_dir=cache_dir, fetcher=fetcher)
    # second call adds BBB; only BBB should be fetched (AAA already cached)
    minbar_cache.get_session_bars(
        "2026-06-05", ["AAA", "BBB"], cache_dir=cache_dir, fetcher=fetcher)
    assert seen == [["AAA"], ["BBB"]]


# ==========================================================================
# estimate_requests helper
# ==========================================================================
def test_estimate_requests_groups_by_session():
    # 45 unique symbols in one session @ chunk 20 -> 3 requests.
    pairs = [("2026-06-05", f"S{i}") for i in range(45)]
    assert minbar_cache.estimate_requests(pairs) == 3


def test_estimate_requests_multi_session_dedups():
    # session A: 2 unique symbols -> 1 req; session B: 25 unique -> 2 reqs => 3.
    pairs = ([("A", "X"), ("A", "Y"), ("A", "X")]
             + [("B", f"S{i}") for i in range(25)])
    assert minbar_cache.estimate_requests(pairs) == 3
