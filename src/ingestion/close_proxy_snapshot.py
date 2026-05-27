"""close_proxy_snapshot.py — same-day close[t] price proxy for the signal engine.

Fetches the latest (~3 PM) price per universe ticker so the daily compute can
inject a today-dated row into the price panel, making live signal generation
mirror the backtests' close[t] decision. Read-only: never writes the master
parquet (the post-close EOD refresh writes the real close[t]).

Equities/ETPs: `alpaca data multi-snapshots` (latestTrade.p, falling back to
minuteBar.c then dailyBar.c). Crypto (BASE-USD): `alpaca data crypto
latest-trades`. Indices/futures/FX are skipped (no snapshot).

Raises CloseProxyError on TOTAL failure (nothing fetched). The caller
(engine.load_prices) must let this propagate so the signals step aborts rather
than emitting an empty signal set that would orphan-close the whole book.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

CLI = os.environ.get("ALPACA_CLI_BIN", "/root/go/bin/alpaca")
_CHUNK = 50
_TIMEOUT = 40


class CloseProxyError(RuntimeError):
    """Raised when the close[t]-proxy snapshot cannot be fetched at all."""


def _to_alpaca_equity(ticker: str) -> str | None:
    """engine ticker -> Alpaca equity symbol; None for non-equity symbols
    (indices `^`, futures/FX `=`, crypto `-USD`) which have no equity snapshot."""
    t = (ticker or "").strip().upper()
    if not t or t.startswith("^") or "=" in t or t.endswith("-USD"):
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
    try:
        out = subprocess.run(
            [CLI, "-q"] + args, capture_output=True, text=True,
            timeout=_TIMEOUT, env=os.environ,
        )
    except Exception as e:  # noqa: BLE001 - CLI missing/timeout -> treat as chunk failure
        logger.warning("close_proxy: CLI invocation failed (%s)", e)
        return None
    if out.returncode != 0:
        logger.warning("close_proxy: CLI rc=%s stderr=%s", out.returncode, (out.stderr or "")[:200])
        return None
    s = out.stdout or ""
    for ch in "[{":
        i = s.find(ch)
        if i >= 0:
            try:
                return json.loads(s[i:])
            except Exception:  # noqa: BLE001
                return None
    return None


def _price_from_snap(snap: dict) -> float | None:
    for top, key in (("latestTrade", "p"), ("minuteBar", "c"), ("dailyBar", "c")):
        v = (snap.get(top) or {}).get(key)
        if v:
            return float(v)
    return None


def fetch_close_proxy(universe, asof_date) -> dict:
    """Return {engine_ticker: latest_price} for the universe.

    Partial results are fine (tickers missing a snapshot are omitted).
    Raises CloseProxyError only if a fetch was attempted and produced nothing.
    """
    equities: dict[str, str] = {}  # alpaca_sym -> engine_ticker
    cryptos: list[str] = []
    for tk in universe or []:
        a = _to_alpaca_equity(tk)
        if a:
            equities[a] = tk
        elif (tk or "").strip().upper().endswith("-USD"):
            cryptos.append(tk)

    out: dict[str, float] = {}

    syms = list(equities.keys())
    for i in range(0, len(syms), _CHUNK):
        chunk = syms[i:i + _CHUNK]
        res = _run_cli(["data", "multi-snapshots", "--symbols", ",".join(chunk)])
        if not isinstance(res, dict):
            continue
        for asym, snap in res.items():
            p = _price_from_snap(snap or {})
            if p is not None:
                out[equities.get(asym, _from_alpaca_equity(asym))] = p

    for tk in cryptos:
        pair = tk.strip().upper()[:-4] + "/USD"  # BTC-USD -> BTC/USD
        res = _run_cli(["data", "crypto", "latest-trades", "--symbols", pair])
        price = None
        if isinstance(res, dict):
            node = res.get(pair) or (res.get("trades") or {}).get(pair) or {}
            price = node.get("p") if isinstance(node, dict) else None
        if price:
            out[tk] = float(price)

    if (equities or cryptos) and not out:
        raise CloseProxyError("close[t]-proxy snapshot fetch produced no prices")
    return out
