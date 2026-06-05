"""SP-6 B-flow Phase 1 — Alpaca 1-minute bar fetch + per-session parquet cache (§4).

Two layers, kept strictly separate so tests can inject at the right seam:

  * fetch layer  — ``fetch_session_bars`` does the network: chunk symbols, page
    through ``next_page_token``, retry on 429/5xx. Inject ``http_get`` to test it.
  * cache layer  — ``get_session_bars`` reads/writes one parquet per worked
    session, fetches ONLY missing tickers, normalises raw bars to the Bar
    contract, and remembers empties via a ``minute = -1`` sentinel row so they
    are never refetched. Inject ``fetcher`` to test it (the poisoned-fetcher
    test asserts it is never called when everything is cached).

Bar contract (consumed by oracle.py): one dict per minute
    {minute: int 0..389 (offset from 09:30 ET), o, h, l, c, v, vw}
``minute`` is an OFFSET, never a timestamp (the b1 CWD/offset traps). ``vw`` may
be None (Alpaca occasionally omits it on thin bars); oracle handles None/NaN.

The cache under ``data/cache/min_bars/`` is REBUILDABLE — it is NOT part of the
append-only ``data/master/`` family, so no NEVER-DELETE guard is needed. We
still never drop existing rows (additive concat + atomic os.replace), only
because corrupting an in-progress rebuild is wasteful.

Empirical Alpaca facts (verified live 2026-06-05, spec §4):
  GET https://data.alpaca.markets/v2/stocks/bars
  params: symbols=<comma list>, timeframe=1Min, feed=sip, limit=10000,
          sort=asc, start/end RFC3339, page_token (paginate until None)
  headers: APCA-API-KEY-ID / APCA-API-SECRET-KEY
  response: {"bars": {SYM: [{t,o,h,l,c,v,n,vw}, ...]}, "next_page_token": str|None}
  bar 't' is the RFC3339 UTC bar START; rate limit 10,000 req/min.
"""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
_NY = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_DEFAULT_CACHE = "/root/openclaw/data/cache/min_bars"
_CACHE_COLUMNS = ["ticker", "minute", "o", "h", "l", "c", "v", "vw"]
_SENTINEL_MINUTE = -1
_MAX_ATTEMPTS = 4   # 1 initial + 3 retries


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------
def _env(key):
    """Read a key from the process env or, failing that, /root/openclaw/.env
    (the worktree has no .env — gitignored). Mirrors b1_order_source.py:19-27."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        for line in open("/root/openclaw/.env"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return None


def load_alpaca_creds():
    """Return (key, secret) for the Alpaca data API. Raise RuntimeError with a
    clear message if either is missing (env or /root/openclaw/.env)."""
    key = _env("ALPACA_API_KEY")
    secret = _env("ALPACA_SECRET_KEY")
    if not key or not secret:
        missing = [n for n, v in (("ALPACA_API_KEY", key),
                                  ("ALPACA_SECRET_KEY", secret)) if not v]
        raise RuntimeError(
            "Missing Alpaca credentials: " + ", ".join(missing)
            + " (set in env or /root/openclaw/.env)")
    return key, secret


# --------------------------------------------------------------------------
# session bounds (DST-safe)
# --------------------------------------------------------------------------
def session_rth_utc(session):
    """Return (start_iso, end_iso): 09:30 and 16:00 America/New_York of
    ``session`` ('YYYY-MM-DD') converted to UTC ISO strings.

    DST-safe: ZoneInfo applies the correct offset for the date, so June (EDT,
    UTC-4) gives 13:30/20:00 UTC and January (EST, UTC-5) gives 14:30/21:00 UTC.
    Never hard-codes an offset (the b1 ``bucket_of`` EDT-only wart)."""
    y, m, d = (int(x) for x in session.split("-"))
    open_ny = datetime(y, m, d, 9, 30, tzinfo=_NY)
    close_ny = datetime(y, m, d, 16, 0, tzinfo=_NY)
    start = open_ny.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = close_ny.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def _session_open_utc(session):
    """The 09:30-ET instant of ``session`` as a tz-aware UTC datetime."""
    y, m, d = (int(x) for x in session.split("-"))
    return datetime(y, m, d, 9, 30, tzinfo=_NY).astimezone(_UTC)


def _session_close_utc(session):
    y, m, d = (int(x) for x in session.split("-"))
    return datetime(y, m, d, 16, 0, tzinfo=_NY).astimezone(_UTC)


# --------------------------------------------------------------------------
# fetch layer
# --------------------------------------------------------------------------
def _default_http_get(url, headers=None, params=None, timeout=None):
    """Thin requests.get wrapper. Does NOT raise_for_status — the retry logic in
    ``_get_with_retry`` inspects ``.status_code`` so it can distinguish a
    retryable 429/5xx from a genuine response."""
    return requests.get(url, headers=headers, params=params, timeout=timeout)


def _get_with_retry(http_get, url, headers, params, sleep_s):
    """Call ``http_get`` up to _MAX_ATTEMPTS times (1 initial + 3 retries),
    retrying on status 429 or >=500 with doubling backoff. Returns the parsed
    JSON of the first acceptable response; raises RuntimeError after the budget
    is exhausted on a retryable status."""
    backoff = max(sleep_s, 0.0) or 0.2
    last_status = None
    for attempt in range(_MAX_ATTEMPTS):
        resp = http_get(url, headers=headers, params=params, timeout=30)
        status = getattr(resp, "status_code", 200)
        last_status = status
        if status == 429 or status >= 500:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            break
        return resp.json()
    raise RuntimeError(
        f"Alpaca bars request failed after {_MAX_ATTEMPTS} attempts "
        f"(last status {last_status})")


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_session_bars(symbols, session, http_get=None, feed="sip",
                       chunk=20, sleep_s=0.2):
    """Fetch raw 1-minute bars for ``symbols`` over the RTH window of
    ``session`` ('YYYY-MM-DD'). Returns {symbol: [raw bar dict, ...]} with each
    requested symbol present (absent-from-response -> []).

    Chunks symbols into groups of <= ``chunk`` per request; paginates each chunk
    on ``next_page_token``; sleeps ``sleep_s`` between HTTP calls; retries each
    call on 429/5xx (see _get_with_retry). ``http_get`` is injectable for tests
    and defaults to a thin requests.get wrapper."""
    if http_get is None:
        http_get = _default_http_get
    key, secret = load_alpaca_creds()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    start, end = session_rth_utc(session)

    out = {s: [] for s in symbols}
    first_call = True
    for group in _chunked(list(symbols), chunk):
        page_token = None
        while True:
            if not first_call and sleep_s:
                time.sleep(sleep_s)
            first_call = False
            params = {
                "symbols": ",".join(group),
                "timeframe": "1Min",
                "feed": feed,
                "limit": 10000,
                "sort": "asc",
                "start": start,
                "end": end,
            }
            if page_token:
                params["page_token"] = page_token
            js = _get_with_retry(http_get, _BARS_URL, headers, params, sleep_s)
            bars_map = js.get("bars") or {}
            for sym, blist in bars_map.items():
                out.setdefault(sym, []).extend(blist or [])
            page_token = js.get("next_page_token")
            if not page_token:
                break
    return out


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------
def _parse_utc(t):
    """Parse an RFC3339 UTC timestamp (e.g. '2026-06-05T13:30:00Z') to a
    tz-aware UTC datetime."""
    s = t.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def normalize_rth(raw_bars, session):
    """Raw Alpaca bars -> list[Bar] for ``session``, sorted by minute.

    A bar is kept iff its instant ``t`` satisfies open_utc <= t < close_utc
    (instant comparison — date-safe and DST-safe with a single bound: 09:29 ET
    is excluded, 09:30 = minute 0, 15:59 = minute 389, 16:00 excluded).
    ``minute`` = round((t - open_utc)/60s). Missing 'vw' tolerated -> None."""
    open_utc = _session_open_utc(session)
    close_utc = _session_close_utc(session)
    bars = []
    for b in raw_bars:
        t = b.get("t")
        if t is None:
            continue
        dt = _parse_utc(t)
        if dt < open_utc or dt >= close_utc:
            continue
        minute = int(round((dt - open_utc).total_seconds() / 60.0))
        bars.append({
            "minute": minute,
            "o": b.get("o"),
            "h": b.get("h"),
            "l": b.get("l"),
            "c": b.get("c"),
            "v": b.get("v"),
            "vw": b.get("vw"),
        })
    bars.sort(key=lambda x: x["minute"])
    return bars


# --------------------------------------------------------------------------
# cache layer
# --------------------------------------------------------------------------
def _cache_path(cache_dir, session):
    return os.path.join(cache_dir, f"min_bars_{session}.parquet")


def _native(x):
    """Coerce a parquet-read scalar back to a native python int/float/None so
    Bar dicts compare equal across the round-trip. NaN (parquet's null for a
    float column) -> None."""
    if x is None:
        return None
    try:
        if isinstance(x, float) and math.isnan(x):
            return None
        if hasattr(x, "item"):          # numpy scalar
            x = x.item()
        if isinstance(x, float) and math.isnan(x):
            return None
    except (TypeError, ValueError):
        return x
    return x


def _load_cache(cache_dir, session):
    """Return (df_or_None, cached_tickers_set). A ticker is 'cached' if it has
    ANY row (including a minute=-1 sentinel) in the session parquet."""
    path = _cache_path(cache_dir, session)
    if not os.path.exists(path):
        return None, set()
    df = pd.read_parquet(path)
    cached = set(df["ticker"].dropna().unique()) if len(df) else set()
    return df, cached


def _bars_from_df(df, ticker):
    """Extract the Bar list for ``ticker`` from a cache DataFrame, dropping the
    sentinel (minute < 0) and coercing scalars back to native python."""
    sub = df[df["ticker"] == ticker]
    bars = []
    for _, row in sub.iterrows():
        minute = int(row["minute"])
        if minute < 0:
            continue
        bars.append({
            "minute": minute,
            "o": _native(row["o"]),
            "h": _native(row["h"]),
            "l": _native(row["l"]),
            "c": _native(row["c"]),
            "v": _native(row["v"]),
            "vw": _native(row["vw"]),
        })
    bars.sort(key=lambda x: x["minute"])
    return bars


def _rows_for_ticker(ticker, bars):
    """Build cache rows for ``ticker``. Zero bars -> one sentinel row
    (minute=-1) so the empty is remembered as fetched and never refetched."""
    if not bars:
        return [{"ticker": ticker, "minute": _SENTINEL_MINUTE,
                 "o": None, "h": None, "l": None, "c": None,
                 "v": None, "vw": None}]
    rows = []
    for b in bars:
        rows.append({
            "ticker": ticker,
            "minute": b["minute"],
            "o": b.get("o"), "h": b.get("h"), "l": b.get("l"),
            "c": b.get("c"), "v": b.get("v"), "vw": b.get("vw"),
        })
    return rows


def _atomic_write(df, path):
    """Write ``df`` to ``path`` via a tmp file + os.replace (atomic same-FS
    rename), so a kill mid-write never leaves a half-written parquet."""
    tmp = path + ".tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def get_session_bars(session, tickers, cache_dir=None, fetcher=fetch_session_bars):
    """Return {ticker: [Bar, ...]} for ``tickers`` on the worked ``session``,
    reading from / writing to one parquet per session.

    Loads any cached tickers; fetches ONLY the missing ones (via ``fetcher``,
    which returns RAW bars and is normalised here); appends the new data
    atomically; returns the Bar list per requested ticker (``[]`` for empties).
    A fetched ticker that returned zero bars is remembered via a minute=-1
    sentinel row so we never refetch it.

    ``cache_dir`` defaults to ``$BFLOW_CACHE_DIR`` or
    ``/root/openclaw/data/cache/min_bars``. ``fetcher`` is injectable; when every
    requested ticker is already cached it is NEVER called."""
    if cache_dir is None:
        cache_dir = os.environ.get("BFLOW_CACHE_DIR", _DEFAULT_CACHE)
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, session)

    df, cached = _load_cache(cache_dir, session)
    missing = [t for t in tickers if t not in cached]

    if missing:
        # fetcher signature mirrors fetch_session_bars(symbols, session, ...).
        raw_map = fetcher(missing, session)
        new_rows = []
        for t in missing:
            bars = normalize_rth(raw_map.get(t, []) or [], session)
            new_rows.extend(_rows_for_ticker(t, bars))
        if new_rows:
            new_df = pd.DataFrame(new_rows, columns=_CACHE_COLUMNS)
            if df is not None and len(df):
                combined = pd.concat([df, new_df], ignore_index=True)
            else:
                combined = new_df
            _atomic_write(combined, path)
            df = combined

    if df is None:
        df = pd.DataFrame(columns=_CACHE_COLUMNS)
    return {t: _bars_from_df(df, t) for t in tickers}


# --------------------------------------------------------------------------
# request-count estimator (for the driver log)
# --------------------------------------------------------------------------
def estimate_requests(pairs, chunk=20):
    """Estimate the number of HTTP requests (single-page) for an iterable of
    ``(session, ticker)`` pairs: group by session, dedup tickers within a
    session, sum ceil(unique_symbols / chunk) per session.

    NOTE for the run_phase1 driver: the chosen input shape is an iterable of
    (session, ticker) 2-tuples; ``chunk`` defaults to the fetch default 20.
    Pagination is not modelled (it's data-dependent)."""
    by_session = {}
    for session, ticker in pairs:
        by_session.setdefault(session, set()).add(ticker)
    total = 0
    for syms in by_session.values():
        total += math.ceil(len(syms) / chunk)
    return total
