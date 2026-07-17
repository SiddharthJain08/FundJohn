"""SP-7 Phase A1 — market_cap = shares_outstanding × split-adjusted close.

The FMP profile source never delivered (403 / empty cache since inception —
see /root/universe_expansion_audit_2026-06-04.md §2). This module is the
prices_x_shares fallback the 2026-05-22 probe selected
(docs/archive/superpowers/specs/sp2-fmp-mktcap-probe.md: FALLBACK:prices_x_shares).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

SHARES_PARQUET = Path("/root/openclaw/data/master/shares_outstanding.parquet")
PRICES_PARQUET = Path("/root/openclaw/data/master/prices.parquet")
PRICE_STALENESS_DAYS = 10


def build_market_cap_lookup(
    symbols: list[str],
    as_of: date,
    *,
    shares_path=SHARES_PARQUET,
    prices_path=PRICES_PARQUET,
) -> dict[str, Optional[float]]:
    out: dict[str, Optional[float]] = {s: None for s in symbols}
    shares_path, prices_path = Path(shares_path), Path(prices_path)
    if not shares_path.exists() or not prices_path.exists():
        return out
    iso = as_of.isoformat()
    floor_iso = (as_of - timedelta(days=PRICE_STALENESS_DAYS)).isoformat()
    symset = set(symbols)

    sh = pd.read_parquet(shares_path, columns=["ticker", "asof_date", "shares"])
    sh = sh[sh.ticker.isin(symset) & (sh.asof_date.astype(str) <= iso)]
    latest_shares = (sh.sort_values("asof_date").groupby("ticker")["shares"].last())

    px = pd.read_parquet(prices_path, columns=["ticker", "date", "close"])
    px = px[px.ticker.isin(symset)
            & (px.date.astype(str) <= iso)
            & (px.date.astype(str) >= floor_iso)]
    latest_close = (px.sort_values("date").groupby("ticker")["close"].last())

    for s in symbols:
        sh_v = latest_shares.get(s)
        px_v = latest_close.get(s)
        if sh_v is not None and px_v is not None and pd.notna(sh_v) and pd.notna(px_v):
            out[s] = float(sh_v) * float(px_v)
    return out
