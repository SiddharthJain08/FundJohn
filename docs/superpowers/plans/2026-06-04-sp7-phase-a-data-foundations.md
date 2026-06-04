# SP-7 Phase A — Data Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the data foundations for whole-market universe expansion: EDGAR-sourced market_cap (revives r1000/r3000/cap predicates), point-in-time listed dates, a canonical split-adjusted price convention, a gated engine universe clamp, and the liquid-5k price backfill machinery.

**Architecture:** Five sub-systems (A1–A5 in the spec), each gated/inert until the operator flips or runs them. New EDGAR shares ingester writes an append-only parquet; a market_cap lookup joins it with split-adjusted closes; the existing metadata writers consume it via their pre-built `market_cap_lookup` seams. The engine clamp MUST be merged + flipped before any broad backfill runs (ordering invariant — 2026-06-04 lesson: the live engine's universe is literally prices.parquet).

**Tech Stack:** Python 3.13 (psycopg2, pandas, pyarrow, urllib), Alpaca CLI (`/root/go/bin/alpaca`), SEC EDGAR companyfacts JSON API, systemd user timers, pytest.

**Spec:** `docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md`
**Audit evidence:** `/root/universe_expansion_audit_2026-06-04.md`

**Branch:** `feat/sp7-phase-a-data-foundations` off the live branch tip (`feat/sp6-phase-a-eod-open-execution` @ `eb91fba` or later). Use a worktree (superpowers:using-git-worktrees). NEVER `git reset --hard` — live-critical uncommitted files exist on the main checkout (`manifest.json`, `strategy_signatures.json`, `run_sentiment_step.py`).

**Hard constraints (apply to every task):**
- `data/master/` is append-only (CLAUDE.md core invariant). The Phase-B backfill driver is the sole sanctioned exception path.
- 8GB/2-core box: anything heavy runs `nice -n 19`, sequential, resumable.
- Every new behavior is gated default-OFF or operator-invoked.
- Env for Python DB access: `POSTGRES_URI` from `/root/openclaw/.env` (do NOT `source` the whole .env — unquoted parens break bash; grep specific keys).
- Postgres access from shell: `docker exec openclaw-postgres psql -U openclaw -d openclaw`.

---

### Task 0: Grounding snapshot + worktree

**Files:**
- Create: `docs/superpowers/specs/2026-06-04-sp7-phase-a-grounding.md`

- [ ] **Step 1: Create worktree branch**

```bash
cd /root/openclaw
git worktree add /tmp/wt-sp7-phase-a -b feat/sp7-phase-a-data-foundations
cd /tmp/wt-sp7-phase-a
```

- [ ] **Step 2: Verify live-state facts that tasks depend on**

```bash
# (a) next migration number is free
ls src/database/migrations/ | sort -t_ -k1 -n | tail -3
# Expected: ...128_option_hedge_ledger.sql is the max → Task 4 uses 129. If 129 exists, renumber Task 4.

# (b) CIK map present + format
python3 -c "import json; m=json.load(open('/root/openclaw/data/master/_sec_ticker_cik.json')); print(len(m), list(m.items())[:2])"
# Expected: dict ticker->zero-padded-CIK-string, e.g. ('NVDA', '0001045810')

# (c) corporate_actions split rows exist
python3 -c "import pandas as pd; df=pd.read_parquet('/root/openclaw/data/master/corporate_actions.parquet', columns=['action_type']); print(df.action_type.value_counts().to_dict())"
# Expected: contains 'forward_split' (and 'cash_dividend')

# (d) daily metadata chain: refresh_tradable_universe chains run_ticker_metadata_step
grep -n "run_ticker_metadata_step" src/maintenance/refresh_tradable_universe.py
# Expected: subprocess call ~line 277-282

# (e) collector adjustment flag
grep -n "'--adjustment', 'all'" src/pipeline/collector.js || grep -n '"--adjustment", "all"' src/pipeline/collector.js
# Expected: one hit ~line 589 inside fillPricesAlpaca
```

- [ ] **Step 3: Write the grounding snapshot doc** — record each command + output verbatim in `docs/superpowers/specs/2026-06-04-sp7-phase-a-grounding.md`. Any mismatch = STOP, update this plan before dispatching further tasks (D1-schema-drift lesson).

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-04-sp7-phase-a-grounding.md
git commit -m "docs(sp7a): grounding snapshot for Phase A"
```

---

### Task 1: EDGAR shares-outstanding ingester

**Files:**
- Create: `src/pipeline/backfillers/edgar_shares.py`
- Create: `tests/pipeline/test_edgar_shares.py` (create `tests/pipeline/__init__.py` if absent)
- Create: `tests/fixtures/edgar_companyfacts_sample.json`

**Contract:** fetch SEC companyfacts per CIK, extract the shares-outstanding time-series, append net-new `(ticker, asof_date)` rows to `data/master/shares_outstanding.parquet` (NEW member of the NEVER-DELETE family). Entity-level shares (multi-class entities like GOOG/GOOGL report one entity total — documented caveat, fine for cap-tier ranking).

- [ ] **Step 1: Write the fixture** — `tests/fixtures/edgar_companyfacts_sample.json`:

```json
{
  "cik": 1045810,
  "entityName": "NVIDIA CORP",
  "facts": {
    "dei": {
      "EntityCommonStockSharesOutstanding": {
        "units": {
          "shares": [
            {"end": "2024-01-26", "val": 2464000000, "form": "10-K", "filed": "2024-02-21"},
            {"end": "2024-04-26", "val": 2462000000, "form": "10-Q", "filed": "2024-05-29"},
            {"end": "2024-04-26", "val": 2462500000, "form": "10-Q/A", "filed": "2024-06-15"}
          ]
        }
      }
    },
    "us-gaap": {
      "CommonStockSharesOutstanding": {
        "units": {
          "shares": [
            {"end": "2023-10-29", "val": 2466000000, "form": "10-Q", "filed": "2023-11-21"}
          ]
        }
      }
    }
  }
}
```

- [ ] **Step 2: Write failing tests** — `tests/pipeline/test_edgar_shares.py`:

```python
import json
from pathlib import Path
import pandas as pd
import pytest

from src.pipeline.backfillers.edgar_shares import (
    parse_shares_series, merge_append_only,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "edgar_companyfacts_sample.json"


def _facts():
    return json.loads(FIXTURE.read_text())


def test_parse_extracts_dei_and_gaap_series():
    rows = parse_shares_series("NVDA", _facts())
    dates = {r["asof_date"] for r in rows}
    # 3 distinct end-dates: 2023-10-29 (gaap), 2024-01-26, 2024-04-26 (dei)
    assert dates == {"2023-10-29", "2024-01-26", "2024-04-26"}


def test_parse_dedupes_same_end_date_preferring_latest_filed():
    rows = parse_shares_series("NVDA", _facts())
    apr = [r for r in rows if r["asof_date"] == "2024-04-26"]
    assert len(apr) == 1
    assert apr[0]["shares"] == 2462500000  # the 10-Q/A filed later wins


def test_parse_rejects_implausible_units():
    facts = _facts()
    facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"].append(
        {"end": "2024-07-26", "val": 12, "form": "10-Q", "filed": "2024-08-20"}
    )  # 12 shares — implausible, must be dropped
    rows = parse_shares_series("NVDA", facts)
    assert "2024-07-26" not in {r["asof_date"] for r in rows}


def test_merge_append_only_never_drops_or_mutates(tmp_path):
    pq = tmp_path / "shares_outstanding.parquet"
    existing = pd.DataFrame([
        {"ticker": "NVDA", "asof_date": "2024-01-26", "shares": 2464000000,
         "form": "10-K", "filed": "2024-02-21"},
    ])
    existing.to_parquet(pq, index=False)
    new_rows = [
        # duplicate (ticker, asof_date) with DIFFERENT value — must NOT overwrite
        {"ticker": "NVDA", "asof_date": "2024-01-26", "shares": 1,
         "form": "10-K", "filed": "2024-02-21"},
        {"ticker": "NVDA", "asof_date": "2024-04-26", "shares": 2462500000,
         "form": "10-Q/A", "filed": "2024-06-15"},
    ]
    added = merge_append_only(pq, new_rows)
    out = pd.read_parquet(pq)
    assert added == 1
    assert len(out) == 2
    jan = out[out.asof_date == "2024-01-26"].iloc[0]
    assert jan.shares == 2464000000  # original row untouched
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /tmp/wt-sp7-phase-a && nice -n 19 python3 -m pytest tests/pipeline/test_edgar_shares.py -v`
Expected: FAIL with `ModuleNotFoundError: src.pipeline.backfillers.edgar_shares`

- [ ] **Step 4: Implement** — `src/pipeline/backfillers/edgar_shares.py`:

```python
"""SP-7 Phase A1 — EDGAR shares-outstanding ingester.

Fetches SEC companyfacts per CIK and appends shares-outstanding rows to
data/master/shares_outstanding.parquet (append-only — member of the
NEVER-DELETE family; existing (ticker, asof_date) rows are never mutated).

Source tags: dei.EntityCommonStockSharesOutstanding (primary, cover-page
entity total) + us-gaap.CommonStockSharesOutstanding (fallback/older filings).
Multi-class entities report one entity-level total — adequate for cap-tier
ranking (documented caveat in the SP-7 spec §3 A1).

SEC fair-access: <=10 req/s, descriptive User-Agent REQUIRED (default
python-urllib UA gets Cloudflare-1010-style blocks — same lesson as the
Discord webhook 403s, memory: reference_discord_urllib_cloudflare_ua).

Usage:
  POSTGRES_URI=... python3 -m src.pipeline.backfillers.edgar_shares \
      [--tickers NVDA,AAPL] [--universe-file data/.backfill_universe_v2.txt] \
      [--covered-only] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path("/root/openclaw")
SHARES_PARQUET = ROOT / "data" / "master" / "shares_outstanding.parquet"
CIK_MAP = ROOT / "data" / "master" / "_sec_ticker_cik.json"
PRICES_PARQUET = ROOT / "data" / "master" / "prices.parquet"
UA = "OpenClaw research (siddharthj1908@gmail.com)"
MIN_SHARES, MAX_SHARES = 1e6, 2e11  # unit-sanity gates (spec §7)

SCHEMA_COLUMNS = ["ticker", "asof_date", "shares", "form", "filed", "fetched_at"]


def fetch_companyfacts(cik_padded: str) -> dict:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def parse_shares_series(ticker: str, facts: dict) -> list[dict]:
    """Extract deduped shares series. Latest `filed` wins per asof_date.

    Returns [] when the entity reports no shares facts (funds, some ADRs).
    """
    entries: list[dict] = []
    for taxonomy, tag in (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ):
        units = (
            facts.get("facts", {}).get(taxonomy, {}).get(tag, {}).get("units", {})
        )
        for item in units.get("shares", []):
            end, val = item.get("end"), item.get("val")
            if not end or val is None:
                continue
            if not (MIN_SHARES <= float(val) <= MAX_SHARES):
                continue
            entries.append({
                "ticker": ticker,
                "asof_date": end,
                "shares": float(val),
                "form": item.get("form") or "",
                "filed": item.get("filed") or "",
            })
    # Dedupe per asof_date: prefer the latest `filed` (amendments supersede).
    best: dict[str, dict] = {}
    for e in entries:
        cur = best.get(e["asof_date"])
        if cur is None or e["filed"] > cur["filed"]:
            best[e["asof_date"]] = e
    return sorted(best.values(), key=lambda r: r["asof_date"])


def merge_append_only(parquet_path, new_rows: list[dict]) -> int:
    """Append rows whose (ticker, asof_date) is not already present.

    Existing rows are NEVER mutated or dropped (NEVER-DELETE invariant).
    Atomic: write tmp file then rename. Returns number of rows added.
    """
    parquet_path = Path(parquet_path)
    fetched_at = datetime.now(timezone.utc).isoformat()
    new_df = pd.DataFrame([{**r, "fetched_at": fetched_at} for r in new_rows])
    if parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
        if not new_df.empty:
            key = existing.ticker + "|" + existing.asof_date.astype(str)
            new_key = new_df.ticker + "|" + new_df.asof_date.astype(str)
            new_df = new_df[~new_key.isin(set(key))]
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    if "fetched_at" not in combined.columns:
        combined["fetched_at"] = fetched_at
    tmp = parquet_path.with_suffix(".parquet.tmp")
    combined.to_parquet(tmp, index=False)
    os.replace(tmp, parquet_path)
    return len(new_df)


def _load_universe(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.universe_file:
        return [l.strip() for l in Path(args.universe_file).read_text().splitlines() if l.strip()]
    if args.covered_only:
        df = pd.read_parquet(PRICES_PARQUET, columns=["ticker"])
        return sorted(df.ticker.unique().tolist())
    raise SystemExit("one of --tickers / --universe-file / --covered-only required")


def main() -> int:
    ap = argparse.ArgumentParser(prog="edgar_shares")
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--covered-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cik_map: dict[str, str] = json.loads(CIK_MAP.read_text())
    universe = _load_universe(args)
    total_added, no_cik, no_facts = 0, 0, 0
    for i, ticker in enumerate(universe):
        # CIK map keys use dot share-class form sometimes; try both.
        cik = cik_map.get(ticker) or cik_map.get(ticker.replace("-", "."))
        if not cik:
            no_cik += 1
            continue
        try:
            facts = fetch_companyfacts(cik)
        except Exception as e:
            sys.stderr.write(f"[edgar] {ticker}: fetch failed: {e}\n")
            time.sleep(1.0)
            continue
        rows = parse_shares_series(ticker, facts)
        if not rows:
            no_facts += 1
        elif args.dry_run:
            print(f"[dry-run] {ticker}: {len(rows)} share rows")
        else:
            total_added += merge_append_only(SHARES_PARQUET, rows)
        if (i + 1) % 250 == 0:
            print(f"[edgar] {i+1}/{len(universe)} done, +{total_added} rows")
        time.sleep(0.12)  # ~8 req/s, under SEC's 10/s ceiling
    print(f"[edgar] DONE universe={len(universe)} added={total_added} "
          f"no_cik={no_cik} no_facts={no_facts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_edgar_shares.py -v`
Expected: 4 PASS

- [ ] **Step 6: Live smoke (read-only against SEC, writes to a tmp path)**

```bash
cd /tmp/wt-sp7-phase-a && nice -n 19 python3 - << 'EOF'
from src.pipeline.backfillers.edgar_shares import fetch_companyfacts, parse_shares_series
import json
cik = json.load(open('/root/openclaw/data/master/_sec_ticker_cik.json'))['NVDA']
rows = parse_shares_series('NVDA', fetch_companyfacts(cik))
print(len(rows), rows[-1])
assert rows and 1e9 < rows[-1]['shares'] < 1e11
EOF
```
Expected: dozens of rows; latest NVDA shares ~2.4e9.

- [ ] **Step 7: Commit**

```bash
git add src/pipeline/backfillers/edgar_shares.py tests/pipeline/test_edgar_shares.py tests/fixtures/edgar_companyfacts_sample.json tests/pipeline/__init__.py
git commit -m "feat(sp7a): EDGAR shares-outstanding ingester (append-only shares_outstanding.parquet)"
```

---

### Task 2: market_cap lookup module

**Files:**
- Create: `src/pipeline/market_cap_lookup.py`
- Create: `tests/pipeline/test_market_cap_lookup.py`

**Contract:** `build_market_cap_lookup(symbols, as_of)` → `dict[str, float|None]`: latest shares row with `asof_date <= as_of` (carry-forward, no staleness limit — filings are quarterly) × latest close with `date <= as_of` AND within 10 calendar days (stale price → None, not a wrong cap).

- [ ] **Step 1: Write failing tests** — `tests/pipeline/test_market_cap_lookup.py`:

```python
import pandas as pd
import pytest
from datetime import date

from src.pipeline.market_cap_lookup import build_market_cap_lookup


@pytest.fixture
def stores(tmp_path):
    shares = tmp_path / "shares.parquet"
    prices = tmp_path / "prices.parquet"
    pd.DataFrame([
        {"ticker": "NVDA", "asof_date": "2026-01-26", "shares": 2.4e9},
        {"ticker": "NVDA", "asof_date": "2026-04-26", "shares": 2.5e9},
        {"ticker": "OLDCO", "asof_date": "2020-02-01", "shares": 1.0e8},
    ]).to_parquet(shares, index=False)
    pd.DataFrame([
        {"ticker": "NVDA", "date": "2026-06-01", "close": 100.0},
        {"ticker": "NVDA", "date": "2026-06-03", "close": 110.0},
        {"ticker": "OLDCO", "date": "2026-01-02", "close": 5.0},  # stale vs 06-03
    ]).to_parquet(prices, index=False)
    return shares, prices


def test_latest_shares_times_latest_close(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["NVDA"], date(2026, 6, 3),
                                  shares_path=shares, prices_path=prices)
    assert out["NVDA"] == pytest.approx(2.5e9 * 110.0)


def test_shares_carry_forward_pit(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["NVDA"], date(2026, 6, 1),
                                  shares_path=shares, prices_path=prices)
    # as_of 06-01: latest shares row <= 06-01 is the 04-26 filing; close = 100
    assert out["NVDA"] == pytest.approx(2.5e9 * 100.0)


def test_stale_price_yields_none(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["OLDCO"], date(2026, 6, 3),
                                  shares_path=shares, prices_path=prices)
    assert out["OLDCO"] is None  # close is >10 days old


def test_missing_ticker_yields_none(stores):
    shares, prices = stores
    out = build_market_cap_lookup(["GHOST"], date(2026, 6, 3),
                                  shares_path=shares, prices_path=prices)
    assert out["GHOST"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_market_cap_lookup.py -v`
Expected: FAIL ModuleNotFoundError

- [ ] **Step 3: Implement** — `src/pipeline/market_cap_lookup.py`:

```python
"""SP-7 Phase A1 — market_cap = shares_outstanding × split-adjusted close.

The FMP profile source never delivered (403 / empty cache since inception —
see /root/universe_expansion_audit_2026-06-04.md §2). This module is the
prices_x_shares fallback the 2026-05-22 probe selected
(docs/superpowers/specs/sp2-fmp-mktcap-probe.md: FALLBACK:prices_x_shares).
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_market_cap_lookup.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/market_cap_lookup.py tests/pipeline/test_market_cap_lookup.py
git commit -m "feat(sp7a): market_cap lookup (shares x split-adjusted close, PIT carry-forward)"
```

---

### Task 3: Wire market_cap into the daily writer + historical builder

**Files:**
- Modify: `src/pipeline/ticker_metadata_writer.py:73-135` (`build_metadata_rows` gains `market_cap_lookup` param)
- Modify: `src/pipeline/run_ticker_metadata_step.py:44-56` (`main` builds + passes the lookup)
- Modify: `scripts/backfill_universe_5y.py` `_run_metadata` (~line 636: pass per-month lookup into `build_month_snapshot`)
- Create: `tests/pipeline/test_metadata_market_cap_wiring.py`

- [ ] **Step 1: Write failing tests** — `tests/pipeline/test_metadata_market_cap_wiring.py`:

```python
from datetime import date

from src.pipeline.ticker_metadata_writer import build_metadata_rows
from src.pipeline.backfillers.universe_metadata import rank_in_r1000_r3000


ALPACA_ROWS = [
    {"symbol": "NVDA", "asset_class": "us_equity", "exchange": "NASDAQ",
     "status": "active", "tradable": True, "shortable": True,
     "fractionable": True, "easy_to_borrow": True,
     "first_seen_at": None, "last_seen_at": None},
    {"symbol": "TINY", "asset_class": "us_equity", "exchange": "NYSE",
     "status": "active", "tradable": True, "shortable": False,
     "fractionable": False, "easy_to_borrow": False,
     "first_seen_at": None, "last_seen_at": None},
]


def test_lookup_overrides_fmp_and_feeds_ranking():
    rows = build_metadata_rows(
        date(2026, 6, 4), ALPACA_ROWS,
        fmp_profile={"NVDA": {"mktCap": 1.0}},   # stale/wrong FMP value
        prices_parquet={}, options_cache={}, source_tag="test",
        market_cap_lookup={"NVDA": 3.0e12, "TINY": 5.0e8},
    )
    by = {r["symbol"]: r for r in rows}
    assert by["NVDA"]["market_cap"] == 3.0e12      # lookup wins over FMP
    assert by["TINY"]["market_cap"] == 5.0e8
    # ranking self-heals: both rank into r3000, NVDA into r1000
    assert by["NVDA"]["in_r1000"] is True
    assert by["TINY"]["in_r3000"] is True


def test_absent_lookup_preserves_legacy_fmp_path():
    rows = build_metadata_rows(
        date(2026, 6, 4), ALPACA_ROWS,
        fmp_profile={"NVDA": {"mktCap": 7.0}},
        prices_parquet={}, options_cache={}, source_tag="test",
    )
    by = {r["symbol"]: r for r in rows}
    assert by["NVDA"]["market_cap"] == 7.0          # byte-identical legacy
    assert by["TINY"]["market_cap"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_metadata_market_cap_wiring.py -v`
Expected: FAIL `TypeError: build_metadata_rows() got an unexpected keyword argument 'market_cap_lookup'`

- [ ] **Step 3: Implement writer change** — in `ticker_metadata_writer.py`, add the keyword param and one resolution line. Signature becomes:

```python
def build_metadata_rows(
    snapshot_date,
    alpaca_rows,
    fmp_profile: dict[str, dict],
    prices_parquet: dict[str, dict],
    options_cache: dict[str, bool],
    source_tag: str,
    market_cap_lookup: dict[str, float | None] | None = None,
) -> list[dict]:
```

and inside the per-symbol loop, replace the existing `"market_cap": p.get("mktCap"),` line with:

```python
            "market_cap": (
                market_cap_lookup.get(sym)
                if market_cap_lookup is not None and market_cap_lookup.get(sym) is not None
                else p.get("mktCap")
            ),
```

(Keep everything else — the docstring gains one line describing the priority: lookup > FMP mktCap > None.)

- [ ] **Step 4: Wire the daily step** — in `run_ticker_metadata_step.py` `main()`, after `prices_parquet = adv_usd_from_parquet(symbols)` add:

```python
    from src.pipeline.market_cap_lookup import build_market_cap_lookup
    market_caps = build_market_cap_lookup(symbols, today)
```

and pass `market_cap_lookup=market_caps` to `build_metadata_rows(...)`.

- [ ] **Step 5: Wire the historical builder** — in `scripts/backfill_universe_5y.py` `_run_metadata`, replace the bare `df = build_month_snapshot(snapshot_date, universe, pg)` call with:

```python
            from src.pipeline.market_cap_lookup import build_market_cap_lookup
            mcaps = build_market_cap_lookup(list(universe), snapshot_date)
            df = build_month_snapshot(
                snapshot_date, universe, pg, market_cap_lookup=mcaps,
            )
```

(`build_month_snapshot` already accepts `market_cap_lookup` — universe_metadata.py:290. No change to that module.)

- [ ] **Step 6: Run tests + regression**

Run: `nice -n 19 python3 -m pytest tests/pipeline/ -v`
Expected: all PASS (new + Task 1/2 tests).

- [ ] **Step 7: Commit**

```bash
git add src/pipeline/ticker_metadata_writer.py src/pipeline/run_ticker_metadata_step.py scripts/backfill_universe_5y.py tests/pipeline/test_metadata_market_cap_wiring.py
git commit -m "feat(sp7a): wire market_cap lookup into daily writer + historical builder (r1000/r3000 self-heal)"
```

---

### Task 4: Point-in-time listed dates

**Files:**
- Create: `src/database/migrations/129_listed_date.sql` (renumber if Task 0 found 129 taken)
- Create: `scripts/probe_listed_dates.py`
- Modify: `src/pipeline/backfillers/universe_metadata.py:192-230` (`_alpaca_status_batch` PIT filter)
- Create: `tests/pipeline/test_listed_date_pit.py`

- [ ] **Step 1: Write the migration** — `src/database/migrations/129_listed_date.sql`:

```sql
-- SP-7 Phase A2: true listing dates for point-in-time universe membership.
-- first_seen_at is refresh-log-derived (~2026-05-14 for everything) and unusable
-- for historical PIT filters. listed_date = earliest Alpaca daily bar.
ALTER TABLE alpaca_tradable_universe
    ADD COLUMN IF NOT EXISTS listed_date DATE;
```

- [ ] **Step 2: Write failing PIT tests** — `tests/pipeline/test_listed_date_pit.py`:

```python
from datetime import date
from unittest.mock import MagicMock

from src.pipeline.backfillers.universe_metadata import _alpaca_status_batch


def _cursor_returning(rows, cols):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.description = [MagicMock(name=c) for c in cols]
    for d, c in zip(cur.description, cols):
        d.name = c
    pg = MagicMock()
    pg.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    pg.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pg


COLS = ["symbol", "asset_class", "exchange", "status", "tradable", "shortable",
        "fractionable", "easy_to_borrow", "first_seen_at", "last_seen_at",
        "listed_date"]


def test_listed_date_governs_pit_when_present():
    # DELL listed 2021-01-04 but first_seen 2026-05-14 → must be PRESENT for 2023 snapshots
    rows = [("DELL", "us_equity", "NYSE", "active", True, True, True, True,
             date(2026, 5, 14), None, date(2021, 1, 4))]
    out = _alpaca_status_batch(["DELL"], date(2023, 6, 30), _cursor_returning(rows, COLS))
    assert "DELL" in out


def test_falls_back_to_first_seen_when_listed_date_null():
    rows = [("NEWCO", "us_equity", "NYSE", "active", True, True, True, True,
             date(2026, 5, 14), None, None)]
    out = _alpaca_status_batch(["NEWCO"], date(2023, 6, 30), _cursor_returning(rows, COLS))
    assert "NEWCO" not in out  # legacy behavior preserved when listed_date missing


def test_not_yet_listed_excluded():
    rows = [("GEV", "us_equity", "NYSE", "active", True, True, True, True,
             date(2026, 5, 14), None, date(2024, 4, 2))]
    out = _alpaca_status_batch(["GEV"], date(2023, 6, 30), _cursor_returning(rows, COLS))
    assert "GEV" not in out
```

- [ ] **Step 3: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_listed_date_pit.py -v`
Expected: FAIL (SELECT has no listed_date column / filter uses first_seen only)

- [ ] **Step 4: Implement the PIT switch** — in `_alpaca_status_batch`: add `listed_date` to the SELECT column list, and replace the first_seen comparison with:

```python
            first_seen = d.get("first_seen_at")
            listed = d.get("listed_date")
            if isinstance(first_seen, datetime):
                first_seen = first_seen.date()
            effective_listing = listed or first_seen   # PIT: listed_date wins
            if effective_listing and effective_listing > on:
                continue  # not yet listed on the snapshot date
```

(Keep the `last_seen_at < on → status='inactive'` branch unchanged.)

- [ ] **Step 5: Run tests to verify pass**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_listed_date_pit.py tests/pipeline/ -v`
Expected: PASS (and Task 1-3 tests still green)

- [ ] **Step 6: Write the probe** — `scripts/probe_listed_dates.py`:

```python
"""SP-7 Phase A2 — one-shot listed_date probe.

For each alpaca_tradable_universe row with listed_date IS NULL (optionally
scoped --tickers/--universe-file), fetch the EARLIEST Alpaca daily bar and
write its date into listed_date. Resumable by construction (NULL-scoped).

Usage: POSTGRES_URI=... nice -n 19 python3 scripts/probe_listed_dates.py \
           [--tickers A,B] [--universe-file PATH] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

ALPACA_BIN = "/root/go/bin/alpaca"


def earliest_bar_date(symbol: str) -> str | None:
    args = [ALPACA_BIN, "data", "bars", "--symbol", symbol,
            "--start", "2000-01-03", "--end", "2026-12-31",
            "--timeframe", "1Day", "--adjustment", "split", "--limit", "1"]
    res = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:200])
    bars = (json.loads(res.stdout) or {}).get("bars") or []
    if not bars:
        return None
    return (bars[0].get("t") or "")[:10] or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    scope = None
    if args.tickers:
        scope = [t.strip().upper() for t in args.tickers.split(",")]
    elif args.universe_file:
        scope = [l.strip() for l in Path(args.universe_file).read_text().splitlines() if l.strip()]

    pg = psycopg2.connect(os.environ["POSTGRES_URI"])
    with pg.cursor() as cur:
        if scope:
            cur.execute("SELECT symbol FROM alpaca_tradable_universe "
                        "WHERE listed_date IS NULL AND symbol = ANY(%s) ORDER BY symbol",
                        (scope,))
        else:
            cur.execute("SELECT symbol FROM alpaca_tradable_universe "
                        "WHERE listed_date IS NULL ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]
    if args.limit:
        symbols = symbols[: args.limit]

    done = failed = empty = 0
    for sym in symbols:
        try:
            d = earliest_bar_date(sym)
        except Exception as e:
            sys.stderr.write(f"[probe] {sym}: {e}\n")
            failed += 1
            time.sleep(1.0)
            continue
        if d is None:
            empty += 1
        else:
            with pg.cursor() as cur:
                cur.execute("UPDATE alpaca_tradable_universe SET listed_date=%s "
                            "WHERE symbol=%s", (d, sym))
            pg.commit()
            done += 1
        if (done + failed + empty) % 250 == 0:
            print(f"[probe] {done+failed+empty}/{len(symbols)} done={done}")
        time.sleep(0.05)
    print(f"[probe] DONE total={len(symbols)} set={done} empty={empty} failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Apply migration + smoke the probe on 3 tickers (live DB write — within Phase A operator approval)**

```bash
docker exec -i openclaw-postgres psql -U openclaw -d openclaw < src/database/migrations/129_listed_date.sql
set -a && . <(grep -E '^POSTGRES_URI=' /root/openclaw/.env) && set +a
nice -n 19 python3 scripts/probe_listed_dates.py --tickers DELL,GEV,KVUE
docker exec openclaw-postgres psql -U openclaw -d openclaw -t -c \
  "SELECT symbol, listed_date FROM alpaca_tradable_universe WHERE symbol IN ('DELL','GEV','KVUE');"
```
Expected: DELL ≈ 2016-08-17 (re-IPO; any pre-2021 date acceptable), GEV ≈ 2024-03/04, KVUE ≈ 2023-05.

- [ ] **Step 8: Commit**

```bash
git add src/database/migrations/129_listed_date.sql scripts/probe_listed_dates.py src/pipeline/backfillers/universe_metadata.py tests/pipeline/test_listed_date_pit.py
git commit -m "feat(sp7a): point-in-time listed_date (migration 129 + probe + PIT filter switch)"
```

---

### Task 5: Engine universe clamp (MUST merge + flip BEFORE Task 8's backfill runs)

**Files:**
- Create: `src/execution/universe_clamp.py`
- Modify: `src/execution/engine.py:1392-1409` (fallback block calls the clamp)
- Create: `tests/execution/test_universe_clamp.py`

**Contract:** with `OPENCLAW_ENGINE_UNIVERSE_CLAMP=<predicate>` set, the engine's parquet-fallback universe keeps only equity tickers passing the named predicate — **but every ticker that is NOT a categorized us_equity common name passes through untouched** (SPY/QQQ/sector ETFs/VIX/BTC-USD/^GSPC: regime models and S_btc_momentum depend on them). Unset gate = byte-identical current behavior.

- [ ] **Step 1: Write failing tests** — `tests/execution/test_universe_clamp.py`:

```python
from datetime import date

from src.execution.universe_clamp import clamp_universe


META = {
    # symbol -> (asset_class, in_sp500)  — minimal metadata view
    "DELL": ("us_equity", True),
    "RDDT": ("us_equity", False),   # liquid but not SP500
    "SPY":  ("us_equity", False),   # ETF — must pass through via category
}
EQUITY_CATEGORY = {"DELL": "equity", "RDDT": "equity", "SPY": "etf"}


def fake_meta_fetch():
    return META


def fake_category_fetch():
    return EQUITY_CATEGORY


def test_clamp_off_is_identity(monkeypatch):
    monkeypatch.delenv("OPENCLAW_ENGINE_UNIVERSE_CLAMP", raising=False)
    u = ["DELL", "RDDT", "SPY", "BTC-USD"]
    assert clamp_universe(u, fake_meta_fetch, fake_category_fetch) == u


def test_clamp_sp500_filters_equities_only(monkeypatch):
    monkeypatch.setenv("OPENCLAW_ENGINE_UNIVERSE_CLAMP", "sp500")
    u = ["DELL", "RDDT", "SPY", "BTC-USD", "VIX"]
    out = clamp_universe(u, fake_meta_fetch, fake_category_fetch)
    assert "DELL" in out          # sp500 equity passes
    assert "RDDT" not in out      # non-sp500 equity clamped
    assert "SPY" in out           # ETF category → pass-through
    assert "BTC-USD" in out       # not in metadata → pass-through
    assert "VIX" in out           # not in metadata → pass-through


def test_unknown_predicate_fails_open(monkeypatch, capsys):
    monkeypatch.setenv("OPENCLAW_ENGINE_UNIVERSE_CLAMP", "no_such_predicate")
    u = ["DELL", "RDDT"]
    assert clamp_universe(u, fake_meta_fetch, fake_category_fetch) == u
```

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/execution/test_universe_clamp.py -v`
Expected: FAIL ModuleNotFoundError

- [ ] **Step 3: Implement** — `src/execution/universe_clamp.py`:

```python
"""SP-7 Phase A4 — gated engine universe clamp.

The live signals engine's fallback universe is ALL tickers in prices.parquet
(engine.py fallback — log-verified 2026-06-04). Any broad price backfill is
therefore a live-universe change. This clamp pins the engine's EQUITY universe
to a named predicate while data widens underneath; non-equity tickers
(benchmarks, sector ETFs, indices, crypto) always pass through.

Gate: OPENCLAW_ENGINE_UNIVERSE_CLAMP=<predicate name in universe_default>
(unset → identity). Retired (deleted, not gated-off) by SP-7 Phase C when the
resolver takes over live signals.

Fail-open by design: a broken predicate or DB error must never empty the live
universe (mirrors regime-gate fail-open conventions).
"""
from __future__ import annotations

import logging
import os
from datetime import date

logger = logging.getLogger("ENGINE")


def _default_meta_fetch() -> dict[str, tuple[str, bool]]:
    """{symbol: (asset_class, in_sp500)} from the latest metadata snapshot."""
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, asset_class, in_sp500
                  FROM ticker_metadata_snapshots
                 WHERE snapshot_date = (
                       SELECT max(snapshot_date) FROM ticker_metadata_snapshots)
            """)
            return {s: (ac, sp) for s, ac, sp in cur.fetchall()}
    finally:
        conn.close()


def _default_category_fetch() -> dict[str, str]:
    """{ticker: category} from universe_config (etf/index/crypto/... overlay)."""
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, category FROM universe_config")
            return dict(cur.fetchall())
    finally:
        conn.close()


def clamp_universe(universe, meta_fetch=_default_meta_fetch,
                   category_fetch=_default_category_fetch):
    pred_name = os.environ.get("OPENCLAW_ENGINE_UNIVERSE_CLAMP")
    if not pred_name:
        return universe
    try:
        meta = meta_fetch()
        categories = category_fetch()
        if pred_name == "sp500":
            def passes(sym):
                return bool(meta.get(sym, (None, False))[1])
        else:
            # Generic path: resolve the named predicate over TickerMetadata.
            import importlib
            from src.strategies.universe_meta import TickerMetadata  # noqa: F401
            mod = importlib.import_module("src.strategies.universe_default")
            pred = getattr(mod, pred_name)  # raises AttributeError → fail open
            # Predicate needs a full TickerMetadata row — fetch lazily only
            # when a non-sp500 clamp is actually configured.
            from src.strategies._db_adapters import PostgresMetadataDB
            db = PostgresMetadataDB(os.environ["POSTGRES_URI"])
            rows = {r.metadata.symbol: r.metadata
                    for r in map(_wrap, db.fetch_metadata_as_of(date.today()))}

            def passes(sym):
                m = rows.get(sym)
                return bool(m and pred(m, date.today()))
    except Exception as e:  # fail-open: never let the clamp empty the universe
        logger.warning(f"universe clamp '{pred_name}' failed open: {e}")
        return universe

    kept, dropped = [], 0
    for sym in universe:
        in_meta = sym in meta
        category = categories.get(sym, "equity" if in_meta else None)
        is_equity = in_meta and meta[sym][0] == "us_equity" and category == "equity"
        if not is_equity or passes(sym):
            kept.append(sym)
        else:
            dropped += 1
    logger.info(f"universe clamp '{pred_name}': kept {len(kept)}, dropped {dropped}")
    return kept


class _Wrapped:
    def __init__(self, metadata):
        self.metadata = metadata


def _wrap(row):
    return row if hasattr(row, "metadata") else _Wrapped(_to_meta(row))


def _to_meta(row):
    from src.strategies.universe_meta import TickerMetadata
    return TickerMetadata.from_row(row)
```

- [ ] **Step 4: Wire into engine** — in `engine.py`, immediately after the fallback block's `universe = list(dict.fromkeys(universe))  # dedupe preserving order` line, add:

```python
        # SP-7 Phase A4: gated clamp — pins live equity universe while broad
        # price backfills widen prices.parquet underneath. Removed in Phase C.
        from src.execution.universe_clamp import clamp_universe
        universe = clamp_universe(universe)
```

- [ ] **Step 5: Run tests**

Run: `nice -n 19 python3 -m pytest tests/execution/test_universe_clamp.py -v`
Expected: 3 PASS

- [ ] **Step 6: Live parity check (read-only)** — with the gate set in a subshell only:

```bash
set -a && . <(grep -E '^POSTGRES_URI=' /root/openclaw/.env) && set +a
nice -n 19 python3 - << 'EOF'
import os
os.environ['OPENCLAW_ENGINE_UNIVERSE_CLAMP'] = 'sp500'
import pyarrow.parquet as pq
tickers = sorted(pq.read_table('/root/openclaw/data/master/prices.parquet',
                 columns=['ticker']).to_pandas()['ticker'].unique().tolist())
from src.execution.universe_clamp import clamp_universe
out = clamp_universe(tickers)
print('before:', len(tickers), 'after:', len(out))
assert 'DELL' in out and 'SPY' in out and 'BTC-USD' in out
non_sp500_equities = set(tickers) - set(out)
print('clamped out:', len(non_sp500_equities), sorted(non_sp500_equities)[:8])
EOF
```
Expected: before 615 → after ≈ 503 + non-equity passthroughs; DELL/SPY/BTC-USD kept.

- [ ] **Step 7: Commit**

```bash
git add src/execution/universe_clamp.py src/execution/engine.py tests/execution/test_universe_clamp.py
git commit -m "feat(sp7a): gated engine universe clamp (equity-only filter, non-equity pass-through)"
```

**⚠️ Operator flip note (recorded for the runbook, executed at merge):** add `OPENCLAW_ENGINE_UNIVERSE_CLAMP=sp500` to `/root/openclaw/.env` at merge time, BEFORE the Task 8 backfill is armed. Until Phase C, the clamp is what keeps the 4.5k backfill from being a same-day live-universe change.

---

### Task 6: Adjustment convention flip + split-watcher + total-return helper

**Files:**
- Modify: `src/pipeline/collector.js:567,589` (`--adjustment all` → `split`)
- Create: `scripts/split_watcher.py`
- Create: `src/backtest/total_return.py`
- Create: `tests/backtest/test_total_return.py`
- Create: `docs/sp7-adjustment-convention.md`

- [ ] **Step 1: Flip the collector** — in `collector.js`, change line ~589 `'--adjustment', 'all',` → `'--adjustment', 'split',` and update the comment at ~567 from "pre-cutover yfinance `auto_adjust=True` behavior via `--adjustment all`" to:

```js
// SP-7 A3: canonical price convention = SPLIT-ADJUSTED ONLY (matches the
// Phase-B backfiller). Dividend adjustment restates all history on every
// dividend — incompatible with the append-only master store. Dividends are
// explicit in corporate_actions.parquet; see docs/sp7-adjustment-convention.md.
```

Verify: `node --check src/pipeline/collector.js` → OK.

- [ ] **Step 2: Write failing total-return tests** — `tests/backtest/test_total_return.py`:

```python
import pandas as pd
import pytest

from src.backtest.total_return import total_return_close


def test_dividend_adds_back_into_return_series():
    prices = pd.DataFrame({
        "ticker": ["X"] * 3,
        "date": ["2026-01-02", "2026-01-05", "2026-01-06"],
        "close": [100.0, 99.0, 100.0],   # split-adjusted closes
    })
    actions = pd.DataFrame({
        "symbol": ["X"], "action_type": ["cash_dividend"],
        "ex_date": ["2026-01-05"], "cash_amount": [1.0],
    })
    tr = total_return_close(prices, actions, "X")
    # On ex-date the holder receives $1: TR index treats close+div.
    # Day-2 simple TR return = (99 + 1)/100 = 0.0 ; price return = -1%.
    assert tr.loc[tr.date == "2026-01-05", "tr_factor"].iloc[0] == pytest.approx(1.0)
    assert tr.loc[tr.date == "2026-01-06", "tr_close"].iloc[0] > 100.0


def test_no_dividends_identity():
    prices = pd.DataFrame({
        "ticker": ["X"] * 2, "date": ["2026-01-02", "2026-01-05"],
        "close": [100.0, 101.0],
    })
    actions = pd.DataFrame(columns=["symbol", "action_type", "ex_date", "cash_amount"])
    tr = total_return_close(prices, actions, "X")
    assert tr.tr_close.tolist() == [100.0, 101.0]
```

- [ ] **Step 3: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/backtest/test_total_return.py -v`
Expected: FAIL ModuleNotFoundError

- [ ] **Step 4: Implement helper** — `src/backtest/total_return.py`:

```python
"""SP-7 A3 — total-return close from split-adjusted prices + cash dividends.

DOCUMENTED HELPER ONLY in Phase A (spec §3 A3): backtest engines adopt it in
their own projects. Not wired into any engine here.
"""
from __future__ import annotations

import pandas as pd


def total_return_close(prices: pd.DataFrame, actions: pd.DataFrame,
                       ticker: str) -> pd.DataFrame:
    """Returns DataFrame[date, close, tr_factor, tr_close] for one ticker.

    tr_factor on ex-date d = (close_d + dividend_d) / close_d; cumulative
    product reinvests dividends. tr_close = close * cumprod(shift-adjusted).
    """
    px = (prices[prices.ticker == ticker][["date", "close"]]
          .sort_values("date").reset_index(drop=True))
    div = actions[(actions.symbol == ticker)
                  & (actions.action_type == "cash_dividend")]
    div_map = dict(zip(div.ex_date.astype(str), div.cash_amount.astype(float)))
    px["dividend"] = px.date.astype(str).map(div_map).fillna(0.0)
    px["tr_factor"] = (px.close + px.dividend) / px.close
    # Reinvest: each ex-date's factor lifts every SUBSEQUENT close.
    cum = px.tr_factor.cumprod().shift(1).fillna(1.0)
    px["tr_close"] = px.close * cum
    return px[["date", "close", "tr_factor", "tr_close"]]
```

- [ ] **Step 5: Run tests to verify pass**

Run: `nice -n 19 python3 -m pytest tests/backtest/test_total_return.py -v`
Expected: 2 PASS

- [ ] **Step 6: Write the split-watcher** — `scripts/split_watcher.py`:

```python
"""SP-7 A3 — split-watcher.

Split-adjusted history is stable EXCEPT at a split: a new split restates the
ticker's whole past series. Under the append-only invariant the remedy is the
sanctioned per-ticker supersede re-backfill (runbook v2 path:
OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 + --source-tag backfill_5y_vN +
--supersede-quarantine). This watcher DETECTS and QUEUES; the operator runs
the supersede (deliberate, audited).

Run daily post-EOD (systemd timer, 21:15 UTC weekdays).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path("/root/openclaw")
CORP_ACTIONS = ROOT / "data" / "master" / "corporate_actions.parquet"
PRICES = ROOT / "data" / "master" / "prices.parquet"
PENDING = ROOT / "data" / ".pending_split_rebackfills.txt"
WEBHOOK_ENV = "DISCORD_DATA_ALERTS_WEBHOOK"


def find_new_splits(today: str) -> list[dict]:
    ca = pd.read_parquet(CORP_ACTIONS,
                         columns=["symbol", "action_type", "ex_date", "ratio"])
    splits = ca[ca.action_type.astype(str).str.contains("split", case=False)
                & (ca.ex_date.astype(str) == today)]
    if splits.empty:
        return []
    covered = set(pd.read_parquet(PRICES, columns=["ticker"]).ticker.unique())
    return [r for r in splits.to_dict("records") if r["symbol"] in covered]


def notify(msg: str) -> None:
    url = os.environ.get(WEBHOOK_ENV)
    if not url:
        print(f"[split-watcher] (no webhook) {msg}")
        return
    body = json.dumps({"content": msg}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "openclaw-split-watcher/1.0"},  # urllib-UA lesson
    )
    urllib.request.urlopen(req, timeout=15)


def main() -> int:
    today = date.today().isoformat()
    hits = find_new_splits(today)
    if not hits:
        print(f"[split-watcher] {today}: no splits on covered tickers")
        return 0
    PENDING.parent.mkdir(exist_ok=True)
    with PENDING.open("a") as f:
        for h in hits:
            f.write(f"{today} {h['symbol']} ratio={h.get('ratio')}\n")
    syms = ", ".join(h["symbol"] for h in hits)
    notify(f"🪓 Split detected on covered ticker(s): **{syms}** — history is "
           f"now stale-adjusted. Queue written to {PENDING}. Run the supersede "
           f"re-backfill per docs/sp2-backfill-runbook.md (v2 path).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Watcher smoke (no splits expected today → clean exit)**

```bash
nice -n 19 python3 scripts/split_watcher.py
```
Expected: `no splits on covered tickers` (or a legitimate detection).

- [ ] **Step 8: Write the convention doc** — `docs/sp7-adjustment-convention.md`: state the canonical convention (split-adjusted), why (append-only incompatibility of dividend adjustment), the legacy mixed-history note (NOT restated), the split-watcher → supersede flow, and the `total_return_close` helper for backtests. ~30 lines, written fresh from the spec §3 A3 text.

- [ ] **Step 9: Install the timer (systemd user units, root user manager — same pattern as johnbot)**

Create `docs/sp7-split-watcher.service` + `.timer` (OnCalendar=`Mon-Fri 21:15 UTC`, ExecStart=`/usr/bin/nice -n 19 /usr/bin/python3 /root/openclaw/scripts/split_watcher.py`, WorkingDirectory=/root/openclaw, EnvironmentFile=/root/openclaw/.env) and install:

```bash
cp docs/sp7-split-watcher.{service,timer} /root/.config/systemd/user/
XDG_RUNTIME_DIR=/run/user/0 systemctl --user daemon-reload
XDG_RUNTIME_DIR=/run/user/0 systemctl --user enable --now sp7-split-watcher.timer
XDG_RUNTIME_DIR=/run/user/0 systemctl --user list-timers | grep split-watcher
```

- [ ] **Step 10: Commit**

```bash
git add src/pipeline/collector.js scripts/split_watcher.py src/backtest/total_return.py tests/backtest/test_total_return.py docs/sp7-adjustment-convention.md docs/sp7-split-watcher.service docs/sp7-split-watcher.timer
git commit -m "feat(sp7a): canonical split-adjusted convention — collector flip, split-watcher, total-return helper"
```

---

### Task 7: EDGAR ingest run for covered tickers + market_cap acceptance

**Files:** none new (operational task; record results in the grounding doc)

- [ ] **Step 1: Run the ingester for all covered tickers (~615) — live append to NEW parquet**

```bash
set -a && . <(grep -E '^POSTGRES_URI=' /root/openclaw/.env) && set +a
nice -n 19 python3 -m src.pipeline.backfillers.edgar_shares --covered-only 2>&1 | tail -5
```
Expected: `DONE universe=615 added=<thousands> no_cik=<small> no_facts=<small>` (~12 min at 8 req/s). ETFs/trusts in the covered set (SPY/QQQ/sector ETFs) land in no_cik/no_facts — correct, they have no common shares.

- [ ] **Step 2: Verify lookup coverage for the equity names**

```bash
nice -n 19 python3 - << 'EOF'
from datetime import date
import pandas as pd
sp500 = set(pd.read_parquet('/root/openclaw/data/master/prices.parquet', columns=['ticker']).ticker.unique())
from src.pipeline.market_cap_lookup import build_market_cap_lookup
caps = build_market_cap_lookup(sorted(sp500), date.today())
have = {k: v for k, v in caps.items() if v}
print(f"caps for {len(have)}/{len(caps)} covered tickers")
print('NVDA cap ≈', f"{caps.get('NVDA', 0)/1e12:.2f}T")
assert caps.get('NVDA', 0) > 1e12
EOF
```
Expected: caps for ≥ 90% of covered equity tickers (ETFs/index/crypto names correctly None); NVDA in the trillions.

- [ ] **Step 3: Force one daily-writer run and verify the snapshot self-heals**

```bash
nice -n 19 python3 -m src.pipeline.run_ticker_metadata_step
docker exec openclaw-postgres psql -U openclaw -d openclaw -t -c "
SELECT count(*) FILTER (WHERE market_cap IS NOT NULL) AS with_cap,
       count(*) FILTER (WHERE in_r1000) AS r1000,
       count(*) FILTER (WHERE in_r3000) AS r3000
FROM ticker_metadata_snapshots
WHERE snapshot_date = (SELECT max(snapshot_date) FROM ticker_metadata_snapshots);"
```
Expected: with_cap > 0 (≈ covered equity count for now — grows with the 5k backfill), **r1000 > 0, r3000 > 0 for the first time ever**.

- [ ] **Step 4: Append results to the grounding doc + commit**

```bash
git add docs/superpowers/specs/2026-06-04-sp7-phase-a-grounding.md
git commit -m "docs(sp7a): EDGAR ingest + market_cap acceptance evidence (r1000/r3000 first non-empty)"
```

---

### Task 8: Liquid-5k backfill artifact + overnight wrapper

**Files:**
- Create: `scripts/build_backfill_universe_v2.py`
- Create: `scripts/overnight_backfill.sh`
- Create: `docs/sp7-overnight-backfill.service` + `docs/sp7-overnight-backfill.timer`
- Create: `tests/pipeline/test_backfill_universe_v2.py`

- [ ] **Step 1: Write failing test** — `tests/pipeline/test_backfill_universe_v2.py`:

```python
from scripts.build_backfill_universe_v2 import select_v2_universe


def test_selects_liquid_tradable_minus_covered():
    rows = [
        ("DELL", True, "active", True),    # covered → excluded
        ("RDDT", True, "active", True),    # liquid, uncovered → included
        ("ILLIQ", True, "active", False),  # not easy_to_borrow → excluded
        ("DEADCO", True, "inactive", True),
        ("NOTRADE", False, "active", True),
    ]
    covered = {"DELL"}
    out = select_v2_universe(rows, covered)
    assert out == ["RDDT"]


def test_share_class_dash_normalization():
    rows = [("BRK.B", True, "active", True)]
    out = select_v2_universe(rows, set())
    assert out == ["BRK-B"]
```

- [ ] **Step 2: Run to verify failure**

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_backfill_universe_v2.py -v`
Expected: FAIL ModuleNotFoundError

- [ ] **Step 3: Implement** — `scripts/build_backfill_universe_v2.py`:

```python
"""SP-7 A5 — build data/.backfill_universe_v2.txt.

v2 = (tradable ∧ active ∧ easy_to_borrow us_equity) − already-covered.
Artifact is committed (mirrors v1; preflight gate #6 pattern).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

ROOT = Path("/root/openclaw")
OUT = ROOT / "data" / ".backfill_universe_v2.txt"
PRICES = ROOT / "data" / "master" / "prices.parquet"


def select_v2_universe(rows, covered: set[str]) -> list[str]:
    out = []
    for symbol, tradable, status, etb in rows:
        if not (tradable and status == "active" and etb):
            continue
        norm = symbol.replace(".", "-")
        if norm in covered or symbol in covered:
            continue
        out.append(norm)
    return sorted(set(out))


def main() -> int:
    pg = psycopg2.connect(os.environ["POSTGRES_URI"])
    with pg.cursor() as cur:
        cur.execute("""
            SELECT symbol, tradable, status, easy_to_borrow
              FROM alpaca_tradable_universe
             WHERE asset_class = 'us_equity'
        """)
        rows = cur.fetchall()
    covered = set(pd.read_parquet(PRICES, columns=["ticker"]).ticker.unique())
    universe = select_v2_universe(rows, covered)
    OUT.write_text("\n".join(universe) + "\n")
    print(f"[v2-universe] wrote {len(universe)} tickers to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, then build the artifact**

```bash
nice -n 19 python3 -m pytest tests/pipeline/test_backfill_universe_v2.py -v   # 2 PASS
set -a && . <(grep -E '^POSTGRES_URI=' /root/openclaw/.env) && set +a
nice -n 19 python3 scripts/build_backfill_universe_v2.py
wc -l data/.backfill_universe_v2.txt
```
Expected: ~4,300–4,700 tickers.

- [ ] **Step 5: Write the overnight wrapper** — `scripts/overnight_backfill.sh`:

```bash
#!/usr/bin/env bash
# SP-7 A5 — overnight-window wrapper for the liquid-5k price backfill.
# Armed by the operator: touch /root/openclaw/data/.sp7_backfill_armed
# Runs nightly (timer 01:00 UTC = 21:00 ET) until the driver reports DONE
# with zero pending chunks, then disarms itself and notifies.
set -euo pipefail
cd /root/openclaw
ARMED=data/.sp7_backfill_armed
LOG=logs/sp7_backfill_$(date -u +%F).log
[ -f "$ARMED" ] || { echo "[sp7-backfill] not armed, exiting"; exit 0; }

set -a; . <(grep -E '^(POSTGRES_URI|REDIS_URL|ALPACA_API_KEY|ALPACA_API_SECRET|ALPACA_SECRET_KEY|DISCORD)' .env | sed 's/\r$//'); set +a

# Seconds until 13:00 UTC (08:00 ET) — the window close.
now=$(date -u +%s); close=$(date -u -d "13:00" +%s)
[ "$close" -le "$now" ] && close=$(date -u -d "tomorrow 13:00" +%s)
budget=$(( close - now ))
echo "[sp7-backfill] window budget ${budget}s" | tee -a "$LOG"

set +e
timeout --signal=TERM "$budget" nice -n 19 python3 scripts/backfill_universe_5y.py \
    --target prices --resume \
    --tickers "$(paste -sd, data/.backfill_universe_v2.txt)" >> "$LOG" 2>&1
rc=$?
set -e

missing=$(nice -n 19 python3 - <<'PYEOF'
import pandas as pd
cov = set(pd.read_parquet('data/master/prices.parquet', columns=['ticker']).ticker.unique())
v2 = [l.strip() for l in open('data/.backfill_universe_v2.txt') if l.strip()]
# pre-listing tickers quarantine 'empty' forever — treat any ticker with a
# terminal audit row for ALL its year-chunks as accounted-for via the DONE line
print(len([t for t in v2 if t not in cov]))
PYEOF
)
if [ $rc -eq 0 ] && grep -q "\[prices\] DONE" "$LOG" && [ "$missing" -eq 0 ]; then
  rm -f "$ARMED"
  echo "[sp7-backfill] COMPLETE — disarmed" | tee -a "$LOG"
elif [ $rc -eq 0 ] && grep -q "\[prices\] DONE" "$LOG" ; then
  echo "[sp7-backfill] driver DONE but $missing v2 tickers uncovered —" \
       "likely never-listed/quarantined-empty names; review backfill_audit," \
       "then disarm manually (rm $ARMED)" | tee -a "$LOG"
elif [ $rc -eq 124 ]; then
  echo "[sp7-backfill] window closed (SIGTERM at 08:00 ET) — resumes tonight" | tee -a "$LOG"
else
  echo "[sp7-backfill] driver rc=$rc — investigate $LOG" | tee -a "$LOG"
fi
```

`chmod +x scripts/overnight_backfill.sh`

- [ ] **Step 6: Write the units** — `docs/sp7-overnight-backfill.service`:

```ini
[Unit]
Description=SP-7 liquid-5k overnight price backfill window (armed via data/.sp7_backfill_armed)

[Service]
Type=oneshot
WorkingDirectory=/root/openclaw
ExecStart=/bin/bash /root/openclaw/scripts/overnight_backfill.sh
```

`docs/sp7-overnight-backfill.timer`:

```ini
[Unit]
Description=Nightly 01:00 UTC (21:00 ET) SP-7 backfill window

[Timer]
# Mon-Fri 01:00 UTC = Sun-Thu ~21:00 ET nights; window closes 13:00 UTC.
# Saturday EXCLUDED: the Sat 12:00 UTC weekend-refresh stack must not share
# the box with a backfill window (8GB/2-core OOM lesson).
OnCalendar=Mon-Fri 01:00 UTC
Persistent=false

[Install]
WantedBy=timers.target
```

- [ ] **Step 7: Dry-run smoke (5 tickers through the real driver path)**

```bash
set -a && . <(grep -E '^(POSTGRES_URI|REDIS_URL|ALPACA_API_KEY|ALPACA_API_SECRET|ALPACA_SECRET_KEY)' .env | sed 's/\r$//') && set +a
head -5 data/.backfill_universe_v2.txt
nice -n 19 python3 scripts/backfill_universe_5y.py --target prices --dry-run \
  --tickers "$(head -5 data/.backfill_universe_v2.txt | paste -sd,)" 2>&1 | tail -8
```
Expected: `[dry-run] TICKER:YEAR: N rows valid` lines, `DONE promoted=0`.

- [ ] **Step 8: Install timer (DISARMED — runs are no-ops until operator touches the sentinel)**

```bash
cp docs/sp7-overnight-backfill.{service,timer} /root/.config/systemd/user/
XDG_RUNTIME_DIR=/run/user/0 systemctl --user daemon-reload
XDG_RUNTIME_DIR=/run/user/0 systemctl --user enable --now sp7-overnight-backfill.timer
```

- [ ] **Step 9: Commit**

```bash
git add scripts/build_backfill_universe_v2.py scripts/overnight_backfill.sh data/.backfill_universe_v2.txt docs/sp7-overnight-backfill.service docs/sp7-overnight-backfill.timer tests/pipeline/test_backfill_universe_v2.py
git commit -m "feat(sp7a): liquid-5k backfill artifact + armed overnight-window wrapper (disarmed by default)"
```

---

### Task 9: universe_config activation script (runs POST-backfill)

**Files:**
- Create: `scripts/activate_universe_v2.py`
- Create: `tests/pipeline/test_activate_universe_v2.py`

- [ ] **Step 1: Write failing test** (SQL-generation unit — the script's row builder):

```python
from scripts.activate_universe_v2 import build_upsert_params


def test_aux_flags_false_and_notes_tagged():
    params = build_upsert_params(["RDDT"], note="sp7-v2-activation 2026-06-XX")
    assert params == [("RDDT", "{SP500_PLUS}", True, "equity", False, False,
                       3650, "sp7-v2-activation 2026-06-XX")]
```

- [ ] **Step 2: Run to verify failure, then implement** — `scripts/activate_universe_v2.py`:

```python
"""SP-7 A5 — activate the v2 universe in universe_config AFTER the price
backfill completes (daily price maintenance envelope until Phase C).

has_options / has_fundamentals stay FALSE: aux layers are scoped to adopted
strategy universes per spec §2 decision 4. Idempotent upsert; never
deactivates anything.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import psycopg2

V2 = Path("/root/openclaw/data/.backfill_universe_v2.txt")
NOTE = f"sp7-v2-activation {date.today().isoformat()}"
MEMBERSHIP = "SP500_PLUS"  # label only; index_membership is a text[] tag


def build_upsert_params(tickers: list[str], note: str) -> list[tuple]:
    return [(t, "{%s}" % MEMBERSHIP, True, "equity", False, False, 3650, note)
            for t in tickers]


def main() -> int:
    tickers = [l.strip() for l in V2.read_text().splitlines() if l.strip()]
    pg = psycopg2.connect(os.environ["POSTGRES_URI"])
    with pg.cursor() as cur:
        cur.executemany("""
            INSERT INTO universe_config
                (ticker, index_membership, active, category,
                 has_options, has_fundamentals, min_history_days, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker) DO UPDATE SET
                active = true,
                category = 'equity',
                notes = COALESCE(universe_config.notes || ' | ', '') || EXCLUDED.notes
        """, build_upsert_params(tickers, NOTE))
    pg.commit()
    print(f"[activate-v2] upserted {len(tickers)} tickers (aux flags FALSE)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `nice -n 19 python3 -m pytest tests/pipeline/test_activate_universe_v2.py -v` → PASS.
**Do NOT run the script now** — it is a post-backfill runbook step (Task 10).

- [ ] **Step 3: Commit**

```bash
git add scripts/activate_universe_v2.py tests/pipeline/test_activate_universe_v2.py
git commit -m "feat(sp7a): post-backfill universe_config activation script (aux flags scoped off)"
```

---

### Task 10: Runbook + full regression + acceptance pack

**Files:**
- Create: `docs/sp7-phase-a-runbook.md`

- [ ] **Step 1: Full test regression**

Run: `nice -n 19 python3 -m pytest tests/pipeline tests/execution tests/backtest -v 2>&1 | tail -15`
Expected: all green (new suites + pre-existing in those dirs). Known pre-existing failures elsewhere (e.g. `test_intraday_hmm.py` ×3, LRN-20260604-002) are out of scope — do NOT chase them.

- [ ] **Step 2: Write the runbook** — `docs/sp7-phase-a-runbook.md`, exact operator sequence:

```markdown
# SP-7 Phase A — Operator Runbook

## Merge + flip (one sitting)
1. Merge `feat/sp7-phase-a-data-foundations` into the live branch (`--no-ff`).
2. Apply migration 129 (listed_date) if not already applied by Task 4 smoke.
3. Add to /root/openclaw/.env:  OPENCLAW_ENGINE_UNIVERSE_CLAMP=sp500
4. Restart johnbot:  XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service
5. Verify next engine run logs "universe clamp 'sp500': kept ~503+..." (the
   collector/engine spawn per-cycle; no further restarts needed).

## One-time data jobs (any order, nice -19)
6. EDGAR shares for the v2 universe:
   python3 -m src.pipeline.backfillers.edgar_shares --universe-file data/.backfill_universe_v2.txt
7. listed_date probe (full):  python3 scripts/probe_listed_dates.py   (~25 min for 13.9k NULL rows)

## The 4.5k backfill (multi-night, AFTER step 3-5 are verified)
8. Arm:  touch /root/openclaw/data/.sp7_backfill_armed
9. Watch: logs/sp7_backfill_<date>.log + #backfill-log; window = 21:00 ET → 08:00 ET nightly.
10. On COMPLETE (wrapper disarms itself):
    a. python3 scripts/activate_universe_v2.py
    b. Historical metadata for v2 names — NOTE the checkpoint gotcha: month-chunks
       are Redis-marked from v1 runs ('promoted') and from the 2026-06-04 161-run
       ('quarantined'). Both states skip. Run WITH supersede to land the new rows
       (metadata insert is ON CONFLICT DO NOTHING — append-only safe):
       OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/backfill_universe_5y.py \
         --target metadata --source-tag backfill_5y_v2 --supersede-quarantine \
         --tickers "$(paste -sd, data/.backfill_universe_v2.txt)"
    c. Acceptance SQL (below).

## Acceptance
- prices.parquet distinct tickers ≈ 5,100+; v2-file coverage 100%:
    python3 - <<'EOF'
    import pandas as pd
    cov=set(pd.read_parquet('data/master/prices.parquet',columns=['ticker']).ticker.unique())
    v2=[l.strip() for l in open('data/.backfill_universe_v2.txt') if l.strip()]
    print(len(cov), 'missing:', [t for t in v2 if t not in cov][:10])
    EOF
- Latest snapshot: market_cap non-NULL ≥95% of v2∩CIK-mapped; r1000=1000, r3000=3000:
    SELECT count(*) FILTER (WHERE market_cap IS NOT NULL) AS caps,
           count(*) FILTER (WHERE in_r1000) AS r1000,
           count(*) FILTER (WHERE in_r3000) AS r3000
    FROM ticker_metadata_snapshots
    WHERE snapshot_date=(SELECT max(snapshot_date) FROM ticker_metadata_snapshots);
- Engine clamp held: grep latest cycle log for "universe clamp 'sp500'" — kept
  count must NOT grow with the backfill.
- Phase B is GO when all three pass.

## Abort/rollback
- Disarm backfill: rm /root/openclaw/data/.sp7_backfill_armed (chunks already
  promoted stay — append-only; harmless behind the clamp).
- Clamp off: remove the env line (returns engine to all-parquet behavior).
- NEVER git reset --hard on the live checkout (uncommitted live-critical files).
```

- [ ] **Step 3: Commit + hand off for merge review**

```bash
git add docs/sp7-phase-a-runbook.md
git commit -m "docs(sp7a): Phase A operator runbook + acceptance pack"
```

Use superpowers:requesting-code-review, then superpowers:finishing-a-development-branch (operator decides merge timing — the clamp flip is bundled with merge per runbook step 1-5).

---

## Task dependency notes (for the dispatcher)

- Tasks 1→2→3 are sequential (each builds on the previous module).
- Task 4 and Task 5 are independent of 1-3 and of each other (parallelizable
  contexts, but execute sequentially on this 2-core box per VPS constraint).
- Task 6 is independent.
- Task 7 needs Tasks 1-3 merged into the branch.
- Task 8 needs Task 5 committed (clamp must exist before the wrapper lands;
  the FLIP is a merge-time runbook step).
- Task 9 needs Task 8's artifact builder.
- Task 10 last.
- **Ordering invariant (spec §3 A4):** the operator arms the Task 8 backfill
  only AFTER the clamp is merged + flipped (runbook enforces).
```
