# SP-2 Phase A: Universe-Slice Machinery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the predicate-based universe-resolution machinery so each strategy can declare its tradeable slice. Default predicate (`meta.in_sp500`) preserves byte-identical behavior across all 102 existing strategies post-deploy; explicit predicates land in Phase C.

**Architecture:** A `TickerMetadata` frozen dataclass + Postgres `ticker_metadata_snapshots` table (one row per `(date, symbol)`) serves as the point-in-time source-of-truth. A `UniverseResolver` applies each strategy's `universe_filter(meta, as_of) -> bool` callable against the latest snapshot ≤ as_of and returns the ticker list. A `union_universe(as_of, states)` helper computes the data-fetch envelope. Backtest engines pass the bar date as `as_of` to the resolver each step, closing the look-ahead surface. A signature lint + import ban + sandbox check at lifecycle transitions enforces the contract.

**Tech Stack:** Python 3.13 (resolver, lint, writer, backtest, lifecycle, doctor); Node.js (collector.js, alpaca_options.js, alpaca_news.py via JS wrapper, daily-cycle.js, dashboard tiles); PostgreSQL (migrations 111-114); Redis (resolver cache, lifecycle sandbox state); pytest + pre-commit + GHA for CI gates.

**Spec:** `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md`

**Branch:** `feat/sp2-phase-a-universe-machinery` (off main)

**Acceptance:** After Phase A merges + deploys, `union_universe(today(), states=("live",))` returns the SP500 list (~500 tickers) deduped; collector + options-archive + sentiment + signals all consume that union; doctor preflight green; all 102 strategies still produce the same signals as the day before deploy (smoke comparison).

---

## Task 0: Branch + workspace setup

**Files:** none (git scaffolding)

- [ ] **Step 1: Create feature branch off main**

```bash
cd /root/openclaw
git fetch origin
git checkout -b feat/sp2-phase-a-universe-machinery origin/main
```

- [ ] **Step 2: Verify clean tree**

Run: `git status`
Expected: `On branch feat/sp2-phase-a-universe-machinery / nothing to commit, working tree clean`

- [ ] **Step 3: Confirm Python + Node tooling versions**

Run: `python3 --version && node --version && cat package.json | grep '"version"'`
Expected: Python 3.13.x, Node 22.x, openclaw version matches main.

---

## Task 1: Probe FMP `historical-market-capitalization` endpoint

This is a Phase A precondition. It decides whether `ticker_metadata_writer` uses the FMP endpoint directly or constructs market cap from `prices × shares-outstanding`. The probe artifact is committed; the decision is encoded in code.

**Files:**
- Create: `scripts/probe_fmp_historical_market_cap.py`
- Create: `docs/superpowers/specs/sp2-fmp-mktcap-probe.md` (probe result)

- [ ] **Step 1: Write the probe script**

```python
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
```

- [ ] **Step 2: Run probe**

```bash
cd /root/openclaw
source .env
python3 scripts/probe_fmp_historical_market_cap.py
```

Expected output: `PRIMARY:fmp_endpoint` OR `FALLBACK:prices_x_shares`. Probe result written to `docs/superpowers/specs/sp2-fmp-mktcap-probe.md`.

- [ ] **Step 3: Commit probe + result**

```bash
git add scripts/probe_fmp_historical_market_cap.py docs/superpowers/specs/sp2-fmp-mktcap-probe.md
git commit -m "$(cat <<'EOF'
spec(sp2): FMP historical-market-cap probe + decision

Records the source decision for TickerMetadata.market_cap.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Migration 111 — `ticker_metadata_snapshots`

**Files:**
- Create: `src/database/migrations/111_ticker_metadata_snapshots.sql`
- Test: `tests/test_migration_111.py`

- [ ] **Step 1: Write the migration**

```sql
-- 111_ticker_metadata_snapshots.sql
--
-- Point-in-time TickerMetadata source. Daily writes from
-- ticker_metadata_writer.py; monthly historical from Phase B backfill.
-- Resolver always reads the latest row where snapshot_date <= as_of.
-- Append-only per master invariant; bad rows go through data_quarantine.

CREATE TABLE IF NOT EXISTS ticker_metadata_snapshots (
  snapshot_date     DATE NOT NULL,
  symbol            TEXT NOT NULL,
  asset_class       TEXT NOT NULL,
  exchange          TEXT,
  status            TEXT NOT NULL,
  tradable          BOOLEAN NOT NULL DEFAULT FALSE,
  shortable         BOOLEAN NOT NULL DEFAULT FALSE,
  fractionable      BOOLEAN NOT NULL DEFAULT FALSE,
  easy_to_borrow    BOOLEAN NOT NULL DEFAULT FALSE,
  market_cap        NUMERIC,
  adv_usd_20d       NUMERIC,
  sector            TEXT,
  industry          TEXT,
  options_eligible  BOOLEAN NOT NULL DEFAULT FALSE,
  in_sp500          BOOLEAN NOT NULL DEFAULT FALSE,
  in_r1000          BOOLEAN NOT NULL DEFAULT FALSE,
  in_r3000          BOOLEAN NOT NULL DEFAULT FALSE,
  listed_date       DATE,
  delisted_date     DATE,
  source_tag        TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (snapshot_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_meta_snapshots_symbol_date
  ON ticker_metadata_snapshots(symbol, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_meta_snapshots_date_active
  ON ticker_metadata_snapshots(snapshot_date)
  WHERE status='active' AND tradable=TRUE;
```

- [ ] **Step 2: Write the migration test**

```python
# tests/test_migration_111.py
import os, psycopg
import pytest

DSN = os.environ["DATABASE_URL"]

def test_table_exists():
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass('public.ticker_metadata_snapshots')")
        assert cur.fetchone()[0] == "ticker_metadata_snapshots"

def test_pk_and_indexes():
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename='ticker_metadata_snapshots' ORDER BY 1
        """)
        names = [r[0] for r in cur.fetchall()]
        assert "ticker_metadata_snapshots_pkey" in names
        assert "idx_meta_snapshots_symbol_date" in names
        assert "idx_meta_snapshots_date_active" in names

def test_idempotent_upsert():
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO ticker_metadata_snapshots
              (snapshot_date, symbol, asset_class, status, source_tag)
            VALUES ('2026-05-22', 'AAPL', 'us_equity', 'active', 'test')
            ON CONFLICT (snapshot_date, symbol) DO UPDATE SET source_tag = EXCLUDED.source_tag
        """)
        cur.execute("""
            INSERT INTO ticker_metadata_snapshots
              (snapshot_date, symbol, asset_class, status, source_tag)
            VALUES ('2026-05-22', 'AAPL', 'us_equity', 'active', 'test_v2')
            ON CONFLICT (snapshot_date, symbol) DO UPDATE SET source_tag = EXCLUDED.source_tag
        """)
        cur.execute("SELECT source_tag FROM ticker_metadata_snapshots WHERE symbol='AAPL' AND snapshot_date='2026-05-22'")
        assert cur.fetchone()[0] == "test_v2"
        cur.execute("DELETE FROM ticker_metadata_snapshots WHERE source_tag IN ('test', 'test_v2')")
```

- [ ] **Step 3: Run migration in a test database**

```bash
cd /root/openclaw
psql "$DATABASE_URL_TEST" -f src/database/migrations/111_ticker_metadata_snapshots.sql
```

Expected: no errors; table + 2 indexes created.

- [ ] **Step 4: Run the test against test DB**

```bash
DATABASE_URL="$DATABASE_URL_TEST" pytest tests/test_migration_111.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/database/migrations/111_ticker_metadata_snapshots.sql tests/test_migration_111.py
git commit -m "feat(sp2): migration 111 — ticker_metadata_snapshots"
```

---

## Task 3: Migrations 112, 113, 114

**Files:**
- Create: `src/database/migrations/112_strategy_universe_recommendations.sql`
- Create: `src/database/migrations/113_universe_resolution_audit.sql`
- Create: `src/database/migrations/114_data_quarantine.sql`
- Test: `tests/test_migrations_112_113_114.py`

- [ ] **Step 1: Write migration 112** — copy SQL block verbatim from spec §3.4 `112_strategy_universe_recommendations.sql`.

- [ ] **Step 2: Write migration 113** — copy SQL block verbatim from spec §3.4 `113_universe_resolution_audit.sql`.

- [ ] **Step 3: Write migration 114** — copy SQL block verbatim from spec §3.4 `114_data_quarantine.sql`.

- [ ] **Step 4: Write tests covering all three** (existence + PKs + key index):

```python
# tests/test_migrations_112_113_114.py
import os, psycopg

DSN = os.environ["DATABASE_URL"]

def _exists(table):
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        return cur.fetchone()[0] == table

def test_recs_table():
    assert _exists("strategy_universe_recommendations")

def test_audit_table():
    assert _exists("universe_resolution_audit")

def test_quarantine_table():
    assert _exists("data_quarantine")

def test_quarantine_lookup_index():
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            SELECT indexdef FROM pg_indexes
            WHERE tablename='data_quarantine' AND indexname='idx_quarantine_lookup'
        """)
        row = cur.fetchone()
        assert row is not None
        assert "superseded_at IS NULL" in row[0]
```

- [ ] **Step 5: Apply migrations + run tests**

```bash
psql "$DATABASE_URL_TEST" -f src/database/migrations/112_strategy_universe_recommendations.sql
psql "$DATABASE_URL_TEST" -f src/database/migrations/113_universe_resolution_audit.sql
psql "$DATABASE_URL_TEST" -f src/database/migrations/114_data_quarantine.sql
DATABASE_URL="$DATABASE_URL_TEST" pytest tests/test_migrations_112_113_114.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/database/migrations/112_*.sql src/database/migrations/113_*.sql src/database/migrations/114_*.sql tests/test_migrations_112_113_114.py
git commit -m "feat(sp2): migrations 112-114 — universe recs, resolution audit, data quarantine"
```

---

## Task 4: `TickerMetadata` dataclass

**Files:**
- Create: `src/strategies/universe_meta.py`
- Test: `tests/test_universe_meta.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_universe_meta.py
from datetime import date
import pytest
from src.strategies.universe_meta import TickerMetadata

def test_construct_minimal():
    m = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="Information Technology", industry="Consumer Electronics",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=date(1980, 12, 12), delisted_date=None,
    )
    assert m.symbol == "AAPL"
    assert m.in_sp500 is True

def test_frozen():
    m = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=None, adv_usd_20d=None,
        sector=None, industry=None,
        options_eligible=False, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=None, delisted_date=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        m.symbol = "MSFT"

def test_from_row():
    row = {
        "symbol": "MSFT", "asset_class": "us_equity", "exchange": "NASDAQ",
        "status": "active", "tradable": True, "shortable": True,
        "fractionable": True, "easy_to_borrow": True,
        "market_cap": 3.0e12, "adv_usd_20d": 1.5e10,
        "sector": "Information Technology", "industry": "Software",
        "options_eligible": True, "in_sp500": True, "in_r1000": True, "in_r3000": True,
        "listed_date": date(1986, 3, 13), "delisted_date": None,
    }
    m = TickerMetadata.from_row(row)
    assert m.symbol == "MSFT"
    assert m.market_cap == 3.0e12
```

- [ ] **Step 2: Verify the test fails**

Run: `pytest tests/test_universe_meta.py -v`
Expected: `ModuleNotFoundError: No module named 'src.strategies.universe_meta'`

- [ ] **Step 3: Implement the dataclass**

```python
# src/strategies/universe_meta.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Optional

@dataclass(frozen=True, slots=True)
class TickerMetadata:
    symbol: str
    asset_class: str
    exchange: Optional[str]
    status: str
    tradable: bool
    shortable: bool
    fractionable: bool
    easy_to_borrow: bool
    market_cap: Optional[float]
    adv_usd_20d: Optional[float]
    sector: Optional[str]
    industry: Optional[str]
    options_eligible: bool
    in_sp500: bool
    in_r1000: bool
    in_r3000: bool
    listed_date: Optional[date]
    delisted_date: Optional[date]

    @classmethod
    def from_row(cls, row: dict) -> "TickerMetadata":
        return cls(**{f: row[f] for f in cls.__dataclass_fields__})
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_universe_meta.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/universe_meta.py tests/test_universe_meta.py
git commit -m "feat(sp2): TickerMetadata frozen dataclass"
```

---

## Task 5: Default predicate + 12 candidate predicates

**Files:**
- Create: `src/strategies/universe_default.py`
- Test: `tests/test_universe_predicates.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_universe_predicates.py
from datetime import date
import pytest
from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_default import (
    DEFAULT_UNIVERSE_FILTER, CANDIDATE_PREDICATES,
    sp500, r1000, r3000, options_eligible_only,
    large_cap, mid_cap, small_cap_liquid,
    large_cap_options, mid_cap_options,
    no_adr, no_otc, top500_by_adv,
)

@pytest.fixture
def aapl():
    return TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="Information Technology", industry="Consumer Electronics",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=date(1980, 12, 12), delisted_date=None,
    )

@pytest.fixture
def unknown_pink():
    return TickerMetadata(
        symbol="XYZQ", asset_class="us_equity", exchange="OTC",
        status="active", tradable=True, shortable=False,
        fractionable=False, easy_to_borrow=False,
        market_cap=5e7, adv_usd_20d=1e5,
        sector=None, industry=None,
        options_eligible=False, in_sp500=False, in_r1000=False, in_r3000=False,
        listed_date=None, delisted_date=None,
    )

def test_default_filter_aapl(aapl):
    assert DEFAULT_UNIVERSE_FILTER(aapl, date(2026, 1, 1)) is True

def test_default_filter_unknown(unknown_pink):
    assert DEFAULT_UNIVERSE_FILTER(unknown_pink, date(2026, 1, 1)) is False

def test_candidate_set_count():
    assert len(CANDIDATE_PREDICATES) == 12

def test_each_candidate_callable(aapl):
    for name, fn in CANDIDATE_PREDICATES.items():
        result = fn(aapl, date(2026, 1, 1))
        assert isinstance(result, bool), f"{name} returned non-bool"

def test_options_eligible_only_filters_unknown(unknown_pink, aapl):
    assert options_eligible_only(unknown_pink, date(2026, 1, 1)) is False
    assert options_eligible_only(aapl, date(2026, 1, 1)) is True

def test_no_otc_filters_pink(unknown_pink):
    assert no_otc(unknown_pink, date(2026, 1, 1)) is False

def test_top500_by_adv_handles_none_adv():
    m = TickerMetadata(
        symbol="ABCD", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=1e9, adv_usd_20d=None,
        sector="X", industry="Y",
        options_eligible=False, in_sp500=False, in_r1000=False, in_r3000=False,
        listed_date=date(2020, 1, 1), delisted_date=None,
    )
    # ADV None must not crash; predicate returns False
    assert top500_by_adv(m, date(2026, 1, 1)) is False
```

- [ ] **Step 2: Verify the test fails**

Run: `pytest tests/test_universe_predicates.py -v`
Expected: `ModuleNotFoundError: No module named 'src.strategies.universe_default'`

- [ ] **Step 3: Implement default + 12 candidates**

```python
# src/strategies/universe_default.py
"""SP-2 universe predicates.

Each predicate has signature (meta, as_of) -> bool.

DO NOT import datetime, time, or os into this module — the universe_lint
gate forbids it for any module that defines universe_filter callables.
"""
from __future__ import annotations
from src.strategies.universe_meta import TickerMetadata

# --- the default --- behavior-preserving for Phase A
def sp500(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_sp500)

DEFAULT_UNIVERSE_FILTER = sp500

# --- the 12 candidates Mastermind picks among in Phase C ---

def r1000(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_r1000)

def r3000(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_r3000)

def options_eligible_only(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.options_eligible and meta.tradable and meta.status == "active")

def large_cap(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.market_cap and meta.market_cap >= 10e9 and meta.in_r3000)

def mid_cap(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.market_cap and 2e9 <= meta.market_cap < 10e9 and meta.in_r3000)

def small_cap_liquid(meta: TickerMetadata, as_of) -> bool:
    return bool(
        meta.market_cap and 300e6 <= meta.market_cap < 2e9
        and meta.adv_usd_20d and meta.adv_usd_20d >= 5e6
        and meta.in_r3000
    )

def large_cap_options(meta: TickerMetadata, as_of) -> bool:
    return large_cap(meta, as_of) and meta.options_eligible

def mid_cap_options(meta: TickerMetadata, as_of) -> bool:
    return mid_cap(meta, as_of) and meta.options_eligible

def no_adr(meta: TickerMetadata, as_of) -> bool:
    # Conservative ADR detector — Alpaca asset_class doesn't distinguish,
    # so we filter via known ADR exchanges (OTC) + asset_class
    return bool(meta.tradable and meta.status == "active" and meta.exchange not in ("OTC",))

def no_otc(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.tradable and meta.status == "active" and meta.exchange != "OTC")

def top500_by_adv(meta: TickerMetadata, as_of) -> bool:
    # Approximation: rely on adv_usd_20d ranking computed at metadata write time.
    # adv_usd_20d_rank is encoded inline as a derived value when None means
    # "outside top 500".
    if meta.adv_usd_20d is None:
        return False
    return meta.adv_usd_20d >= 50e6 and bool(meta.in_r3000)

CANDIDATE_PREDICATES = {
    "sp500": sp500,
    "r1000": r1000,
    "r3000": r3000,
    "options_eligible_only": options_eligible_only,
    "large_cap": large_cap,
    "mid_cap": mid_cap,
    "small_cap_liquid": small_cap_liquid,
    "large_cap_options": large_cap_options,
    "mid_cap_options": mid_cap_options,
    "no_adr": no_adr,
    "no_otc": no_otc,
    "top500_by_adv": top500_by_adv,
}
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_universe_predicates.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/universe_default.py tests/test_universe_predicates.py
git commit -m "feat(sp2): default predicate + 12 candidate set"
```

---

## Task 6: Universe predicate lint

**Files:**
- Create: `src/strategies/universe_lint.py`
- Create: `scripts/lint_universe_predicates.py`
- Test: `tests/test_universe_lint.py`
- Test fixtures: `tests/fixtures/universe_lint/{good_predicate.py, bad_signature.py, bad_import.py, transitive_today.py, helper_with_today.py}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_universe_lint.py
from pathlib import Path
import pytest
from src.strategies.universe_lint import scan_module, LintError

FIX = Path(__file__).parent / "fixtures" / "universe_lint"

def test_good_predicate_passes():
    errors = scan_module(FIX / "good_predicate.py")
    assert errors == []

def test_bad_signature_fails():
    errors = scan_module(FIX / "bad_signature.py")
    assert any("signature" in e.message for e in errors)

def test_bad_import_fails():
    errors = scan_module(FIX / "bad_import.py")
    assert any("forbidden import" in e.message for e in errors)

def test_transitive_today_fails():
    # Module imports a helper that itself imports datetime
    errors = scan_module(FIX / "transitive_today.py")
    assert any("transitive" in e.message for e in errors)

def test_scan_module_returns_pathed_errors():
    errors = scan_module(FIX / "bad_signature.py")
    assert all(str(FIX) in str(e.path) for e in errors)
```

- [ ] **Step 2: Create fixtures**

```python
# tests/fixtures/universe_lint/good_predicate.py
from src.strategies.universe_meta import TickerMetadata
def universe_filter(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_sp500)
```

```python
# tests/fixtures/universe_lint/bad_signature.py
from src.strategies.universe_meta import TickerMetadata
def universe_filter(meta: TickerMetadata) -> bool:
    return True
```

```python
# tests/fixtures/universe_lint/bad_import.py
from datetime import datetime
from src.strategies.universe_meta import TickerMetadata
def universe_filter(meta: TickerMetadata, as_of) -> bool:
    return datetime.now().year > 2020 and bool(meta.in_sp500)
```

```python
# tests/fixtures/universe_lint/helper_with_today.py
from datetime import date
def is_recent():
    return date.today().year >= 2026
```

```python
# tests/fixtures/universe_lint/transitive_today.py
from src.strategies.universe_meta import TickerMetadata
from tests.fixtures.universe_lint.helper_with_today import is_recent
def universe_filter(meta: TickerMetadata, as_of) -> bool:
    return is_recent() and bool(meta.in_sp500)
```

- [ ] **Step 3: Verify the test fails**

Run: `pytest tests/test_universe_lint.py -v`
Expected: `ModuleNotFoundError` (`universe_lint` missing).

- [ ] **Step 4: Implement the lint**

```python
# src/strategies/universe_lint.py
"""AST-based linter for universe_filter predicates.

Enforces:
  1. Signature: def universe_filter(meta, as_of) -> bool
  2. No imports of datetime, time, os, calendar in the predicate module
  3. No first-order callees that themselves import the forbidden modules
"""
from __future__ import annotations
import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_IMPORTS = {"datetime", "time", "os", "calendar"}
EXPECTED_PARAMS = ("meta", "as_of")

@dataclass
class LintError:
    path: Path
    line: int
    message: str

def _find_universe_filter(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "universe_filter":
            return node
    return None

def _signature_ok(fn: ast.FunctionDef) -> bool:
    args = fn.args.args
    if len(args) != len(EXPECTED_PARAMS):
        return False
    return [a.arg for a in args] == list(EXPECTED_PARAMS)

def _forbidden_imports(tree: ast.AST) -> list[str]:
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_IMPORTS:
                bad.append(node.module)
    return bad

def _local_imports(tree: ast.AST) -> list[str]:
    """Return import paths that look local (start with src. or tests.)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith(("src.", "tests.")):
                out.append(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("src.", "tests.")):
                    out.append(alias.name)
    return out

def _module_file(import_path: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(import_path)
        if spec and spec.origin:
            return Path(spec.origin)
    except (ImportError, ValueError, ModuleNotFoundError):
        pass
    return None

def scan_module(path: Path) -> list[LintError]:
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    errors: list[LintError] = []
    fn = _find_universe_filter(tree)
    if fn is None:
        return errors  # no predicate in this module; nothing to lint
    if not _signature_ok(fn):
        errors.append(LintError(path, fn.lineno,
            "universe_filter signature must be (meta, as_of)"))
    for bad in _forbidden_imports(tree):
        errors.append(LintError(path, 1,
            f"forbidden import in predicate module: {bad}"))
    for local in _local_imports(tree):
        f = _module_file(local)
        if f is None or not f.exists():
            continue
        callee_tree = ast.parse(f.read_text(), filename=str(f))
        bad_transitive = _forbidden_imports(callee_tree)
        if bad_transitive:
            errors.append(LintError(path, 1,
                f"transitive forbidden import via {local}: {bad_transitive}"))
    return errors
```

```python
# scripts/lint_universe_predicates.py
#!/usr/bin/env python3
"""CI gate: scan src/strategies/implementations/*.py + universe_default.py."""
import sys
from pathlib import Path
from src.strategies.universe_lint import scan_module

ROOTS = [
    Path("src/strategies/universe_default.py"),
    *Path("src/strategies/implementations").rglob("S*.py"),
]

def main() -> int:
    total = 0
    for p in ROOTS:
        if not p.exists(): continue
        for e in scan_module(p):
            print(f"{e.path}:{e.line} {e.message}")
            total += 1
    if total:
        print(f"\n{total} predicate lint error(s)")
        return 1
    print(f"scanned {len(ROOTS)} files — clean")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Verify tests pass**

Run: `pytest tests/test_universe_lint.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run lint against current tree (should pass — no predicates yet)**

Run: `python3 scripts/lint_universe_predicates.py`
Expected: `scanned 103 files — clean` (102 strategies + universe_default.py).

- [ ] **Step 7: Commit**

```bash
git add src/strategies/universe_lint.py scripts/lint_universe_predicates.py \
        tests/test_universe_lint.py tests/fixtures/universe_lint/
git commit -m "feat(sp2): universe_filter AST lint + CI gate"
```

---

## Task 7: `UniverseResolver` core

**Files:**
- Create: `src/strategies/universe_resolver.py`
- Test: `tests/test_universe_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_universe_resolver.py
from datetime import date
import pytest
from src.strategies.universe_resolver import UniverseResolver, AsOfInFutureError
from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_default import sp500, options_eligible_only

class FakeDB:
    def __init__(self, snapshots):
        self.snapshots = snapshots
    def fetch_metadata_as_of(self, as_of):
        rows = [s for s in self.snapshots if s.snapshot_date <= as_of]
        latest = {}
        for s in sorted(rows, key=lambda r: r.snapshot_date):
            latest[s.symbol] = s
        return list(latest.values())

class FakeCoverage:
    def __init__(self, coverage_map):
        self.cov = coverage_map
    def has_floor(self, symbol, as_of):
        return self.cov.get(symbol, True)

@pytest.fixture
def db():
    s1 = type("Row", (), {})()
    s1.snapshot_date = date(2026, 1, 1)
    s1.metadata = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="IT", industry="CE",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=date(1980, 12, 12), delisted_date=None,
    )
    s1.symbol = "AAPL"
    return FakeDB([s1])

def test_resolve_returns_predicate_matches(db, monkeypatch):
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    result = resolver.resolve("S5", as_of=date(2026, 6, 1))
    assert result == ["AAPL"]

def test_resolve_cache_hit(db, monkeypatch):
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    resolver.resolve("S5", date(2026, 6, 1))
    db_calls_before = sum(1 for _ in db.fetch_metadata_as_of(date(2026, 6, 1)))
    # second call should hit cache
    resolver.resolve("S5", date(2026, 6, 1))
    # FakeDB has no counter; instead assert cache present
    assert ("S5", date(2026, 6, 1)) in resolver._cache

def test_resolve_refuses_future(db, monkeypatch):
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    from datetime import timedelta, date as _d
    future = _d.today() + timedelta(days=1)
    with pytest.raises(AsOfInFutureError):
        resolver.resolve("S5", as_of=future)

def test_resolve_excludes_no_coverage(db, monkeypatch):
    resolver = UniverseResolver(
        db=db, coverage=FakeCoverage({"AAPL": False})
    )
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    result = resolver.resolve("S5", as_of=date(2026, 6, 1))
    assert result == []
```

- [ ] **Step 2: Verify the test fails**

Run: `pytest tests/test_universe_resolver.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the resolver**

```python
# src/strategies/universe_resolver.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import date as _date
from typing import Callable, Iterable, Protocol
import importlib

from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_default import DEFAULT_UNIVERSE_FILTER


class AsOfInFutureError(ValueError):
    """Raised when resolve(as_of) > today() — defense against look-ahead bias."""


class _DBProtocol(Protocol):
    def fetch_metadata_as_of(self, as_of: _date) -> list: ...

class _CoverageProtocol(Protocol):
    def has_floor(self, symbol: str, as_of: _date) -> bool: ...


class UniverseResolver:
    def __init__(self, db: _DBProtocol, coverage: _CoverageProtocol,
                 manifest_loader: Callable[[], dict] | None = None,
                 today_fn: Callable[[], _date] = _date.today):
        self._db = db
        self._coverage = coverage
        self._cache: dict[tuple[str, _date], list[str]] = {}
        self._manifest_loader = manifest_loader
        self._today_fn = today_fn

    def _load_predicate(self, strategy_id: str) -> Callable[[TickerMetadata, _date], bool]:
        if self._manifest_loader is None:
            return DEFAULT_UNIVERSE_FILTER
        manifest = self._manifest_loader()
        ref = (manifest.get("strategies", {}).get(strategy_id, {})
                       .get("metadata", {}).get("universe_filter_ref"))
        if ref is None:
            return DEFAULT_UNIVERSE_FILTER
        mod_path, attr = ref.rsplit(":", 1)
        module = importlib.import_module(mod_path)
        return getattr(module, attr)

    def resolve(self, strategy_id: str, as_of: _date) -> list[str]:
        if as_of > self._today_fn():
            raise AsOfInFutureError(f"as_of {as_of} > today {self._today_fn()}")
        key = (strategy_id, as_of)
        if key in self._cache:
            return self._cache[key]
        predicate = self._load_predicate(strategy_id)
        rows = self._db.fetch_metadata_as_of(as_of)
        out = []
        for row in rows:
            meta = row.metadata if hasattr(row, "metadata") else TickerMetadata.from_row(row)
            try:
                if predicate(meta, as_of) and self._coverage.has_floor(meta.symbol, as_of):
                    out.append(meta.symbol)
            except Exception:
                # Defensive: a broken predicate skips the ticker; lifecycle
                # sandbox check should have caught this earlier.
                continue
        out.sort()
        self._cache[key] = out
        return out
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_universe_resolver.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/universe_resolver.py tests/test_universe_resolver.py
git commit -m "feat(sp2): UniverseResolver.resolve() with cache + as_of ceiling"
```

---

## Task 8: `union_universe` + Redis caching

**Files:**
- Modify: `src/strategies/universe_resolver.py`
- Test: extend `tests/test_universe_resolver.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_universe_resolver.py`:

```python
def test_union_universe_aggregates(db, monkeypatch):
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    monkeypatch.setattr(resolver, "_live_strategy_ids", lambda states: ["S5", "S15"])
    result = resolver.union_universe(date(2026, 6, 1), states=("live",))
    assert result == ["AAPL"]  # both S5 and S15 resolve to {AAPL}, union dedupes

def test_union_universe_writes_audit(db, monkeypatch):
    audit_rows = []
    resolver = UniverseResolver(db=db, coverage=FakeCoverage({}),
                                 audit_writer=lambda row: audit_rows.append(row))
    monkeypatch.setattr(resolver, "_load_predicate", lambda sid: sp500)
    monkeypatch.setattr(resolver, "_live_strategy_ids", lambda states: ["S5"])
    monkeypatch.setattr(resolver, "_alpaca_universe_size", lambda: 8421)
    resolver.union_universe(date(2026, 6, 1), states=("live",))
    assert len(audit_rows) == 1
    a = audit_rows[0]
    assert a["resolved_for_date"] == date(2026, 6, 1)
    assert a["union_size"] == 1
    assert a["per_strategy_sizes"] == {"S5": 1}
    assert a["alpaca_universe_size"] == 8421
```

- [ ] **Step 2: Verify failures**

Run: `pytest tests/test_universe_resolver.py -v -k union`
Expected: 2 failed (`union_universe` undefined).

- [ ] **Step 3: Extend resolver**

Append to `src/strategies/universe_resolver.py`:

```python
    # In __init__, add:
    #   audit_writer: Callable[[dict], None] | None = None
    # and: self._audit_writer = audit_writer
    #
    # Replace the existing __init__ with:

    def __init__(self, db: _DBProtocol, coverage: _CoverageProtocol,
                 manifest_loader: Callable[[], dict] | None = None,
                 today_fn: Callable[[], _date] = _date.today,
                 audit_writer: Callable[[dict], None] | None = None):
        self._db = db
        self._coverage = coverage
        self._cache: dict[tuple[str, _date], list[str]] = {}
        self._manifest_loader = manifest_loader
        self._today_fn = today_fn
        self._audit_writer = audit_writer

    def _live_strategy_ids(self, states: tuple[str, ...]) -> list[str]:
        if self._manifest_loader is None:
            return []
        manifest = self._manifest_loader()
        return [sid for sid, rec in manifest.get("strategies", {}).items()
                if rec.get("state") in states]

    def _alpaca_universe_size(self) -> int:
        # Overridden in production with a DB count; default 0 for tests
        return 0

    def union_universe(self, as_of: _date, states: tuple[str, ...] = ("live",)) -> list[str]:
        import time
        t0 = time.monotonic()
        per_strategy: dict[str, int] = {}
        seen: set[str] = set()
        for sid in self._live_strategy_ids(states):
            tickers = self.resolve(sid, as_of)
            per_strategy[sid] = len(tickers)
            seen.update(tickers)
        union = sorted(seen)
        if self._audit_writer:
            self._audit_writer({
                "resolved_for_date": as_of,
                "lifecycle_states": list(states),
                "union_size": len(union),
                "per_strategy_sizes": per_strategy,
                "alpaca_universe_size": self._alpaca_universe_size(),
                "resolver_ms": int((time.monotonic() - t0) * 1000),
            })
        return union
```

Replace the original `__init__` block in the file with the new one (do not duplicate).

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_universe_resolver.py -v`
Expected: 6 passed (4 previous + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/strategies/universe_resolver.py tests/test_universe_resolver.py
git commit -m "feat(sp2): UniverseResolver.union_universe + audit"
```

---

## Task 9: `StrategyRecord.universe_filter_ref` (lifecycle threading)

**Files:**
- Modify: `src/strategies/lifecycle.py` (lines 103 dataclass; 153 from_manifest; 480 to_dict)
- Test: `tests/test_lifecycle_universe_filter_ref.py`

- [ ] **Step 1: Write the failing regression test** (the silent-strip pitfall)

```python
# tests/test_lifecycle_universe_filter_ref.py
import json
import pytest
from pathlib import Path
from src.strategies.lifecycle import LifecycleStateMachine, StrategyRecord, StrategyState

MANIFEST = """
{
  "schema_version": "1.0",
  "strategies": {
    "S_test": {
      "state": "live",
      "state_since": "2026-01-01T00:00:00+00:00",
      "metadata": {
        "canonical_file": "S_test.py",
        "class": "Test",
        "description": "Probe",
        "universe_filter_ref": "src.strategies.universe_default:options_eligible_only"
      },
      "history": []
    }
  }
}
"""

def test_loads_universe_filter_ref(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(MANIFEST)
    lsm = LifecycleStateMachine.from_manifest(p)
    rec = lsm.get("S_test")
    assert rec.universe_filter_ref == "src.strategies.universe_default:options_eligible_only"

def test_roundtrips_universe_filter_ref(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(MANIFEST)
    lsm = LifecycleStateMachine.from_manifest(p)
    lsm.save_manifest(p)
    payload = json.loads(p.read_text())
    assert (payload["strategies"]["S_test"]["metadata"]["universe_filter_ref"]
            == "src.strategies.universe_default:options_eligible_only")

def test_default_when_missing(tmp_path):
    payload = json.loads(MANIFEST)
    del payload["strategies"]["S_test"]["metadata"]["universe_filter_ref"]
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(payload))
    lsm = LifecycleStateMachine.from_manifest(p)
    assert lsm.get("S_test").universe_filter_ref is None
```

- [ ] **Step 2: Verify the test fails**

Run: `pytest tests/test_lifecycle_universe_filter_ref.py -v`
Expected: `AttributeError: 'StrategyRecord' object has no attribute 'universe_filter_ref'`.

- [ ] **Step 3: Add field to StrategyRecord**

Modify `src/strategies/lifecycle.py` around line 103 — add field to the dataclass:

```python
@dataclass
class StrategyRecord:
    # ... existing fields ...
    universe_filter_ref: Optional[str] = None   # NEW — Phase A
```

- [ ] **Step 4: Thread through `from_manifest` (around line 153)**

Inside the per-strategy parse loop, when constructing `StrategyRecord`, read:

```python
universe_filter_ref = data.get("metadata", {}).get("universe_filter_ref")
```

and pass `universe_filter_ref=universe_filter_ref` to the constructor.

- [ ] **Step 5: Thread through `to_dict` (around line 480)**

Inside the per-strategy serializer, when writing the strategy dict, add to `metadata`:

```python
if record.universe_filter_ref is not None:
    metadata_dict["universe_filter_ref"] = record.universe_filter_ref
```

(Do not write None — keep absent for defaulted strategies.)

- [ ] **Step 6: Verify tests pass**

Run: `pytest tests/test_lifecycle_universe_filter_ref.py -v`
Expected: 3 passed.

- [ ] **Step 7: Verify existing lifecycle tests still pass**

Run: `pytest tests/test_strategy_lifecycle*.py -v`
Expected: 100% prior tests still green.

- [ ] **Step 8: Commit**

```bash
git add src/strategies/lifecycle.py tests/test_lifecycle_universe_filter_ref.py
git commit -m "feat(sp2): StrategyRecord.universe_filter_ref + manifest round-trip"
```

---

## Task 10: Lifecycle sandbox check at transition

When a strategy's `universe_filter_ref` changes during a lifecycle transition, evaluate the predicate twice — once with the real clock, once with a frozen clock at 2020-01-01 — against a fixed `TickerMetadata` fixture. If they differ, reject the transition.

**Files:**
- Modify: `src/strategies/lifecycle.py` (transition method)
- Test: extend `tests/test_lifecycle_universe_filter_ref.py`

- [ ] **Step 1: Add failing test**

```python
def test_transition_rejects_predicate_with_clock_drift(tmp_path):
    # Create a fixture module that returns different output based on today()
    bad_mod = tmp_path / "bad_predicate.py"
    bad_mod.write_text(
        "from datetime import date as _d\n"
        "def universe_filter(meta, as_of):\n"
        "    return bool(meta.in_sp500) and _d.today().year >= 2020\n"
    )
    # ... manifest setup, attempt transition setting universe_filter_ref
    # ... to "bad_predicate:universe_filter" — expect rejection
    # (Full setup requires sys.path manipulation; pseudo-test for spec clarity.)
```

This test is tricky because it relies on dynamic-module loading. Implement using a real predicate stored in `tests/fixtures/sp2/`:

```python
# tests/fixtures/sp2/clock_dependent_predicate.py
from datetime import date as _d
def universe_filter(meta, as_of):
    return bool(meta.in_sp500) and _d.today().year >= 2020
```

Concrete test:

```python
def test_transition_rejects_clock_dependent_predicate(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "sp2"))
    p = tmp_path / "manifest.json"
    payload = json.loads(MANIFEST)
    payload["strategies"]["S_test"]["state"] = "candidate"
    p.write_text(json.dumps(payload))
    lsm = LifecycleStateMachine.from_manifest(p)
    with pytest.raises(ValueError, match="predicate behavior differs"):
        lsm.transition("S_test", StrategyState.LIVE,
                       actor="system", reason="test",
                       metadata={"universe_filter_ref": "clock_dependent_predicate:universe_filter"})
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_lifecycle_universe_filter_ref.py::test_transition_rejects_clock_dependent_predicate -v`
Expected: fails because transition currently does not sandbox-check.

- [ ] **Step 3: Implement sandbox check**

In `src/strategies/lifecycle.py`, inside `transition`, after applying `metadata.universe_filter_ref` to the record, before persisting:

```python
def _sandbox_check_predicate(predicate_ref: str) -> None:
    """Evaluate the predicate with real and frozen clock; reject if outputs differ.
    Requires `freezegun` (already a dev dep)."""
    if predicate_ref is None:
        return
    import importlib, freezegun
    from datetime import date as _d
    from src.strategies.universe_meta import TickerMetadata
    fixture = TickerMetadata(
        symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
        status="active", tradable=True, shortable=True,
        fractionable=True, easy_to_borrow=True,
        market_cap=3.5e12, adv_usd_20d=1.8e10,
        sector="IT", industry="CE",
        options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
        listed_date=_d(1980, 12, 12), delisted_date=None,
    )
    mod_path, attr = predicate_ref.rsplit(":", 1)
    module = importlib.import_module(mod_path)
    fn = getattr(module, attr)
    as_of = _d(2024, 1, 15)  # fixed historical date
    real = fn(fixture, as_of)
    with freezegun.freeze_time("2020-01-01"):
        frozen = fn(fixture, as_of)
    if real != frozen:
        raise ValueError(
            f"predicate behavior differs between real ({real}) and frozen ({frozen}) clock — "
            f"likely reads today()/now()/env. Predicate: {predicate_ref}"
        )
```

Then call `_sandbox_check_predicate(new_ref)` inside `transition`.

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_lifecycle_universe_filter_ref.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/lifecycle.py tests/fixtures/sp2/clock_dependent_predicate.py tests/test_lifecycle_universe_filter_ref.py
git commit -m "feat(sp2): lifecycle sandbox-check rejects clock-dependent predicates"
```

---

## Task 11: `ticker_metadata_writer.py` (daily live writer)

Writes today's TickerMetadata row from `alpaca_tradable_universe` × FMP `profile` × `prices.parquet` × options eligibility cache. Idempotent on `(snapshot_date, symbol)`. Source decision (FMP endpoint vs prices × shares) follows the Task 1 probe result.

**Files:**
- Create: `src/pipeline/ticker_metadata_writer.py`
- Test: `tests/test_ticker_metadata_writer.py`

- [ ] **Step 1: Write the failing test** (with mocked Alpaca + FMP + parquet sources)

```python
# tests/test_ticker_metadata_writer.py
from datetime import date
import os, psycopg
import pytest
from src.pipeline.ticker_metadata_writer import write_snapshots, build_metadata_rows

@pytest.fixture
def fake_sources():
    alpaca_rows = [{
        "symbol": "AAPL", "asset_class": "us_equity", "exchange": "NASDAQ",
        "status": "active", "tradable": True, "shortable": True,
        "fractionable": True, "easy_to_borrow": True,
        "first_seen_at": "1980-12-12", "last_seen_at": "2026-05-22",
    }]
    fmp_profile = {"AAPL": {"sector": "Information Technology",
                            "industry": "Consumer Electronics",
                            "mktCap": 3.5e12, "ipoDate": "1980-12-12"}}
    prices_parquet = {"AAPL": {"adv_usd_20d": 1.8e10}}
    options_cache = {"AAPL": True}
    return alpaca_rows, fmp_profile, prices_parquet, options_cache

def test_build_metadata_rows(fake_sources):
    rows = build_metadata_rows(date(2026, 5, 22), *fake_sources, source_tag="live_daily")
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "AAPL"
    assert r["market_cap"] == 3.5e12
    assert r["adv_usd_20d"] == 1.8e10
    assert r["options_eligible"] is True
    assert r["sector"] == "Information Technology"
    assert r["source_tag"] == "live_daily"
    assert r["in_sp500"] is True  # hardcoded list from universe.js includes AAPL

def test_idempotent_write_db(fake_sources):
    rows = build_metadata_rows(date(2026, 5, 22), *fake_sources, source_tag="test_idem")
    n1 = write_snapshots(os.environ["DATABASE_URL_TEST"], rows)
    n2 = write_snapshots(os.environ["DATABASE_URL_TEST"], rows)  # re-run
    assert n1 == 1
    assert n2 == 1  # UPSERT returns 1 affected; same row
    with psycopg.connect(os.environ["DATABASE_URL_TEST"]) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM ticker_metadata_snapshots WHERE source_tag='test_idem'")
        assert cur.fetchone()[0] == 1
        cur.execute("DELETE FROM ticker_metadata_snapshots WHERE source_tag='test_idem'")
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_ticker_metadata_writer.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement writer**

```python
# src/pipeline/ticker_metadata_writer.py
from __future__ import annotations
from datetime import date
from typing import Iterable
import psycopg

# Hardcoded SP500 set, sourced from src/pipeline/universe.js
# This is the documented current-state-as-history proxy for the in_sp500 field.
# Future enhancement: Wikipedia historical lists.
from src.strategies._sp500_membership import SP500_SET  # see Task 11.1

UPSERT_SQL = """
INSERT INTO ticker_metadata_snapshots (
    snapshot_date, symbol, asset_class, exchange, status,
    tradable, shortable, fractionable, easy_to_borrow,
    market_cap, adv_usd_20d, sector, industry, options_eligible,
    in_sp500, in_r1000, in_r3000, listed_date, delisted_date, source_tag
) VALUES (
    %(snapshot_date)s, %(symbol)s, %(asset_class)s, %(exchange)s, %(status)s,
    %(tradable)s, %(shortable)s, %(fractionable)s, %(easy_to_borrow)s,
    %(market_cap)s, %(adv_usd_20d)s, %(sector)s, %(industry)s, %(options_eligible)s,
    %(in_sp500)s, %(in_r1000)s, %(in_r3000)s, %(listed_date)s, %(delisted_date)s, %(source_tag)s
)
ON CONFLICT (snapshot_date, symbol) DO UPDATE SET
    asset_class=EXCLUDED.asset_class, exchange=EXCLUDED.exchange, status=EXCLUDED.status,
    tradable=EXCLUDED.tradable, shortable=EXCLUDED.shortable,
    fractionable=EXCLUDED.fractionable, easy_to_borrow=EXCLUDED.easy_to_borrow,
    market_cap=EXCLUDED.market_cap, adv_usd_20d=EXCLUDED.adv_usd_20d,
    sector=EXCLUDED.sector, industry=EXCLUDED.industry,
    options_eligible=EXCLUDED.options_eligible,
    in_sp500=EXCLUDED.in_sp500, in_r1000=EXCLUDED.in_r1000, in_r3000=EXCLUDED.in_r3000,
    listed_date=EXCLUDED.listed_date, delisted_date=EXCLUDED.delisted_date,
    source_tag=EXCLUDED.source_tag
"""

def _rank_r1000_r3000(rows: list[dict]) -> tuple[set, set]:
    ranked = sorted(
        ((r["symbol"], r.get("market_cap") or 0.0) for r in rows),
        key=lambda x: -x[1],
    )
    r1000 = {s for s, _ in ranked[:1000]}
    r3000 = {s for s, _ in ranked[:3000]}
    return r1000, r3000

def build_metadata_rows(
    snapshot_date: date, alpaca_rows: list[dict], fmp_profile: dict,
    prices_parquet: dict, options_cache: dict,
    source_tag: str,
) -> list[dict]:
    enriched = []
    for a in alpaca_rows:
        sym = a["symbol"]
        p = fmp_profile.get(sym, {})
        pp = prices_parquet.get(sym, {})
        enriched.append({
            "symbol": sym,
            "asset_class": a["asset_class"],
            "exchange": a.get("exchange"),
            "status": a["status"],
            "tradable": a.get("tradable", False),
            "shortable": a.get("shortable", False),
            "fractionable": a.get("fractionable", False),
            "easy_to_borrow": a.get("easy_to_borrow", False),
            "market_cap": p.get("mktCap"),
            "adv_usd_20d": pp.get("adv_usd_20d"),
            "sector": p.get("sector"),
            "industry": p.get("industry"),
            "options_eligible": options_cache.get(sym, False),
            "in_sp500": sym in SP500_SET,
            "in_r1000": False,  # filled below
            "in_r3000": False,  # filled below
            "listed_date": p.get("ipoDate") or a.get("first_seen_at"),
            "delisted_date": None if a["status"] == "active" else a.get("last_seen_at"),
        })
    r1000, r3000 = _rank_r1000_r3000(enriched)
    for r in enriched:
        r["in_r1000"] = r["symbol"] in r1000
        r["in_r3000"] = r["symbol"] in r3000
        r["snapshot_date"] = snapshot_date
        r["source_tag"] = source_tag
    return enriched

def write_snapshots(dsn: str, rows: list[dict]) -> int:
    written = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(UPSERT_SQL, r)
            written += 1
        conn.commit()
    return written
```

- [ ] **Step 4: Create Task 11.1 — extract SP500 set as a Python module**

```python
# src/strategies/_sp500_membership.py
"""
Hardcoded SP500 membership projected to history as the in_sp500 proxy.
Sourced from src/pipeline/universe.js — kept in sync manually until a
historical-membership data source is added (future enhancement).
"""
SP500_SET = frozenset({
    # paste the SP500 list from src/pipeline/universe.js (the ~510 ticker set)
    "AAPL", "MSFT", "NVDA", "AVGO", "CRM", "ORCL", "AMD", "QCOM", "ADBE", "TXN",
    # ... (paste full list from universe.js SP500 array; deduplicated)
})
```

Run `node -e "const u = require('./src/pipeline/universe.js'); console.log(JSON.stringify([...new Set(u.SP500)]))"` and paste the JSON list into `SP500_SET` literal.

- [ ] **Step 5: Verify tests pass**

Run: `pytest tests/test_ticker_metadata_writer.py -v`
Expected: 2 passed.

- [ ] **Step 6: Add CLI entrypoint at the bottom of `ticker_metadata_writer.py`**

```python
if __name__ == "__main__":
    import argparse, os, json
    from datetime import date
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()))
    ap.add_argument("--source-tag", default="live_daily")
    args = ap.parse_args()
    # Production: fetch from live sources. In CLI mode we just verify wiring.
    # See src/pipeline/run_ticker_metadata_step.py (Task 11.2) for the live driver.
    print(json.dumps({"ok": True, "date": args.date, "source_tag": args.source_tag}))
```

- [ ] **Step 7: Commit**

```bash
git add src/pipeline/ticker_metadata_writer.py src/strategies/_sp500_membership.py tests/test_ticker_metadata_writer.py
git commit -m "feat(sp2): ticker_metadata_writer + SP500 hardcoded set"
```

---

## Task 11.2: Live driver — fetch + write today's snapshot

**Files:**
- Create: `src/pipeline/run_ticker_metadata_step.py`
- Test: extend `tests/test_ticker_metadata_writer.py` with a smoke test
- Modify: `src/maintenance/refresh_tradable_universe.py` (chain ticker_metadata_writer post-refresh)

- [ ] **Step 1: Write driver**

```python
# src/pipeline/run_ticker_metadata_step.py
"""Daily live-mode driver. Reads alpaca_tradable_universe + FMP profile cache
+ prices.parquet → writes today's ticker_metadata_snapshots row."""
from __future__ import annotations
import os, sys, json
from datetime import date
import psycopg
import pandas as pd
from src.pipeline.ticker_metadata_writer import build_metadata_rows, write_snapshots

DSN = os.environ["DATABASE_URL"]
PRICES_PARQUET = "/root/openclaw/data/master/prices.parquet"
FMP_PROFILE_CACHE = "/root/openclaw/data/.cache/fmp_profile.json"
OPTIONS_ELIGIBILITY_CACHE = "/root/openclaw/data/.cache/options_eligibility.json"

def fetch_alpaca_universe():
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            SELECT symbol, asset_class, exchange, status, tradable, shortable,
                   fractionable, easy_to_borrow, first_seen_at, last_seen_at
            FROM alpaca_tradable_universe
            WHERE status='active'
        """)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

def load_json(path):
    try:
        with open(path) as f: return json.load(f)
    except FileNotFoundError:
        return {}

def adv_usd_from_parquet(symbols):
    if not os.path.exists(PRICES_PARQUET):
        return {s: {"adv_usd_20d": None} for s in symbols}
    df = pd.read_parquet(PRICES_PARQUET, columns=["symbol", "date", "close", "volume"])
    df = df[df["symbol"].isin(symbols)]
    df["dollar_volume"] = df["close"] * df["volume"]
    last_20 = (df.sort_values("date").groupby("symbol")
                 .tail(20).groupby("symbol")["dollar_volume"].mean())
    return {s: {"adv_usd_20d": float(last_20.get(s, 0.0))} for s in symbols}

def main():
    today = date.today()
    alpaca_rows = fetch_alpaca_universe()
    symbols = [r["symbol"] for r in alpaca_rows]
    fmp_profile = load_json(FMP_PROFILE_CACHE)
    options_cache = load_json(OPTIONS_ELIGIBILITY_CACHE)
    prices_parquet = adv_usd_from_parquet(symbols)
    rows = build_metadata_rows(
        today, alpaca_rows, fmp_profile, prices_parquet, options_cache,
        source_tag="live_daily",
    )
    n = write_snapshots(DSN, rows)
    print(json.dumps({"date": str(today), "rows": n}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Wire into refresh_tradable_universe.py**

At the bottom of `src/maintenance/refresh_tradable_universe.py`, after a successful refresh:

```python
# Chain ticker_metadata_writer (SP-2 Phase A)
import subprocess
subprocess.run(
    ["python3", "-m", "src.pipeline.run_ticker_metadata_step"],
    check=False,  # non-fatal if writer fails; doctor will detect staleness
)
```

- [ ] **Step 3: Smoke test the driver dry-run**

```bash
python3 -m src.pipeline.run_ticker_metadata_step
```

Expected: JSON output with `{"date":"2026-05-22","rows":<N>}` and N≥500.

- [ ] **Step 4: Commit**

```bash
git add src/pipeline/run_ticker_metadata_step.py src/maintenance/refresh_tradable_universe.py
git commit -m "feat(sp2): daily live ticker_metadata writer wired into refresh"
```

---

## Task 12: Backtest engine `as_of` integration

5 backtest entry points must resolve universe per-bar instead of universe-at-start.

**Files:**
- Modify: `src/backtest/unified_backtest.py`
- Modify: `src/backtest/quick_backtest.py`
- Modify: `src/backtest/regime_blended_backtest.py`
- Modify: `src/backtest/intraday_regime_backtest.py`
- Modify: `src/backtest/regime_performance_analyzer.py`
- Test: `tests/test_backtest_as_of.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_as_of.py
from datetime import date, timedelta
from unittest.mock import MagicMock
import pytest
from src.backtest.unified_backtest import run_backtest_with_resolver

class StubResolver:
    def __init__(self):
        self.calls = []
    def resolve(self, sid, as_of):
        self.calls.append((sid, as_of))
        return ["AAPL", "MSFT"]

class StubStrategy:
    id = "S_test"
    def generate(self, bar_date, universe):
        return [{"ticker": universe[0], "size": 1}]

def test_resolver_called_per_bar():
    res = StubResolver()
    out = run_backtest_with_resolver(
        StubStrategy(), start=date(2024, 1, 1), end=date(2024, 1, 5),
        resolver=res,
    )
    assert len(res.calls) >= 3  # at least 3 trading days in window
    assert all(c[0] == "S_test" for c in res.calls)

def test_resolver_passes_bar_date():
    res = StubResolver()
    run_backtest_with_resolver(
        StubStrategy(), start=date(2024, 6, 14), end=date(2024, 6, 14),
        resolver=res,
    )
    assert res.calls[0][1] == date(2024, 6, 14)
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_backtest_as_of.py -v`
Expected: ImportError or missing function.

- [ ] **Step 3: Add `run_backtest_with_resolver` helper to `unified_backtest.py`**

```python
# Insert near top of src/backtest/unified_backtest.py, alongside the existing
# main backtest loop (which keeps its current public signature for compatibility):

def run_backtest_with_resolver(strategy, start, end, resolver, **kwargs):
    """SP-2 Phase A: per-bar universe resolution.
    Strategies that depend on universe must accept a `universe` kw or list.
    Returns the same shape as run_backtest()."""
    from src.backtest._oracle_helpers import trading_days
    results = []
    for bar_date in trading_days(start, end):
        universe = resolver.resolve(strategy.id, as_of=bar_date)
        signals = strategy.generate(bar_date, universe)
        results.append({"date": bar_date, "signals": signals})
    return results
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_backtest_as_of.py -v`
Expected: 2 passed.

- [ ] **Step 5: Plumb through `quick_backtest.py`, `regime_blended_backtest.py`, `intraday_regime_backtest.py`, `regime_performance_analyzer.py`**

For each file: add an optional `resolver` parameter to the main entry point. When provided, replace `for bar_date in <loop>: ... universe=<frozen>` with `universe = resolver.resolve(strategy.id, as_of=bar_date)`. Preserve the legacy code path (no resolver) for backward compat — Phase A doesn't require all callers to pass a resolver yet, only that the option exists.

Each file change is structurally identical:

```python
def run(strategy, start, end, *, resolver=None, universe=None, **kwargs):
    for bar_date in trading_days(start, end):
        if resolver is not None:
            universe = resolver.resolve(strategy.id, as_of=bar_date)
        # ... existing logic with `universe`
```

Add a smoke test per file (4 tiny tests) confirming `resolver.resolve` is called when supplied:

```python
def test_quick_backtest_uses_resolver():
    res = StubResolver()
    from src.backtest.quick_backtest import run as quick_run
    quick_run(StubStrategy(), start=date(2024,1,1), end=date(2024,1,3), resolver=res)
    assert len(res.calls) >= 1
```

(Add similar tests for the other 3 engines in `tests/test_backtest_as_of.py`.)

- [ ] **Step 6: Verify all pass**

Run: `pytest tests/test_backtest_as_of.py -v`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add src/backtest/*.py tests/test_backtest_as_of.py
git commit -m "feat(sp2): backtest engines accept resolver for per-bar as_of universe"
```

---

## Task 13: Collector + options-archive + sentiment consume `union_universe`

**Files:**
- Modify: `src/pipeline/collector.js`
- Modify: `src/pipeline/backfillers/alpaca_options.py`
- Modify: `src/ingestion/alpaca_news.py`
- Modify: `src/pipeline/run_sentiment_step.py`
- Test: `tests/test_pipeline_uses_union_universe.py`

- [ ] **Step 1: Write the failing test (Python side — `alpaca_options.py` + sentiment)**

```python
# tests/test_pipeline_uses_union_universe.py
from datetime import date
from unittest.mock import patch, MagicMock
import pytest

def test_alpaca_options_iterates_union_filtered_to_options_eligible(monkeypatch):
    fake_resolver = MagicMock()
    fake_resolver.union_universe.return_value = ["AAPL", "MSFT", "XYZQ"]
    fake_meta = {
        "AAPL": MagicMock(options_eligible=True),
        "MSFT": MagicMock(options_eligible=True),
        "XYZQ": MagicMock(options_eligible=False),
    }
    from src.pipeline.backfillers.alpaca_options import _select_archive_universe
    out = _select_archive_universe(
        as_of=date(2026, 5, 22), resolver=fake_resolver,
        meta_lookup=lambda s, d: fake_meta[s],
    )
    assert sorted(out) == ["AAPL", "MSFT"]

def test_sentiment_uses_live_plus_candidate(monkeypatch):
    fake_resolver = MagicMock()
    fake_resolver.union_universe.return_value = ["AAPL", "TSLA"]
    from src.pipeline.run_sentiment_step import _select_sentiment_universe
    out = _select_sentiment_universe(as_of=date(2026, 5, 22), resolver=fake_resolver)
    fake_resolver.union_universe.assert_called_with(date(2026, 5, 22), states=("live", "candidate"))
    assert out == ["AAPL", "TSLA"]
```

- [ ] **Step 2: Run + watch fail**

Run: `pytest tests/test_pipeline_uses_union_universe.py -v`
Expected: ImportError or no such function.

- [ ] **Step 3: Add `_select_archive_universe` to `alpaca_options.py`**

```python
# At top of src/pipeline/backfillers/alpaca_options.py (or appropriate section)
def _select_archive_universe(as_of, resolver, meta_lookup):
    union = resolver.union_universe(as_of, states=("live",))
    return [s for s in union if meta_lookup(s, as_of).options_eligible]
```

Then in the main archive loop, replace the "iterate alpaca_tradable_universe WHERE active" path with `for symbol in _select_archive_universe(today, resolver, meta_lookup):`.

- [ ] **Step 4: Add `_select_sentiment_universe` to `run_sentiment_step.py`**

```python
def _select_sentiment_universe(as_of, resolver):
    return resolver.union_universe(as_of, states=("live", "candidate"))
```

Wire it into the sentiment main loop.

- [ ] **Step 5: Modify `collector.js` to read Redis-cached union**

```javascript
// src/pipeline/collector.js — add helper near the top:
async function readUnionUniverseFromRedis(redis, dateStr, states = 'live') {
  const key = `universe:union:${dateStr}:${states}`;
  const cached = await redis.get(key);
  if (cached) return JSON.parse(cached);
  // Fallback: shell out
  const { execSync } = require('child_process');
  try {
    const out = execSync(
      `python3 -m src.strategies.universe_resolver --as-of ${dateStr} --states ${states} --json`,
      { encoding: 'utf8', timeout: 30000 },
    );
    const tickers = JSON.parse(out);
    await redis.set(key, JSON.stringify(tickers), { EX: 14400 });
    return tickers;
  } catch (e) {
    console.warn(`[collector] union_universe resolution failed, falling back to SP500: ${e.message}`);
    const { getUniverse } = require('./universe');
    return getUniverse('SP500');
  }
}

module.exports.readUnionUniverseFromRedis = readUnionUniverseFromRedis;
```

Replace `getUniverse('all')` calls in the collector with `await readUnionUniverseFromRedis(redis, todayStr)` unioned with benchmarks + sector ETFs.

- [ ] **Step 6: Add CLI mode to resolver**

Append to `src/strategies/universe_resolver.py`:

```python
if __name__ == "__main__":
    import argparse, json, os, sys
    from datetime import date as _d
    import psycopg
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--states", default="live")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strategy", help="Resolve a single strategy instead of union")
    args = ap.parse_args()
    # Production wiring: real DB + coverage. Stub here for spec brevity.
    # See implementation for full production wiring (omitted — runtime needs
    # a DB adapter + a CoveragerFloor adapter both of which are straightforward).
    print(json.dumps(["AAPL", "MSFT"]))  # placeholder; production replaces
```

(The full production CLI wiring needs concrete DB + coverage adapters; defer the
full implementation detail to Task 13.1 below to keep this task scoped.)

- [ ] **Step 7: Verify Python tests pass**

Run: `pytest tests/test_pipeline_uses_union_universe.py -v`
Expected: 2 passed.

- [ ] **Step 8: Commit**

```bash
git add src/pipeline/collector.js src/pipeline/backfillers/alpaca_options.py \
        src/ingestion/alpaca_news.py src/pipeline/run_sentiment_step.py \
        src/strategies/universe_resolver.py \
        tests/test_pipeline_uses_union_universe.py
git commit -m "feat(sp2): collector + options-archive + sentiment consume union_universe"
```

---

## Task 13.1: Resolver CLI production wiring

**Files:**
- Modify: `src/strategies/universe_resolver.py` (production `__main__`)
- Create: `src/strategies/_db_adapters.py`
- Test: `tests/test_resolver_cli.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/test_resolver_cli.py
import json, subprocess
def test_resolver_cli_returns_json():
    out = subprocess.check_output(
        ["python3", "-m", "src.strategies.universe_resolver",
         "--as-of", "2026-05-22", "--states", "live", "--json"],
        text=True,
    )
    data = json.loads(out)
    assert isinstance(data, list)
    assert all(isinstance(t, str) for t in data)
```

- [ ] **Step 2: Implement production `_db_adapters.py`**

```python
# src/strategies/_db_adapters.py
from __future__ import annotations
import os, json, psycopg
import pandas as pd
from datetime import date
from src.strategies.universe_meta import TickerMetadata

class PostgresMetadataDB:
    def __init__(self, dsn): self._dsn = dsn
    def fetch_metadata_as_of(self, as_of):
        with psycopg.connect(self._dsn) as c, c.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (symbol)
                    snapshot_date, symbol, asset_class, exchange, status, tradable,
                    shortable, fractionable, easy_to_borrow, market_cap, adv_usd_20d,
                    sector, industry, options_eligible, in_sp500, in_r1000, in_r3000,
                    listed_date, delisted_date
                FROM ticker_metadata_snapshots
                WHERE snapshot_date <= %s
                ORDER BY symbol, snapshot_date DESC
            """, (as_of,))
            cols = [d.name for d in cur.description]
            rows = []
            class _Row: pass
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                row = _Row()
                row.symbol = d.pop("symbol")
                row.snapshot_date = d.pop("snapshot_date")
                row.metadata = TickerMetadata(symbol=row.symbol, **d)
                rows.append(row)
            return rows

class ParquetCoverage:
    def __init__(self, prices_path="/root/openclaw/data/master/prices.parquet",
                 min_bars=60):
        self._path = prices_path
        self._min_bars = min_bars
        self._cache_counts: dict[tuple[str, str], int] = {}
    def has_floor(self, symbol, as_of):
        key = (symbol, as_of.isoformat()[:7])  # cache by month
        if key in self._cache_counts:
            return self._cache_counts[key] >= self._min_bars
        if not os.path.exists(self._path):
            return False
        df = pd.read_parquet(self._path, columns=["symbol", "date"])
        n = ((df["symbol"] == symbol) & (df["date"] <= pd.Timestamp(as_of))).sum()
        self._cache_counts[key] = int(n)
        return self._cache_counts[key] >= self._min_bars
```

- [ ] **Step 3: Update resolver `__main__` to use the adapters**

Replace placeholder `__main__` block from Task 13 with:

```python
if __name__ == "__main__":
    import argparse, json, os, sys
    from datetime import date as _d
    from src.strategies._db_adapters import PostgresMetadataDB, ParquetCoverage
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--states", default="live")
    ap.add_argument("--strategy", help="single strategy")
    args = ap.parse_args()
    as_of = _d.fromisoformat(args.as_of)
    states = tuple(args.states.split(","))
    db = PostgresMetadataDB(os.environ["DATABASE_URL"])
    cov = ParquetCoverage()
    def manifest_loader():
        with open("/root/openclaw/src/strategies/manifest.json") as f:
            return json.load(f)
    resolver = UniverseResolver(db=db, coverage=cov, manifest_loader=manifest_loader)
    if args.strategy:
        out = resolver.resolve(args.strategy, as_of=as_of)
    else:
        out = resolver.union_universe(as_of=as_of, states=states)
    print(json.dumps(out))
```

- [ ] **Step 4: Verify test passes**

Run: `pytest tests/test_resolver_cli.py -v`
Expected: passes (in environment with DATABASE_URL + populated test DB).

- [ ] **Step 5: Commit**

```bash
git add src/strategies/_db_adapters.py src/strategies/universe_resolver.py tests/test_resolver_cli.py
git commit -m "feat(sp2): resolver CLI + production DB/coverage adapters"
```

---

## Task 14: LangGraph daily-cycle.js per-strategy universe propagation

**Files:**
- Modify: `src/agent/graphs/daily-cycle.js`
- Test: `tests/test_daily_cycle_universe_state.js` (or extend `test/graph-smoke.js`)

- [ ] **Step 1: Add state field**

In `src/agent/graphs/daily-cycle.js`, extend the state schema:

```javascript
// Around the state-graph declaration:
const cycleState = Annotation.Root({
  // ... existing fields ...
  perStrategyUniverse: Annotation({ default: () => ({}) }),
});
```

- [ ] **Step 2: Populate after `collect` node**

In the post-collect step (or in `signals` node entry), call the resolver CLI once per LIVE strategy and stash:

```javascript
async function loadPerStrategyUniverse(today, liveStrategies) {
  const result = {};
  for (const sid of liveStrategies) {
    const out = execSync(
      `python3 -m src.strategies.universe_resolver --as-of ${today} --strategy ${sid}`,
      { encoding: 'utf8', timeout: 15000 },
    );
    result[sid] = JSON.parse(out);
  }
  return result;
}
```

In the signals node, attach `state.perStrategyUniverse[sid]` to the per-strategy invocation.

- [ ] **Step 3: Smoke test**

```bash
node test/graph-smoke.js
```

Expected: existing smoke green; `cycleState.perStrategyUniverse` populated when verbose.

- [ ] **Step 4: Commit**

```bash
git add src/agent/graphs/daily-cycle.js test/graph-smoke.js
git commit -m "feat(sp2): daily-cycle.js threads per-strategy universe through state"
```

---

## Task 15: Doctor checks + system_checks

**Files:**
- Modify: `src/maintenance/doctor.py`
- Create: `src/system_checks/check_universe_resolution.py`
- Create: `src/system_checks/check_metadata_snapshot_freshness.py`
- Test: `tests/test_doctor_universe.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor_universe.py
import pytest
from unittest.mock import patch, MagicMock
from src.maintenance.doctor import _check_metadata_snapshot_freshness, _check_union_universe_size

def test_metadata_freshness_passes_when_fresh(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._latest_snapshot_age_days", lambda: 1)
    code, msg = _check_metadata_snapshot_freshness()
    assert code == 0

def test_metadata_freshness_warns_at_2d(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._latest_snapshot_age_days", lambda: 2)
    code, msg = _check_metadata_snapshot_freshness()
    assert code == 1

def test_metadata_freshness_fails_at_4d(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._latest_snapshot_age_days", lambda: 4)
    code, msg = _check_metadata_snapshot_freshness()
    assert code == 2

def test_union_size_warn_below_floor(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._union_universe_size", lambda: 150)
    code, msg = _check_union_universe_size()
    assert code == 1

def test_union_size_fail_below_50(monkeypatch):
    monkeypatch.setattr("src.maintenance.doctor._union_universe_size", lambda: 30)
    code, msg = _check_union_universe_size()
    assert code == 2
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_doctor_universe.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement checks in doctor.py**

```python
# src/maintenance/doctor.py — add:
def _latest_snapshot_age_days() -> int:
    import psycopg, os
    from datetime import date
    with psycopg.connect(os.environ["DATABASE_URL"]) as c, c.cursor() as cur:
        cur.execute("SELECT MAX(snapshot_date) FROM ticker_metadata_snapshots")
        last = cur.fetchone()[0]
    return (date.today() - last).days if last else 999

def _check_metadata_snapshot_freshness():
    age = _latest_snapshot_age_days()
    if age <= 1: return (0, f"snapshot fresh ({age}d)")
    if age <= 3: return (1, f"snapshot stale {age}d (warn)")
    return (2, f"snapshot stale {age}d (FAIL)")

def _union_universe_size() -> int:
    import subprocess, json, os
    from datetime import date
    try:
        out = subprocess.check_output(
            ["python3", "-m", "src.strategies.universe_resolver",
             "--as-of", str(date.today()), "--states", "live"],
            text=True, timeout=20,
        )
        return len(json.loads(out))
    except Exception:
        return 0

def _check_union_universe_size():
    floor = int(os.environ.get("UNIVERSE_RESOLVER_MIN_LIVE_TICKERS", "200"))
    n = _union_universe_size()
    if n >= floor: return (0, f"union={n} ≥ {floor}")
    if n >= 50:    return (1, f"union={n} < {floor} (warn)")
    return (2, f"union={n} < 50 (FAIL)")
```

Register both in the doctor preflight order (after the SP-1 checks):

```python
CHECKS.extend([
    ("metadata_snapshot_freshness", _check_metadata_snapshot_freshness),
    ("union_universe_size", _check_union_universe_size),
])
```

- [ ] **Step 4: Implement system_checks**

```python
# src/system_checks/check_universe_resolution.py
"""Pipeline-tagged check: resolver responds within 1s and returns ≥ floor tickers."""
from .base import check
import subprocess, json, time, os
from datetime import date

@check(tag="pipeline", name="universe_resolution")
def run():
    t0 = time.monotonic()
    out = subprocess.check_output(
        ["python3", "-m", "src.strategies.universe_resolver",
         "--as-of", str(date.today()), "--states", "live"],
        text=True, timeout=30,
    )
    elapsed = time.monotonic() - t0
    n = len(json.loads(out))
    floor = int(os.environ.get("UNIVERSE_RESOLVER_MIN_LIVE_TICKERS", "200"))
    if elapsed > 2.0: return {"ok": False, "msg": f"resolver slow {elapsed:.1f}s"}
    if n < floor: return {"ok": False, "msg": f"union={n} < {floor}"}
    return {"ok": True, "msg": f"union={n}, {elapsed*1000:.0f}ms"}
```

```python
# src/system_checks/check_metadata_snapshot_freshness.py
from .base import check
import os, psycopg
from datetime import date

@check(tag="strategies", name="metadata_snapshot_freshness")
def run():
    with psycopg.connect(os.environ["DATABASE_URL"]) as c, c.cursor() as cur:
        cur.execute("SELECT MAX(snapshot_date) FROM ticker_metadata_snapshots")
        last = cur.fetchone()[0]
    if last is None:
        return {"ok": False, "msg": "no snapshots in table"}
    age = (date.today() - last).days
    if age > 2: return {"ok": False, "msg": f"stale {age}d"}
    return {"ok": True, "msg": f"last={last} ({age}d ago)"}
```

- [ ] **Step 5: Verify**

Run: `pytest tests/test_doctor_universe.py -v && python3 -m system_checks --tag pipeline --check universe_resolution`
Expected: 5 passed; pipeline check passes.

- [ ] **Step 6: Commit**

```bash
git add src/maintenance/doctor.py src/system_checks/check_universe_resolution.py \
        src/system_checks/check_metadata_snapshot_freshness.py tests/test_doctor_universe.py
git commit -m "feat(sp2): doctor + system_checks for resolver + snapshot freshness"
```

---

## Task 16: Operator + user dashboard tiles

**Files:**
- Modify: `src/channels/dashboard/server.js` (operator :7870)
- Modify: `src/channels/api/server.js` (user :3000)
- Modify: corresponding JSX/HTML in each dashboard tree
- Test: smoke navigate via curl

- [ ] **Step 1: Add operator API route** in `src/channels/dashboard/server.js`:

```javascript
app.get('/api/universe-slice', async (req, res) => {
  try {
    const result = await dbQuery(`
      SELECT resolved_for_date, union_size, per_strategy_sizes, resolver_ms
      FROM universe_resolution_audit
      ORDER BY resolved_at DESC
      LIMIT 30
    `);
    res.json(result.rows);
  } catch (e) {
    res.status(500).json({error: e.message});
  }
});
```

- [ ] **Step 2: Add UI tile** in the operator dashboard frontend that:
  - Shows today's union size + 30d sparkline
  - Lists each LIVE strategy with its resolved size today
  - Links per-strategy to the predicate file (path from `strategies/manifest.json`)

- [ ] **Step 3: Add user dashboard panel** in `src/channels/api/server.js`:

```javascript
app.get('/api/pipelines/universe-inflation', async (req, res) => {
  try {
    const result = await dbQuery(`
      SELECT resolved_for_date AS d,
             union_size AS u,
             alpaca_universe_size AS total,
             ROUND(union_size::numeric / NULLIF(alpaca_universe_size,0) * 100, 2) AS pct
      FROM universe_resolution_audit
      ORDER BY resolved_at DESC
      LIMIT 30
    `);
    res.json(result.rows);
  } catch (e) {
    res.status(500).json({error: e.message});
  }
});
```

Wire into Pipeline Diagnostics tab (post 2026-05-22 ship).

- [ ] **Step 4: Smoke test**

```bash
curl -s http://127.0.0.1:7870/api/universe-slice | head -3
curl -s http://127.0.0.1:3000/api/pipelines/universe-inflation | head -3
```

Expected: both return JSON arrays.

- [ ] **Step 5: Commit**

```bash
git add src/channels/dashboard/server.js src/channels/api/server.js
git commit -m "feat(sp2): dashboards expose universe-slice + inflation stats"
```

---

## Task 17: CI gate — predicate lint in pre-commit + GHA

**Files:**
- Modify: `.pre-commit-config.yaml`
- Modify: `.github/workflows/ci.yml` (or whichever workflow runs tests)

- [ ] **Step 1: Add pre-commit hook**

```yaml
# .pre-commit-config.yaml — add at end of hooks:
- repo: local
  hooks:
    - id: universe-predicate-lint
      name: Universe predicate lint (SP-2)
      entry: python3 scripts/lint_universe_predicates.py
      language: system
      pass_filenames: false
      always_run: true
```

- [ ] **Step 2: Add GHA step**

In `.github/workflows/ci.yml`, before tests:

```yaml
- name: Universe predicate lint
  run: python3 scripts/lint_universe_predicates.py
```

- [ ] **Step 3: Test gate locally**

```bash
pre-commit run universe-predicate-lint --all-files
```

Expected: clean (no predicates in any strategy file yet).

- [ ] **Step 4: Test gate fails on bad fixture**

```bash
python3 scripts/lint_universe_predicates.py
# Verify exit 0
cp tests/fixtures/universe_lint/bad_import.py src/strategies/implementations/S_test_lint.py
python3 scripts/lint_universe_predicates.py
# Verify exit 1
rm src/strategies/implementations/S_test_lint.py
```

Expected: exit 0 then exit 1.

- [ ] **Step 5: Commit**

```bash
git add .pre-commit-config.yaml .github/workflows/ci.yml
git commit -m "ci(sp2): predicate lint pre-commit + GHA gate"
```

---

## Task 18: `.env` + documentation + memory updates

**Files:**
- Modify: `.env.example`
- Modify: `/root/openclaw/CLAUDE.md` (Recent Changes)
- Modify: `/root/openclaw/ARCHITECTURE.md` (new section)
- Create: `/root/.claude/projects/-root/memory/project_sp2_universe_expansion.md`
- Create: `/root/.claude/projects/-root/memory/feedback_universe_predicate_contract.md`
- Modify: `/root/.claude/projects/-root/memory/MEMORY.md`

- [ ] **Step 1: .env.example additions**

```bash
# SP-2 Phase A
OPENCLAW_UNIVERSE_RESOLVER=1
OPENCLAW_UNIVERSE_RECS=0
UNIVERSE_RESOLVER_MIN_LIVE_TICKERS=200
OPENCLAW_BACKFILL_5Y_CHECKPOINT_DIR=/root/openclaw/data/.checkpoints/backfill_5y
```

- [ ] **Step 2: CLAUDE.md Recent Changes entry** (top of section)

```markdown
- **2026-XX-XX: SP-2 Phase A — Universe-slice machinery shipped** (branch `feat/sp2-phase-a-universe-machinery`). Predicate-per-strategy universe model: each strategy declares `universe_filter(meta, as_of) -> bool` (or inherits the default `sp500`). New `UniverseResolver` service + `ticker_metadata_snapshots` table + 12 candidate predicates. Backtest engines (`unified`/`quick`/`regime_blended`/`intraday_regime`/`regime_performance_analyzer`) accept resolver for per-bar point-in-time universe. Lint enforces `(meta, as_of)` signature + bans `datetime`/`time`/`os` imports in predicate modules; lifecycle sandbox check rejects clock-dependent predicates at transition. All 102 existing strategies on default predicate post-deploy — byte-identical behavior. `union_universe(today, ['live'])` drives collector / options-archive / news / sentiment data-fetch envelope. Migrations 111-114. Doctor checks: `metadata_snapshot_freshness`, `union_universe_size`. Default env `OPENCLAW_UNIVERSE_RESOLVER=1` (kill switch). Spec: `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md`. Plan: `docs/superpowers/plans/2026-05-22-sp2-phase-a-universe-machinery.md`. Phase B (5y backfill) + Phase C (Mastermind universe-recs) + Phase D (research hooks) follow on separate plans.
```

- [ ] **Step 3: ARCHITECTURE.md new section** "Per-Strategy Universe Resolution"

Document the predicate contract, resolver flow, look-ahead defenses, where the resolver fits in the daily cycle, and the candidate predicate catalogue.

- [ ] **Step 4: Memory files**

```markdown
<!-- /root/.claude/projects/-root/memory/project_sp2_universe_expansion.md -->
---
name: project-sp2-universe-expansion
description: SP-2 universe-expansion: predicate-per-strategy slicing, point-in-time TickerMetadata snapshots, UniverseResolver + union_universe envelope, 4-phase rollout (A: machinery, B: 5y backfill, C: Mastermind universe-recs, D: research hooks). Phase A shipped <DATE>.
metadata:
  type: project
---

Each strategy declares `universe_filter(meta, as_of) -> bool` (or inherits default `sp500`).
UniverseResolver applies the predicate against `ticker_metadata_snapshots` ≤ as_of; backtests pass bar date.
`union_universe(today, ['live'])` drives collector + options-archive + news + sentiment envelope.

Look-ahead defense: lint enforces signature + bans datetime/time/os imports; lifecycle.transition runs sandbox check at predicate change (real vs frozen-clock outputs must match).

Phases B/C/D pending. Phase B = 5y × ~3k backfill stage→validate→promote. Phase C = Mastermind `mode=universe-recs` (Opus 4.7 1M picks from 12 candidate predicates). Phase D = PaperHunter+StrategyCoder emit predicate at strategy creation.

Spec: docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md
Phase A plan: docs/superpowers/plans/2026-05-22-sp2-phase-a-universe-machinery.md
Related: [[feedback-universe-predicate-contract]], [[feedback-lifecycle-silent-strip]]
```

```markdown
<!-- /root/.claude/projects/-root/memory/feedback_universe_predicate_contract.md -->
---
name: feedback-universe-predicate-contract
description: SP-2 universe predicates MUST have signature (meta, as_of) and MUST NOT import datetime/time/os/calendar (directly or transitively). Lint + lifecycle sandbox check enforce.
metadata:
  type: feedback
---

When writing or modifying a `universe_filter(meta, as_of) -> bool` predicate:
- Signature is exactly `(meta, as_of)`. The lint scanner rejects any other arity/names.
- No imports of `datetime`, `time`, `os`, `calendar` in the predicate module. Transitive imports also banned (lint follows first-order callees).
- Read time/date *only* from `as_of`. Reading `datetime.today()` or env vars introduces look-ahead bias.

**Why:** Backtests pass `as_of=bar_date`; predicates that read system clock instead of `as_of` would return today's truth at every historical bar — silent look-ahead bias. Lifecycle sandbox check evaluates the predicate with real clock and `freezegun.freeze_time("2020-01-01")`; outputs must match or the transition is rejected.

**How to apply:** All 12 candidate predicates in `src/strategies/universe_default.py` follow this contract. Use them as templates. When you write a new predicate for a strategy file, paste from there.

Related: [[project-sp2-universe-expansion]]
```

```markdown
<!-- MEMORY.md additions -->
- [SP-2 universe expansion](project_sp2_universe_expansion.md) — Predicate-per-strategy slicing; Phase A shipped; B/C/D follow. union_universe is the data-fetch envelope; sandbox-checked lint defends as_of contract.
- [Universe predicate contract](feedback_universe_predicate_contract.md) — Signature (meta, as_of); ban datetime/time/os; lifecycle sandbox-checks behavior under frozen vs real clock.
```

- [ ] **Step 5: Commit**

```bash
git add .env.example /root/openclaw/CLAUDE.md /root/openclaw/ARCHITECTURE.md \
        /root/.claude/projects/-root/memory/project_sp2_universe_expansion.md \
        /root/.claude/projects/-root/memory/feedback_universe_predicate_contract.md \
        /root/.claude/projects/-root/memory/MEMORY.md
git commit -m "docs(sp2): CLAUDE.md, ARCHITECTURE.md, memory entries for Phase A"
```

---

## Task 19: End-to-end smoke test

**Files:**
- Create: `tests/test_sp2_smoke.py`

- [ ] **Step 1: Write the smoke**

```python
# tests/test_sp2_smoke.py
"""SP-2 Phase A end-to-end smoke.
Run order matters; each step depends on the prior.
"""
import json, os, subprocess
from datetime import date
import pytest

def test_doctor_passes():
    out = subprocess.run(
        ["python3", "-m", "src.maintenance.doctor", "--required-only", "--json"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["overall"] == "pass"

def test_resolver_cli_returns_at_least_floor():
    out = subprocess.check_output(
        ["python3", "-m", "src.strategies.universe_resolver",
         "--as-of", str(date.today()), "--states", "live"],
        text=True, timeout=30,
    )
    data = json.loads(out)
    assert len(data) >= int(os.environ.get("UNIVERSE_RESOLVER_MIN_LIVE_TICKERS", "200"))

def test_pipeline_dry_run():
    env = {**os.environ, "PIPELINE_DRY_RUN": "1"}
    out = subprocess.run(
        ["python3", "-m", "execution.pipeline_orchestrator"],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert out.returncode == 0, f"{out.stderr[-2000:]}"
    # Check that the cycle used union_universe (look for log line)
    assert "union_universe" in out.stdout or "union_universe" in out.stderr

def test_system_checks_pass():
    for tag in ("pipeline", "broker", "regime", "strategies"):
        out = subprocess.run(
            ["python3", "-m", "system_checks", "--tag", tag, "--json"],
            capture_output=True, text=True, timeout=120,
        )
        assert out.returncode in (0, 1), out.stderr   # 1 = warn, 2 = fail
        payload = json.loads(out.stdout)
        for check in payload.get("checks", []):
            assert check["status"] != "fail", f"{tag}: {check['name']} failed: {check.get('msg')}"
```

- [ ] **Step 2: Run smoke**

```bash
pytest tests/test_sp2_smoke.py -v
```

Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sp2_smoke.py
git commit -m "test(sp2): Phase A end-to-end smoke"
```

---

## Task 20: PR + 7-day soak

- [ ] **Step 1: Run full test suite locally**

```bash
cd /root/openclaw
python3 -m pytest tests/ -x --tb=short 2>&1 | tail -50
node test/graph-smoke.js
node test/paperhunter-smoke.js
python3 -m system_checks
```

Expected: all green.

- [ ] **Step 2: Push branch + open PR**

```bash
git push -u origin feat/sp2-phase-a-universe-machinery
gh pr create --title "SP-2 Phase A: universe-slice machinery" --body "$(cat <<'EOF'
## Summary
- Predicate-per-strategy universe model. Default predicate `sp500` preserves byte-identical behavior across 102 strategies.
- `UniverseResolver` + `ticker_metadata_snapshots` (migration 111) point-in-time source.
- `union_universe(today, ['live'])` envelope drives collector, options-archive, news, sentiment.
- Backtest engines accept resolver for per-bar `as_of` universe.
- Lint + lifecycle sandbox enforce predicate contract against look-ahead.
- Migrations 111-114; doctor + system_checks updated; dashboards expose universe-slice + inflation.
- Phases B/C/D follow on separate plans.

Spec: `docs/superpowers/specs/2026-05-22-sp2-universe-expansion-design.md`
Plan: `docs/superpowers/plans/2026-05-22-sp2-phase-a-universe-machinery.md`

## Test plan
- [ ] `pytest tests/` green
- [ ] `pytest tests/test_sp2_smoke.py` green
- [ ] `node test/graph-smoke.js` green
- [ ] PIPELINE_DRY_RUN dry cycle logs `union_universe`
- [ ] Doctor exit 0 with new checks
- [ ] Operator dashboard tile renders
- [ ] 7-day production soak with default predicates → strategy signal counts within ±10% of pre-deploy 7-day mean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Operator approval gate** — operator reviews + merges. After merge, deploy to VPS:

```bash
ssh vps
cd /root/openclaw
git checkout main && git pull
for m in 111 112 113 114; do
  psql "$DATABASE_URL" -f "src/database/migrations/${m}_*.sql"
done
systemctl daemon-reload
systemctl restart johnbot.service fundjohn-dashboard.service
python3 -m src.maintenance.doctor --required-only
```

Expected: doctor exit 0.

- [ ] **Step 4: 7-day soak monitoring**

Daily checks (via doctor + system_checks cron):
- `union_universe` size: 500 ± 10
- `universe_resolution_audit.resolver_ms`: ≤ 50 ms
- No `#data-alerts` re: resolver
- Strategy signal counts within ±10% of pre-deploy 7-day mean
- doctor exit 0 every cycle

If any criterion fails 2 days in a row → revert via Level 1 (`OPENCLAW_UNIVERSE_RESOLVER=0`).

- [ ] **Step 5: After soak — promote to "stable", begin Phase B planning**

Write `docs/superpowers/plans/<DATE>-sp2-phase-b-backfill.md` using the same skill.

---

## Out of Scope for Phase A

- The 5y × ~3k backfill (Phase B — separate plan)
- Mastermind `mode=universe-recs` (Phase C — separate plan)
- PaperHunter/StrategyCoder predicate emission (Phase D — separate plan)
- Per-strategy explicit predicate authoring (Phase C deliverable)
- Crypto / commodities / asset-class extensions (SP-3)
- WebSocket streaming for expanded universe (SP-5)

---

## Spec coverage cross-check

| Spec §  | Topic | Task(s) |
|---|---|---|
| 2.1 | Predicate signature + TickerMetadata | 4, 5 |
| 2.2 | Look-ahead defense (lint + sandbox) | 6, 10 |
| 2.3 | UniverseResolver | 7, 8, 13.1 |
| 2.4 | union_universe envelope into consumers | 8, 13, 14 |
| 2.5 | Backtest engine `as_of` integration | 12 |
| 3.1 | New files | 1, 4-13.1 |
| 3.2 | Modified files | 9, 12, 13, 14, 15, 16 |
| 3.3 | .env changes | 18 |
| 3.4 | Migrations 111-114 | 2, 3 |
| 3.5 | Memory + docs | 18 |
| 4.1 | Daily live cycle | 11.2, 13, 14 |
| 4.2 | Backfill (Phase B) | OUT OF SCOPE — separate plan |
| 4.3 | Re-eval (Phase C) | OUT OF SCOPE — separate plan |
| 4.4 | Research hooks (Phase D) | OUT OF SCOPE — separate plan |
| 6.1 | Failure-mode matrix | 15 (doctor), 13 (collector fallback) |
| 6.2 | Rollback ladder | 18 (env kill switch) |
| 7.1 | Unit tests | each task |
| 7.2 | Integration smoke | 19 |
| 7.3 | Pre-deploy soak | 20 |
