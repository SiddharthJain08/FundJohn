"""close_proxy_snapshot.py — same-day close[t] price proxy for the signal engine.

Fetches the latest (~3 PM) price per universe ticker so the daily compute can
inject a today-dated row into the price panel, making live signal generation
mirror the backtests' close[t] decision. Read-only: never writes the master
parquet (the post-close EOD refresh writes the real close[t]).

Equities/ETPs: `alpaca data multi-snapshots` (latestTrade.p, falling back to
minuteBar.c then dailyBar.c). Crypto (BASE-USD): `alpaca data crypto
latest-trades`. Indices/futures/FX are skipped (no snapshot), as are symbol
classes Alpaca does not serve (preferreds `X-PRA`, rights/warrants/units,
multi-letter dot suffixes like DX-Y.NYB) — one such symbol in a request 400s
the WHOLE chunk ("invalid symbol"), which un-hardened dropped ~50 innocent
tickers per offender (2026-07-29 probe: near-total coverage loss on the
12.5k universe). Residual offenders are retried out of the chunk by parsing
the broker's own "invalid symbol: X" error.

Freshness (2026-07-29 same-day pivot): when ``asof_date`` is passed, only
prices whose snapshot node is stamped that date are accepted — a thinly
traded name whose last trade was days ago is OMITTED (NaN in the injected
row) rather than served as a stale close[t]. Coverage below the configured
floor raises, so the acting book is never sized off a hollow snapshot.
``asof_date=None`` keeps the legacy accept-anything behavior (dashboard /
ad-hoc callers).

COVERAGE FLOOR CALIBRATION (measured 2026-07-29 09:50 ET, healthy session):
of 12,036 provider-returned equities in the 10-year panel, 10,336 (85.9%)
had a same-day print; against the active tradable universe it was 87.0%.
The shortfall is not breakage — it is illiquid names that had not traded
20 minutes into the session, plus delisted tickers the API still answers
for. Coverage rises through the day and the real chain runs at 15:00 ET.
The floor therefore exists to catch a DEAD snapshot service (coverage near
zero), not thin early prints: 0.60 leaves wide headroom above a healthy
day's worst case while still aborting an outage.

Raises CloseProxyError on TOTAL failure (nothing fetched) or, with an
asof_date, on equity coverage below OPENCLAW_CLOSE_PROXY_MIN_COVERAGE
(default 0.60 — calibrated below). The caller (engine.load_prices) must let this propagate so
the signals step aborts rather than emitting an empty signal set that would
orphan-close the whole book.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

CLI = os.environ.get("ALPACA_CLI_BIN", "/root/go/bin/alpaca")
_CHUNK = 150  # 2026-08-22: 50 → 150 (matches collector.runIntradaySnapshotPrices); ~100 → ~35 calls on the 15:00 hot path
_TIMEOUT = 40
# Bound on invalid-symbol evictions per chunk before giving up on it.
_MAX_INVALID_RETRIES = 8

_INVALID_SYMBOL_RE = re.compile(r"invalid symbol:\s*([A-Za-z0-9./\-]+)")


class CloseProxyError(RuntimeError):
    """Raised when the close[t]-proxy snapshot cannot be fetched at all,
    or (asof mode) covers too little of the equity universe."""


def _to_alpaca_equity(ticker: str) -> str | None:
    """engine ticker -> Alpaca equity symbol; None for symbols with no
    Alpaca equity snapshot: indices `^`, futures/FX `=`, crypto `-USD`,
    preferred shares (`CMS-PRC`), rights/warrants/units (`X-RT`/`X-WS`/
    `X-UN`), and multi-letter dot suffixes (`DX-Y.NYB`). Sending any of
    these poisons its whole multi-snapshot chunk with a 400.

    Both separators carry these classes: master prices holds `ACHR.WS` and
    `AXIA.PR` alongside the dash forms, and a 2-char dot suffix slipped
    through the length rule below (found 2026-07-30 via the options chain
    feed, which rejects the same symbols)."""
    t = (ticker or "").strip().upper()
    if not t or t.startswith("^") or "=" in t or t.endswith("-USD"):
        return None
    if re.search(r"[-.]PR[A-Z]?$", t):
        return None
    if re.search(r"[-.](RT|WS|WSA|UN?)$", t):
        return None
    if "." in t and len(t.rsplit(".", 1)[-1]) > 2:
        return None
    # Yahoo class-share convention uses a dash; Alpaca wants a dot (BRK-B -> BRK.B).
    if "." not in t and "-" in t and t.count("-") == 1 and len(t.split("-")[1]) <= 2:
        t = t.replace("-", ".")
    return t


def _from_alpaca_equity(sym: str) -> str:
    """Alpaca equity symbol -> engine ticker (BRK.B -> BRK-B)."""
    if "." in sym and len(sym.rsplit(".", 1)[-1]) <= 2:
        return sym.replace(".", "-")
    return sym


def _run_cli(args: list[str]):
    """Run the alpaca CLI; return (parsed_json_or_None, stderr_text)."""
    try:
        out = subprocess.run(
            [CLI, "-q"] + args, capture_output=True, text=True,
            timeout=_TIMEOUT, env=os.environ,
        )
    except Exception as e:  # noqa: BLE001 - CLI missing/timeout -> treat as chunk failure
        logger.warning("close_proxy: CLI invocation failed (%s)", e)
        return None, str(e)
    if out.returncode != 0:
        logger.warning("close_proxy: CLI rc=%s stderr=%s", out.returncode, (out.stderr or "")[:200])
        return None, (out.stderr or "")
    s = (out.stdout or "").strip()
    if not s:
        return None, (out.stderr or "")
    # Parse the WHOLE document first. The previous implementation scanned for
    # the first '[' or '{' ANYWHERE and sliced from it, which silently broke
    # during RTH: a quote's condition list ("c": ["R"]) is the first '[' in an
    # object payload, so it sliced mid-object, failed, and returned None —
    # every snapshot chunk empty, CloseProxyError, signals abort. Measured
    # 2026-07-29 14:00Z on a live SPY/NVDA multi-snapshots response.
    try:
        return json.loads(s), (out.stderr or "")
    except Exception:  # noqa: BLE001
        pass
    # Tolerate a banner/preamble before the JSON body: try each candidate start
    # and keep going if one fails, instead of giving up on the first.
    for i in sorted(j for j in (s.find("{"), s.find("[")) if j > 0):
        try:
            return json.loads(s[i:]), (out.stderr or "")
        except Exception:  # noqa: BLE001
            continue
    return None, (out.stderr or "")


def _fetch_chunk(chunk: list[str]) -> dict:
    """Fetch one multi-snapshots chunk, evicting broker-named invalid
    symbols and retrying so one bad symbol cannot poison its chunk."""
    chunk = list(chunk)
    for _ in range(_MAX_INVALID_RETRIES):
        if not chunk:
            return {}
        res, err = _run_cli(["data", "multi-snapshots", "--symbols", ",".join(chunk)])
        if isinstance(res, dict):
            return res
        m = _INVALID_SYMBOL_RE.search(err or "")
        if not m or m.group(1) not in chunk:
            return {}
        bad = m.group(1)
        chunk.remove(bad)
        logger.warning("close_proxy: evicted invalid symbol %s from chunk, retrying", bad)
    logger.warning("close_proxy: chunk still failing after %d evictions — dropped",
                   _MAX_INVALID_RETRIES)
    return {}


def _price_from_snap(snap: dict, asof_str: str | None = None) -> float | None:
    """Best price from a snapshot node. With ``asof_str`` (YYYY-MM-DD), only
    nodes stamped that date qualify — stale last-trades are rejected. Trade
    timestamps are UTC ISO; at the ~15:00 ET chain the UTC and ET calendar
    dates coincide, so a plain prefix match is exact."""
    for top, key in (("latestTrade", "p"), ("minuteBar", "c"), ("dailyBar", "c")):
        node = snap.get(top) or {}
        v = node.get(key)
        if not v:
            continue
        if asof_str and not str(node.get("t", "")).startswith(asof_str):
            continue
        return float(v)
    return None


def fetch_close_proxy(universe, asof_date, min_coverage: float | None = None) -> dict:
    """Return {engine_ticker: latest_price} for the universe.

    asof_date: when given, prices must be stamped that date (same-day guard)
    and equity coverage below the floor raises. None = legacy best-effort.
    min_coverage: fraction of the SERVABLE equity subset that must price;
    default env OPENCLAW_CLOSE_PROXY_MIN_COVERAGE (0.90). Only enforced in
    asof mode.
    """
    equities: dict[str, str] = {}  # alpaca_sym -> engine_ticker
    cryptos: list[str] = []
    for tk in universe or []:
        a = _to_alpaca_equity(tk)
        if a:
            equities[a] = tk
        elif (tk or "").strip().upper().endswith("-USD"):
            cryptos.append(tk)

    asof_str = str(asof_date)[:10] if asof_date is not None else None
    out: dict[str, float] = {}
    n_equity_priced = 0

    syms = list(equities.keys())
    n_returned = 0
    for i in range(0, len(syms), _CHUNK):
        chunk = syms[i:i + _CHUNK]
        res = _fetch_chunk(chunk)
        n_returned += sum(1 for a in res if a in equities)
        for asym, snap in res.items():
            p = _price_from_snap(snap or {}, asof_str)
            if p is not None:
                out[equities.get(asym, _from_alpaca_equity(asym))] = p
                n_equity_priced += 1

    for tk in cryptos:
        pair = tk.strip().upper()[:-4] + "/USD"  # BTC-USD -> BTC/USD
        res, _err = _run_cli(["data", "crypto", "latest-trades", "--symbols", pair])
        price = None
        if isinstance(res, dict):
            node = res.get(pair) or (res.get("trades") or {}).get(pair) or {}
            price = node.get("p") if isinstance(node, dict) else None
        if price:
            out[tk] = float(price)

    if (equities or cryptos) and not out:
        raise CloseProxyError("close[t]-proxy snapshot fetch produced no prices")

    if asof_str and equities:
        floor = (min_coverage if min_coverage is not None
                 else float(os.environ.get("OPENCLAW_CLOSE_PROXY_MIN_COVERAGE", "0.60")))
        # Denominator = symbols the PROVIDER returned a snapshot for, not every
        # symbol we asked about. A 10-year price panel carries ~1.7k delisted
        # tickers the API simply omits; counting them made real coverage read
        # 85.8% on a healthy RTH day (2026-07-29) and would have aborted the
        # chain. Provider-returned-but-not-priced-today IS the signal worth
        # gating on: it means the snapshot service is degraded or the session
        # has no trades.
        denom = n_returned or len(equities)
        coverage = n_equity_priced / denom
        logger.info("close_proxy: %d/%d provider-returned equities priced (%.1f%%) "
                    "for %s (%d requested)",
                    n_equity_priced, denom, coverage * 100, asof_str, len(equities))
        if coverage < floor:
            raise CloseProxyError(
                f"close[t]-proxy coverage {coverage:.1%} of {denom} provider-returned "
                f"equities is below the {floor:.0%} floor for {asof_str} — refusing "
                f"to size the acting book off a hollow snapshot")
    return out


def fetch_open_prices(universe) -> dict:
    """Return {engine_ticker: today's OPEN price} (dailyBar.o) for equities/ETPs.

    Used by the dashboard-only SOD refresh — NOT a signal input. Best-effort:
    missing tickers omitted, never raises (dashboard cosmetics must not break
    anything)."""
    equities: dict[str, str] = {}
    for tk in universe or []:
        a = _to_alpaca_equity(tk)
        if a:
            equities[a] = tk
    out: dict[str, float] = {}
    syms = list(equities.keys())
    for i in range(0, len(syms), _CHUNK):
        res = _fetch_chunk(syms[i:i + _CHUNK])
        for asym, snap in res.items():
            o = ((snap or {}).get("dailyBar") or {}).get("o")
            if o:
                out[equities.get(asym, _from_alpaca_equity(asym))] = float(o)
    return out
