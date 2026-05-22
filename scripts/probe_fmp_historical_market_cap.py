#!/usr/bin/env python3
"""SP-2 Phase A precondition probe.

Calls FMP `historical-market-capitalization` for a representative ticker set
across multi-year windows on the Starter tier. Decides primary vs fallback
sourcing for the `market_cap` field on TickerMetadata.

Result is written to docs/superpowers/specs/sp2-fmp-mktcap-probe.md and
committed. Run ONCE per phase; delete the script after Phase A ships.
"""
from __future__ import annotations
import os, json, time, sys
import requests
from pathlib import Path
from datetime import date

API_KEY = os.environ["FMP_API_KEY"]
TICKERS = ["AAPL", "MSFT", "SMCI", "RIVN", "BRK-B"]
WINDOWS = [("2021-01-01", "2021-12-31"),
           ("2023-06-01", "2023-09-30"),
           ("2025-01-01", "2025-06-30")]

def probe(ticker, frm, to):
    url = (f"https://financialmodelingprep.com/api/v3/"
           f"historical-market-capitalization/{ticker}"
           f"?from={frm}&to={to}&apikey={API_KEY}")
    r = requests.get(url, timeout=10)
    return {
        "ticker": ticker,
        "window": f"{frm}..{to}",
        "status": r.status_code,
        "row_count": len(r.json()) if r.status_code == 200 else 0,
        "sample_row": (r.json()[0] if r.status_code == 200 and r.json() else None),
    }

def main():
    results = []
    for t in TICKERS:
        for frm, to in WINDOWS:
            results.append(probe(t, frm, to))
            time.sleep(0.3)  # 300 req/min Starter cap
    all_200 = all(r["status"] == 200 and r["row_count"] > 0 for r in results)
    decision = "PRIMARY:fmp_endpoint" if all_200 else "FALLBACK:prices_x_shares"
    out = Path("docs/superpowers/specs/sp2-fmp-mktcap-probe.md")
    out.write_text(
        f"# FMP historical-market-capitalization probe\n\n"
        f"**Date:** {date.today()}\n"
        f"**Decision:** {decision}\n\n"
        f"## Results\n\n```json\n{json.dumps(results, indent=2)}\n```\n"
    )
    print(decision)

if __name__ == "__main__":
    main()
