# SP-7 Phase C — Live Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-strategy universes live in the trading engine (C1), re-point the collector's daily fetch envelope to the resolver union (C2), and wire every universe consumer to its correct envelope (C3) — every gate default-OFF, shadow-parity before any flip.

**Architecture:** In-cycle per-strategy resolution with mirror-clamp semantics (predicates scope clampable equities only; non-equity passes through), made affordable by a perf fix (hoisted `CoverageIndex` + memoized metadata adapter: 67 resolves share ONE parquet read + ONE DB query). One union price panel, sliced per strategy. Shadow parity rows (migration 133) gate the flip; the A4 clamp is DELETED post-flip.

**Tech Stack:** Python 3 (psycopg2, pandas, pyarrow), Node.js (ioredis, pg), Postgres, Redis, pytest + standalone `node test/*.js` smokes.

**Spec:** `docs/superpowers/specs/2026-06-07-sp7-phase-c-live-wiring-design.md` (operator-approved). Parent: `2026-06-04-sp7-universe-expansion-design.md` §5.

---

## Execution conventions (read first)

- **Worktree:** create via superpowers:using-git-worktrees — branch `feat/sp7-phase-c-live-wiring` off `feat/sp6-phase-a-eod-open-execution` HEAD (`0b0f0be` or later).
- **⚠️ Live-checkout hazard:** the production checkout has UNCOMMITTED changes in `src/pipeline/run_sentiment_step.py` (a `_append_parquet` datetime-fix shadow, plus modified `manifest.json`/`strategy_signatures.json` — live-critical, owned by other workstreams). NEVER touch the live checkout's working tree; build only in the worktree. Task 11's edit to the same file may conflict at merge time — both changes are in disjoint regions (imports+`_append_parquet` vs `main()` universe block); resolve by keeping both.
- **Box:** 2-core / 8 GB / no swap. Run pytest per-file (monolithic runs OOM). Sequential subagents only. NO heavy sweeps 01:00–13:00 UTC Mon–Fri (the Phase B ladder owns that window).
- **Imports:** strategy-layer modules use `from src.strategies.x import ...` (matches `_db_adapters.py`); engine-internal imports use `from execution.x import ...` (matches `engine.py:1533`).
- **Tests env:** `pytest.ini` sets `pythonpath = src`; `conftest.py` auto-restores `os.environ` per test — use `monkeypatch.setenv` freely.
- **Commit per task.** Trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Hoist `CoverageIndex` into an importable module

**Files:**
- Create: `src/strategies/coverage_index.py`
- Modify: `scripts/build_tier_membership.py` (delete local class, import instead)
- Test: `tests/test_coverage_index.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coverage_index.py
"""SP-7 Phase C Task 1 — importable CoverageIndex (hoisted from build_tier_membership)."""
from datetime import date

import pandas as pd
import pytest


def _prices_df():
    # AAPL: 70 bars Jan-Mar 2026 (passes 60-floor by March); NEWT: 5 bars (fails)
    rows = []
    for i in range(70):
        rows.append({"ticker": "AAPL", "date": (pd.Timestamp("2026-01-02") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")})
    for i in range(5):
        rows.append({"ticker": "NEWT", "date": (pd.Timestamp("2026-03-02") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")})
    return pd.DataFrame(rows)


def test_has_floor_basic():
    from src.strategies.coverage_index import CoverageIndex
    idx = CoverageIndex(_prices_df(), min_bars=60)
    assert idx.has_floor("AAPL", date(2026, 3, 31)) is True
    assert idx.has_floor("NEWT", date(2026, 3, 31)) is False      # only 5 bars
    assert idx.has_floor("MISSING", date(2026, 3, 31)) is False   # absent symbol
    assert idx.has_floor("AAPL", date(2025, 12, 31)) is False     # before any bars


def test_min_bars_constant_matches_parquet_coverage():
    from src.strategies.coverage_index import MIN_BARS
    assert MIN_BARS == 60  # mirrors ParquetCoverage default (_db_adapters.py)


def test_equivalence_with_parquet_coverage(tmp_path, monkeypatch):
    """At month-end as_of (the live case: as_of=today, no future bars), the
    month-granular CoverageIndex equals the day-granular ParquetCoverage."""
    import src.pipeline.quarantine_filter as qf
    monkeypatch.setattr(qf, "filter_quarantined", lambda df, t: df)
    df = _prices_df()
    pq_path = tmp_path / "prices.parquet"
    df.to_parquet(pq_path, index=False)

    from src.strategies._db_adapters import ParquetCoverage
    from src.strategies.coverage_index import CoverageIndex
    legacy = ParquetCoverage(prices_path=str(pq_path), min_bars=60)
    fast = CoverageIndex.from_parquet(path=str(pq_path), min_bars=60)
    as_of = date(2026, 3, 31)  # >= max bar date → no within-month peek possible
    for sym in ("AAPL", "NEWT", "MISSING"):
        assert fast.has_floor(sym, as_of) == legacy.has_floor(sym, as_of), sym


def test_build_tier_membership_imports_hoisted_class():
    """build_tier_membership must consume the hoisted module (no local copy)."""
    from pathlib import Path
    src = Path("scripts/build_tier_membership.py").read_text()
    assert "from src.strategies.coverage_index import" in src
    assert "class CoverageIndex" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_coverage_index.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.strategies.coverage_index'`

- [ ] **Step 3: Create `src/strategies/coverage_index.py`** (class body VERBATIM from `scripts/build_tier_membership.py:41-70` — do not "improve" it; equivalence with Phase B's precompute is the point)

```python
"""SP-7 Phase C — importable (ticker × month) cumulative-bar coverage index.

Hoisted verbatim from scripts/build_tier_membership.py (Phase B) so the LIVE
resolve path shares ONE parquet read per process instead of ParquetCoverage's
full re-read per month-miss (src/strategies/_db_adapters.py:58).

Month-granularity note: counts are cumulative through the END of each month.
For live resolution (as_of = today) this equals day-granular counting — no
bars exist beyond today. For historical mid-month as_of it would count bars
from later in that month: do NOT use for PIT backtests (PrecomputedResolver
owns that path).
"""
from __future__ import annotations

from datetime import date

MIN_BARS = 60  # mirrors ParquetCoverage min_bars (src/strategies/_db_adapters.py:45)


class CoverageIndex:
    """(ticker × month) cumulative bar counts from ONE parquet read."""

    def __init__(self, prices_df, min_bars: int = MIN_BARS):
        df = prices_df.copy()
        df['month'] = df['date'].astype(str).str[:7]
        counts = (df.groupby(['ticker', 'month']).size()
                    .unstack(fill_value=0).sort_index(axis=1)
                    .cumsum(axis=1))
        self._counts = counts
        self._min = min_bars

    @classmethod
    def from_parquet(cls, path='data/master/prices.parquet', min_bars=MIN_BARS):
        import pandas as pd
        df = pd.read_parquet(path, columns=['ticker', 'date'])
        from src.pipeline.quarantine_filter import filter_quarantined
        df = filter_quarantined(df, 'prices.parquet')
        return cls(df, min_bars)

    def has_floor(self, symbol: str, as_of: date) -> bool:
        m = as_of.isoformat()[:7]
        if symbol not in self._counts.index:
            return False
        row = self._counts.loc[symbol]
        cols = [c for c in row.index if c <= m]
        if not cols:
            return False
        return int(row[cols[-1]]) >= self._min
```

- [ ] **Step 4: Re-point `scripts/build_tier_membership.py`** — delete its local `MIN_BARS` constant (line 28) and the whole local `class CoverageIndex` (lines 41–70); add at the top imports:

```python
from src.strategies.coverage_index import CoverageIndex, MIN_BARS
```

(`cov = CoverageIndex.from_parquet()` at line ~102 keeps working unchanged.)

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_coverage_index.py -v`
Expected: 4 PASS
Run: `python3 -m pytest tests/test_sp7_phase_b*.py tests/test_build_tier_membership*.py -v 2>/dev/null || true` (whatever Phase B membership tests exist — `ls tests/ | grep -i 'tier\|membership'` first)
Expected: PASS (no regression in the Phase B precompute path)

- [ ] **Step 6: Commit**

```bash
git add src/strategies/coverage_index.py scripts/build_tier_membership.py tests/test_coverage_index.py
git commit -m "feat(sp7-phase-c): hoist CoverageIndex to importable module (C1 perf prerequisite)"
```

---

### Task 2: Memoized + shared-connection `PostgresMetadataDB`

The live resolve calls `fetch_metadata_as_of(as_of)` once PER STRATEGY (67× per cycle), each opening a fresh psycopg2 conn (`_db_adapters.py:15`). Memoize by `as_of` (snapshots are append-only daily — a cycle-lifetime memo is correct) and accept an optional injected connection.

**Files:**
- Modify: `src/strategies/_db_adapters.py:10-41`
- Test: `tests/test_db_adapters_memo.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_adapters_memo.py
"""SP-7 Phase C Task 2 — PostgresMetadataDB memoization + injected conn."""
from datetime import date
from unittest import mock


class _FakeCursor:
    description = [type("D", (), {"name": n})() for n in (
        "snapshot_date", "symbol", "asset_class", "exchange", "status", "tradable",
        "shortable", "fractionable", "easy_to_borrow", "market_cap", "adv_usd_20d",
        "sector", "industry", "options_eligible", "in_sp500", "in_r1000", "in_r3000",
        "listed_date", "delisted_date")]
    def execute(self, sql, params): pass
    def fetchall(self):
        return [(date(2026, 6, 1), "AAPL", "us_equity", "NASDAQ", "active", True,
                 True, True, True, 3.5e12, 1.8e10, "IT", "CE",
                 True, True, True, True, date(1980, 12, 12), None)]
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeConn:
    def cursor(self): return _FakeCursor()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_memo_one_connect_per_as_of(monkeypatch):
    from src.strategies import _db_adapters
    calls = []
    monkeypatch.setattr(_db_adapters.psycopg2, "connect",
                        lambda dsn: calls.append(dsn) or _FakeConn())
    db = _db_adapters.PostgresMetadataDB("postgresql://fake")
    r1 = db.fetch_metadata_as_of(date(2026, 6, 5))
    r2 = db.fetch_metadata_as_of(date(2026, 6, 5))  # memo hit
    assert len(calls) == 1
    assert r1 is r2
    assert r1[0].symbol == "AAPL"
    db.fetch_metadata_as_of(date(2026, 6, 6))       # different as_of → new query
    assert len(calls) == 2


def test_injected_conn_skips_connect(monkeypatch):
    from src.strategies import _db_adapters
    monkeypatch.setattr(_db_adapters.psycopg2, "connect",
                        lambda dsn: (_ for _ in ()).throw(AssertionError("must not connect")))
    db = _db_adapters.PostgresMetadataDB("postgresql://fake", conn=_FakeConn())
    rows = db.fetch_metadata_as_of(date(2026, 6, 5))
    assert rows[0].symbol == "AAPL"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_adapters_memo.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'conn'`

- [ ] **Step 3: Implement** — replace `PostgresMetadataDB.__init__` and split `fetch_metadata_as_of` (the SQL + row-building body moves VERBATIM into `_fetch`):

```python
class PostgresMetadataDB:
    def __init__(self, dsn, conn=None):
        self._dsn = dsn
        self._conn = conn      # optional long-lived connection (SP-7 C1: one per cycle)
        # {as_of: rows} — ticker_metadata_snapshots is append-only daily, so a
        # process-lifetime memo per as_of is correct and collapses the live
        # resolver's 67 identical queries per cycle into one.
        self._memo: dict = {}

    def fetch_metadata_as_of(self, as_of):
        if as_of in self._memo:
            return self._memo[as_of]
        if self._conn is not None:
            rows = self._fetch(self._conn, as_of)
        else:
            with psycopg2.connect(self._dsn) as c:
                rows = self._fetch(c, as_of)
        self._memo[as_of] = rows
        return rows

    def _fetch(self, c, as_of):
        with c.cursor() as cur:
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
                row.symbol = d["symbol"]
                row.snapshot_date = d.pop("snapshot_date")
                # market_cap and adv_usd_20d come back as Decimal from psycopg2; cast to float
                if d["market_cap"] is not None:
                    d["market_cap"] = float(d["market_cap"])
                if d["adv_usd_20d"] is not None:
                    d["adv_usd_20d"] = float(d["adv_usd_20d"])
                row.metadata = TickerMetadata(**d)
                rows.append(row)
            return rows
```

- [ ] **Step 4: Run tests (new + existing adapter/resolver suites)**

Run: `python3 -m pytest tests/test_db_adapters_memo.py tests/test_universe_resolver.py tests/test_quarantine_filter_integration.py -v`
Expected: ALL PASS (existing callers construct `PostgresMetadataDB(dsn)` — default behavior unchanged)

- [ ] **Step 5: Commit**

```bash
git add src/strategies/_db_adapters.py tests/test_db_adapters_memo.py
git commit -m "perf(sp7-phase-c): memoize metadata fetch by as_of + optional injected conn (67 queries/cycle → 1)"
```

---

### Task 3: Fast adapters in the resolver CLI + production factory + perf smoke

`CoverageIndex.has_floor(symbol, as_of)` duck-types `_CoverageProtocol` — pass it AS the coverage adapter. Swap in the CLI `__main__` (used by doctor:1149, system_checks `universe_resolution`, and collector's `readUnionUniverseFromRedis` subprocess) and in `universe_threshold_proposals._resolver()`. Do NOT touch `universe_grid_cli.py` (backtest PIT path keeps day-granular `ParquetCoverage`).

**Files:**
- Modify: `src/strategies/universe_resolver.py:115-137` (CLI block)
- Modify: `src/execution/universe_threshold_proposals.py:35-45` (`_resolver` factory)
- Test: `tests/test_resolver_perf_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolver_perf_smoke.py
"""SP-7 Phase C Task 3 — live-path resolver uses CoverageIndex; perf smoke."""
import os
import time
from datetime import date
from pathlib import Path

import pytest


def test_cli_block_uses_coverage_index():
    src = Path("src/strategies/universe_resolver.py").read_text()
    main_block = src[src.index('if __name__ == "__main__"'):]
    assert "CoverageIndex.from_parquet" in main_block
    assert "ParquetCoverage()" not in main_block


def test_threshold_proposals_factory_uses_coverage_index():
    src = Path("src/execution/universe_threshold_proposals.py").read_text()
    assert "CoverageIndex.from_parquet" in src


def test_grid_cli_untouched_keeps_parquet_coverage():
    """Backtest PIT path must NOT adopt the month-granular index."""
    src = Path("src/backtest/universe_grid_cli.py").read_text()
    assert "CoverageIndex" not in src


@pytest.mark.integration
def test_union_resolve_under_10s_warm():
    """Spec §3.2 perf acceptance: warm 67-strategy union <10s on the loaded box."""
    if not os.environ.get("POSTGRES_URI"):
        pytest.skip("POSTGRES_URI not set")
    if not Path("/root/openclaw/data/master/prices.parquet").exists():
        pytest.skip("master parquet absent")
    from src.execution.live_universe import build_resolver  # Task 5 module
    resolver = build_resolver()
    resolver.union_universe(date.today())          # cold: builds index + memo
    t0 = time.monotonic()
    out = resolver.union_universe(date.today())    # warm
    assert time.monotonic() - t0 < 10.0
    assert len(out) >= 200
```

- [ ] **Step 2: Run to verify the 3 unit tests fail**

Run: `python3 -m pytest tests/test_resolver_perf_smoke.py -v -m "not integration"`
Expected: `test_cli_block_uses_coverage_index` and `test_threshold_proposals_factory_uses_coverage_index` FAIL; `test_grid_cli_untouched...` PASSES (pinning current state).

- [ ] **Step 3: Edit the CLI block** in `src/strategies/universe_resolver.py` — two lines change:

```python
    from src.strategies._db_adapters import PostgresMetadataDB
    from src.strategies.coverage_index import CoverageIndex
    ...
    db = PostgresMetadataDB(os.environ["POSTGRES_URI"])
    cov = CoverageIndex.from_parquet("/root/openclaw/data/master/prices.parquet")
```

(absolute path — the CLI is shelled from varying cwds; mirrors `ParquetCoverage`'s absolute default.)

- [ ] **Step 4: Edit `universe_threshold_proposals._resolver()`** identically:

```python
def _resolver():
    from src.strategies._db_adapters import PostgresMetadataDB
    from src.strategies.coverage_index import CoverageIndex
    from src.strategies.universe_resolver import UniverseResolver

    def manifest_loader():
        return json.loads(
            (ROOT / 'src' / 'strategies' / 'manifest.json').read_text())

    return UniverseResolver(
        db=PostgresMetadataDB(os.environ['POSTGRES_URI']),
        coverage=CoverageIndex.from_parquet('/root/openclaw/data/master/prices.parquet'),
        manifest_loader=manifest_loader)
```

- [ ] **Step 5: Run unit tests; defer the integration test**

Run: `python3 -m pytest tests/test_resolver_perf_smoke.py -v -m "not integration"`
Expected: 3 PASS. (`test_union_resolve_under_10s_warm` runs in Task 5 Step 6, after `build_resolver` exists — and NOT inside 01:00–13:00 UTC.)
Run: `python3 -m pytest tests/test_resolver_cli.py tests/test_universe_resolver.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/strategies/universe_resolver.py src/execution/universe_threshold_proposals.py tests/test_resolver_perf_smoke.py
git commit -m "perf(sp7-phase-c): live resolver paths use CoverageIndex (one parquet read/process); grid CLI untouched"
```

---

### Task 4: Migration 133 — `universe_shadow_parity`

**Files:**
- Create: `src/database/migrations/133_universe_shadow_parity.sql`

- [ ] **Step 1: Write the migration** (pattern: mig 132; idempotent `IF NOT EXISTS` — the runner re-applies every file)

```sql
-- 133: universe_shadow_parity (SP-7 Phase C C1 shadow, 2026-06-07)
--
-- One row per (run_date, strategy_id): the resolver-built per-strategy
-- universe diffed against the actual clamped universe the engine used.
-- Parity criterion (spec §3.5): zero diff for all is_adopted=FALSE rows on
-- ≥3 consecutive trading days. resolve_error non-NULL = the builder
-- failed-open for that strategy (counts as a parity break — code, not data).

CREATE TABLE IF NOT EXISTS universe_shadow_parity (
    id              BIGSERIAL    PRIMARY KEY,
    run_date        DATE         NOT NULL,
    strategy_id     TEXT         NOT NULL,
    predicate       TEXT         NOT NULL,             -- e.g. 'sp500', 'tier_r1000'
    n_resolved      INT          NOT NULL,
    n_actual        INT          NOT NULL,
    added_tickers   JSONB        NOT NULL DEFAULT '[]',  -- resolved − actual
    removed_tickers JSONB        NOT NULL DEFAULT '[]',  -- actual − resolved
    is_adopted      BOOLEAN      NOT NULL DEFAULT FALSE,
    resolve_error   TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, strategy_id)
);

CREATE INDEX IF NOT EXISTS idx_usp_run_date
    ON universe_shadow_parity (run_date DESC);
```

- [ ] **Step 2: Apply + verify** (the `migrate()` runner has a known re-run wart — verify the table landed)

```bash
cd /root/openclaw && npm run db:migrate
python3 - <<'EOF'
import os, psycopg2
from dotenv import load_dotenv; load_dotenv('.env')
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("SELECT to_regclass('universe_shadow_parity')")
    assert cur.fetchone()[0] == 'universe_shadow_parity', 'migration 133 did not land'
    print('universe_shadow_parity OK')
EOF
```

Expected: `universe_shadow_parity OK`

- [ ] **Step 3: Commit**

```bash
git add src/database/migrations/133_universe_shadow_parity.sql
git commit -m "feat(sp7-phase-c): migration 133 universe_shadow_parity (C1 shadow table)"
```

---

### Task 5: `src/execution/live_universe.py` — mirror-clamp builder + shadow writer + production resolver factory

The heart of C1. Classification helpers are LIFTED from `universe_clamp.py` (not imported — the clamp gets deleted at the end of C1; no dangling import allowed).

**Files:**
- Create: `src/execution/live_universe.py`
- Test: `tests/execution/test_live_universe.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/execution/test_live_universe.py
"""SP-7 Phase C Task 5 — mirror-clamp per-strategy universes (spec §3.3)."""
import json
from datetime import date

import pytest

AS_OF = date(2026, 6, 8)
FALLBACK = ["AAPL", "BRK-B", "RDDT", "SPY", "BTC-USD", "GLD"]

# metadata: dot-form symbols (Alpaca); AAPL+BRK.B in sp500, RDDT not; SPY is
# us_equity in Alpaca metadata but category 'etf' in universe_config.
META = {"AAPL": ("us_equity", True), "BRK.B": ("us_equity", True),
        "RDDT": ("us_equity", False), "SPY": ("us_equity", False)}
CATS = {"AAPL": "equity", "BRK-B": "equity", "RDDT": "equity",
        "SPY": "etf", "GLD": "etf"}


class FakeResolver:
    def __init__(self, per_strategy):
        self.per_strategy = per_strategy   # {sid: list[dot-form syms] | Exception}
    def resolve(self, sid, as_of):
        v = self.per_strategy[sid]
        if isinstance(v, Exception):
            raise v
        return v


@pytest.fixture
def refs(tmp_path, monkeypatch):
    # NOTE: universe_filter_ref is nested under metadata — the REAL manifest
    # shape (adoption writer + _load_predicate both use metadata.universe_filter_ref)
    manifest = {"strategies": {
        "S_default": {"state": "live"},                                   # ref absent → sp500
        "S_adopted": {"state": "live", "metadata":
                      {"universe_filter_ref": "src.strategies.universe_default:tier_r1000"}},
        "S_broken":  {"state": "live"},
    }}
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest))
    import src.execution.live_universe as lu
    monkeypatch.setattr(lu, "MANIFEST_PATH", str(p))
    return lu


def test_unadopted_equals_clamp_output(refs):
    """THE load-bearing parity test: a default-predicate strategy reproduces
    clamp_universe() exactly (mirror-clamp semantics, decision D3)."""
    from src.execution.universe_clamp import clamp_universe
    import os
    os.environ["OPENCLAW_ENGINE_UNIVERSE_CLAMP"] = "sp500"
    clamp_out = clamp_universe(list(FALLBACK), lambda: META, lambda: CATS)

    resolver = FakeResolver({"S_default": ["AAPL", "BRK.B"]})  # sp500∩floor, dot-form
    out = refs.build_strategy_universes(
        ["S_default"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert set(out["S_default"]["universe"]) == set(clamp_out)
    assert out["S_default"]["predicate"] == "sp500"
    assert out["S_default"]["adopted"] is False
    assert out["S_default"]["error"] is None


def test_adopted_widens_equities_only(refs):
    resolver = FakeResolver({"S_adopted": ["AAPL", "BRK.B", "RDDT"]})  # r1000 adds RDDT
    out = refs.build_strategy_universes(
        ["S_adopted"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    u = set(out["S_adopted"]["universe"])
    assert "RDDT" in u                          # adoption took effect
    assert {"SPY", "BTC-USD", "GLD"} <= u       # passthrough intact
    assert out["S_adopted"]["adopted"] is True
    assert out["S_adopted"]["predicate"] == "tier_r1000"


def test_nonequity_passthrough_survives_any_predicate(refs):
    resolver = FakeResolver({"S_default": []})  # predicate matches NOTHING
    out = refs.build_strategy_universes(
        ["S_default"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    u = set(out["S_default"]["universe"])
    assert {"SPY", "BTC-USD", "GLD"} <= u       # crypto/ETF never clamped out
    assert "AAPL" not in u                      # equities follow the predicate


def test_dash_dot_bridge(refs):
    """Resolver emits BRK.B (metadata form); fallback holds BRK-B (parquet form)."""
    resolver = FakeResolver({"S_default": ["BRK.B"]})
    out = refs.build_strategy_universes(
        ["S_default"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert "BRK-B" in out["S_default"]["universe"]


def test_fail_open_keeps_fallback_and_records_error(refs):
    resolver = FakeResolver({"S_broken": RuntimeError("db down")})
    out = refs.build_strategy_universes(
        ["S_broken"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert out["S_broken"]["universe"] == list(FALLBACK)   # never empty a live universe
    assert "db down" in out["S_broken"]["error"]


def test_universe_always_subset_of_fallback(refs):
    """Resolved names without price data (pre-C2 adopted tiers) are excluded —
    per-strategy universe ⊆ fallback so load_prices always has the columns."""
    resolver = FakeResolver({"S_adopted": ["AAPL", "NODATA1", "NODATA2"]})
    out = refs.build_strategy_universes(
        ["S_adopted"], AS_OF, list(FALLBACK), resolver=resolver,
        meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert set(out["S_adopted"]["universe"]) <= set(FALLBACK)
```

- [ ] **Step 2: Run to verify failure**

Run: `mkdir -p tests/execution && python3 -m pytest tests/execution/test_live_universe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.execution.live_universe'`
(If `tests/execution/` lacks an `__init__.py` and collection errors, check how `tests/execution/test_universe_clamp.py` is collected — it exists already, so the directory works.)

- [ ] **Step 3: Create `src/execution/live_universe.py`**

```python
"""SP-7 Phase C — per-strategy LIVE universes (C1) + shadow-parity writer.

Mirror-clamp semantics (operator decision D3, spec §3.3): a strategy's
predicate decides CLAMPABLE EQUITIES only; every non-equity ticker in the
engine's fallback universe (etf / index / crypto / absent-from-metadata)
passes through to every strategy. Un-adopted (sp500-default) strategies
therefore reproduce today's clamped universe BY CONSTRUCTION, and the two
live non-equity strategies keep their tickers under any predicate.

Per-strategy universe is always ⊆ the fallback universe (parquet tickers),
so the price panel always has every column a strategy may reference.

Fail-open per strategy: any resolve error leaves that strategy on the FULL
fallback universe (never empty a live universe) and records the error.

The classification helpers are LIFTED from src/execution/universe_clamp.py
(not imported): the clamp is DELETED at the end of C1 (spec §3.6) and this
module must survive that deletion.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

logger = logging.getLogger("ENGINE")

MANIFEST_PATH = "/root/openclaw/src/strategies/manifest.json"


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
            return {s: (ac, bool(sp)) for s, ac, sp in cur.fetchall()}
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


def _manifest_universe_refs() -> dict[str, str | None]:
    """universe_filter_ref is NESTED under metadata (the adoption writer at
    lifecycle_universe_adoption.py:176 sets strategies[sid].metadata.
    universe_filter_ref, and _load_predicate at universe_resolver.py:38-39
    reads the same path). Reading it top-level would mislabel every adopted
    strategy as un-adopted → permanent parity WARN → flip blocked."""
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    return {sid: rec.get("metadata", {}).get("universe_filter_ref")
            for sid, rec in manifest.get("strategies", {}).items()}


def _predicate_name(ref: str | None) -> str:
    return ref.rsplit(":", 1)[1] if ref else "sp500"


def build_resolver(conn=None):
    """Production resolver on the Phase C fast adapters: memoized metadata
    fetch (one DB query per as_of) + CoverageIndex (one parquet read)."""
    from src.strategies._db_adapters import PostgresMetadataDB
    from src.strategies.coverage_index import CoverageIndex
    from src.strategies.universe_resolver import UniverseResolver

    def manifest_loader():
        with open(MANIFEST_PATH) as f:
            return json.load(f)

    return UniverseResolver(
        db=PostgresMetadataDB(os.environ["POSTGRES_URI"], conn=conn),
        coverage=CoverageIndex.from_parquet(
            "/root/openclaw/data/master/prices.parquet"),
        manifest_loader=manifest_loader,
    )


def build_strategy_universes(strategy_ids, as_of, fallback_universe,
                             resolver=None,
                             meta_fetch=_default_meta_fetch,
                             category_fetch=_default_category_fetch):
    """{strategy_id: {'universe': [...], 'predicate': str, 'adopted': bool,
                      'error': str | None}}"""
    refs = _manifest_universe_refs()
    if resolver is None:
        resolver = build_resolver()
    meta = meta_fetch()
    categories = category_fetch()

    def is_clampable_equity(sym: str) -> bool:
        # Symbol-form bridge (SP-7 §11 / ab4238f): parquet/universe_config use
        # dash form ('BRK-B'); ticker_metadata_snapshots uses Alpaca dot form.
        meta_sym = sym if sym in meta else sym.replace("-", ".")
        in_meta = meta_sym in meta
        category = categories.get(sym, "equity" if in_meta else None)
        return in_meta and meta[meta_sym][0] == "us_equity" and category == "equity"

    clampable = {s for s in fallback_universe if is_clampable_equity(s)}
    passthrough = [s for s in fallback_universe if s not in clampable]

    out = {}
    for sid in strategy_ids:
        if sid not in refs:
            logger.warning("[live-universe] %s missing from manifest — default sp500", sid)
        ref = refs.get(sid)
        pred_name = _predicate_name(ref)
        adopted = pred_name != "sp500"
        try:
            resolved = set(resolver.resolve(sid, as_of))
            kept_equities = [s for s in fallback_universe
                             if s in clampable
                             and (s in resolved or s.replace("-", ".") in resolved)]
            universe = sorted(set(kept_equities) | set(passthrough))
            out[sid] = {"universe": universe, "predicate": pred_name,
                        "adopted": adopted, "error": None}
        except Exception as e:  # noqa: BLE001 — never empty a live universe
            logger.error("[live-universe] %s resolve failed — fail-open to "
                         "shared universe: %s", sid, e)
            out[sid] = {"universe": list(fallback_universe), "predicate": pred_name,
                        "adopted": adopted, "error": str(e)}
    return out


def write_shadow_parity(run_date, strategy_ids, actual_universe,
                        conn=None, resolver=None,
                        meta_fetch=_default_meta_fetch,
                        category_fetch=_default_category_fetch):
    """Diff resolver-built per-strategy universes against the actual clamped
    universe; UPSERT into universe_shadow_parity (migration 133).

    Opens its OWN connection by default — the engine's transaction must not
    be committed mid-flight by a sidecar. Caller wraps in try/except; any
    exception here is non-fatal to the signals step.
    """
    import psycopg2
    as_of = run_date if isinstance(run_date, date) else date.fromisoformat(str(run_date))
    built = build_strategy_universes(strategy_ids, as_of, list(actual_universe),
                                     resolver=resolver, meta_fetch=meta_fetch,
                                     category_fetch=category_fetch)
    actual = set(actual_universe)
    own_conn = conn is None
    if own_conn:
        conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        with conn.cursor() as cur:
            for sid, info in built.items():
                resolved = set(info["universe"])
                added = sorted(resolved - actual)
                removed = sorted(actual - resolved)
                cur.execute("""
                    INSERT INTO universe_shadow_parity
                        (run_date, strategy_id, predicate, n_resolved, n_actual,
                         added_tickers, removed_tickers, is_adopted, resolve_error)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                    ON CONFLICT (run_date, strategy_id) DO UPDATE SET
                        predicate       = EXCLUDED.predicate,
                        n_resolved      = EXCLUDED.n_resolved,
                        n_actual        = EXCLUDED.n_actual,
                        added_tickers   = EXCLUDED.added_tickers,
                        removed_tickers = EXCLUDED.removed_tickers,
                        is_adopted      = EXCLUDED.is_adopted,
                        resolve_error   = EXCLUDED.resolve_error
                """, (as_of, sid, info["predicate"], len(resolved), len(actual),
                      json.dumps(added), json.dumps(removed),
                      info["adopted"], info["error"]))
        conn.commit()
        n_drift = sum(1 for s, i in built.items()
                      if not i["adopted"] and set(i["universe"]) != actual)
        logger.info("[live-universe] shadow parity: %d strategies, %d un-adopted drift",
                    len(built), n_drift)
    finally:
        if own_conn:
            conn.close()
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/execution/test_live_universe.py -v`
Expected: 6 PASS

- [ ] **Step 5: Add the shadow-writer test** (append to the same test file)

```python
class _Cur:
    def __init__(self, log): self.log = log
    def execute(self, sql, params): self.log.append(params)
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self): self.log = []; self.committed = False
    def cursor(self): return _Cur(self.log)
    def commit(self): self.committed = True


def test_shadow_writer_diffs_and_upserts(refs):
    resolver = FakeResolver({"S_default": ["AAPL", "BRK.B"],
                             "S_adopted": ["AAPL", "BRK.B", "RDDT"]})
    conn = _Conn()
    refs.write_shadow_parity(AS_OF, ["S_default", "S_adopted"], list(FALLBACK),
                             conn=conn, resolver=resolver,
                             meta_fetch=lambda: META, category_fetch=lambda: CATS)
    assert conn.committed
    by_sid = {p[1]: p for p in conn.log}
    # S_default mirrors the clamp: only RDDT (non-sp500 equity) removed
    assert json.loads(by_sid["S_default"][6]) == ["RDDT"]      # removed_tickers
    assert json.loads(by_sid["S_default"][5]) == []            # added_tickers
    # S_adopted keeps RDDT → zero diff vs the 6-name fallback
    assert json.loads(by_sid["S_adopted"][6]) == []
    assert by_sid["S_adopted"][7] is True                      # is_adopted
```

Run: `python3 -m pytest tests/execution/test_live_universe.py -v`
Expected: 7 PASS

- [ ] **Step 6: Run the Task 3 perf integration test** (OUTSIDE 01:00–13:00 UTC; needs live DB+parquet)

Run: `python3 -m pytest tests/test_resolver_perf_smoke.py -v -m integration`
Expected: PASS in well under 10s warm. If it FAILs on time, STOP — the perf prerequisite is not met; investigate before any engine wiring.

- [ ] **Step 7: Commit**

```bash
git add src/execution/live_universe.py tests/execution/test_live_universe.py
git commit -m "feat(sp7-phase-c): live_universe — mirror-clamp per-strategy builder + shadow-parity writer"
```

---

### Task 6: Engine gate `OPENCLAW_LIVE_UNIVERSE_RESOLVER` + per-strategy slicing

**Files:**
- Modify: `src/execution/engine.py` (`run_strategies` ~821-871; main() universe block ~1531-1553)
- Test: `tests/test_engine_live_universe_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_engine_live_universe_gate.py
"""SP-7 Phase C Task 6 — gate-OFF byte-identity + gate-ON per-strategy slicing."""
import pandas as pd


def _wide_prices():
    idx = pd.to_datetime(["2026-06-04", "2026-06-05"])
    return pd.DataFrame({"AAPL": [1.0, 2.0], "RDDT": [3.0, 4.0], "SPY": [5.0, 6.0]}, index=idx)


class _CaptureStrategy:
    def __init__(self, sid):
        self.id = sid
        self.calls = []
    def generate_signals(self, prices, regime, universe, aux):
        self.calls.append({"cols": list(prices.columns), "universe": list(universe),
                           "fin_keys": sorted(aux.get("financials", {}).keys())})
        return []


def _aux():
    return {"financials": {"AAPL": {}, "RDDT": {}, "SPY": {}},
            "insider_txns": {"AAPL": [], "RDDT": []},
            "options": {"AAPL": {}},
            "sentiment": {"RDDT": {}},
            "macro": {"vix": pd.Series([15.0])},
            "prices_30m": pd.DataFrame({"ticker": ["AAPL", "RDDT"], "close": [1, 2]})}


def _run(strategy_universes):
    from execution.engine import run_strategies
    strat = _CaptureStrategy("S_x")
    import execution.engine as eng
    # neutralize regime/instrument gates for the unit test
    orig_elig, orig_ic = eng.is_eligible, eng.instrument_class_for
    eng.is_eligible = lambda sid, r: True
    eng.instrument_class_for = lambda sid: "equity"
    try:
        run_strategies([strat], _wide_prices(), {"state": "LOW_VOL"},
                       ["AAPL", "RDDT", "SPY"], _aux(),
                       strategy_universes=strategy_universes)
    finally:
        eng.is_eligible, eng.instrument_class_for = orig_elig, orig_ic
    return strat.calls[0]


def test_gate_off_identical_inputs():
    call = _run(None)
    assert call["cols"] == ["AAPL", "RDDT", "SPY"]
    assert call["universe"] == ["AAPL", "RDDT", "SPY"]
    assert call["fin_keys"] == ["AAPL", "RDDT", "SPY"]


def test_gate_on_slices_prices_universe_and_aux():
    call = _run({"S_x": ["AAPL", "SPY"]})
    assert call["cols"] == ["AAPL", "SPY"]            # RDDT column gone
    assert call["universe"] == ["AAPL", "SPY"]
    assert call["fin_keys"] == ["AAPL", "SPY"]        # ticker-keyed aux sliced


def test_slice_aux_helper():
    from execution.engine import _slice_aux
    out = _slice_aux(_aux(), {"AAPL"})
    assert sorted(out["financials"]) == ["AAPL"]
    assert sorted(out["insider_txns"]) == ["AAPL"]
    assert "vix" in out["macro"]                       # macro passes whole
    assert list(out["prices_30m"]["ticker"]) == ["AAPL"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_engine_live_universe_gate.py -v`
Expected: FAIL — `run_strategies() got an unexpected keyword argument 'strategy_universes'`

- [ ] **Step 3: Add `_slice_aux` to engine.py** (place directly above `run_strategies`)

```python
_TICKER_KEYED_AUX = ('financials', 'insider_txns', 'options', 'sentiment')


def _slice_aux(aux_data: dict, universe_set: set) -> dict:
    """SP-7 C1: per-strategy aux slice. Ticker-keyed dicts are filtered;
    prices_30m (long DataFrame with a ticker column) is row-filtered; macro
    (series-name keyed) passes through whole. Slicing aux alongside the price
    panel makes identical-universe ⇒ identical-signals airtight even for
    strategies that iterate aux keys instead of the universe param."""
    out = dict(aux_data)
    for k in _TICKER_KEYED_AUX:
        v = aux_data.get(k)
        if isinstance(v, dict):
            out[k] = {t: d for t, d in v.items() if t in universe_set}
    p30 = aux_data.get('prices_30m')
    if p30 is not None and hasattr(p30, 'columns') and 'ticker' in p30.columns:
        out['prices_30m'] = p30[p30['ticker'].isin(universe_set)]
    return out
```

- [ ] **Step 4: Thread `strategy_universes` through `run_strategies`** — signature becomes:

```python
def run_strategies(strategies, prices, regime, universe, aux_data,
                   strategy_universes=None) -> dict:
```

and inside the per-strategy loop, replace the single `generate_signals` line:

```python
            # SP-7 Phase C (C1): per-strategy universe slice. None (gate OFF)
            # → byte-identical legacy behavior: shared panel/universe/aux.
            if strategy_universes is not None and strat.id in strategy_universes:
                _su = strategy_universes[strat.id]
                _su_set = set(_su)
                strat_prices = prices[[c for c in prices.columns if c in _su_set]]
                strat_aux = _slice_aux(aux_data, _su_set)
                strat_universe = _su
            else:
                strat_prices, strat_aux, strat_universe = prices, aux_data, universe
            signals = strat.generate_signals(strat_prices, strat_regime, strat_universe, strat_aux)
```

- [ ] **Step 5: Wire the gate in main()** — insert AFTER the clamp call (`universe = clamp_universe(universe)`, ~line 1534) and BEFORE `prices = load_prices(universe)`:

```python
        # SP-7 Phase C (C1): per-strategy universes via UniverseResolver.
        # Gate default-OFF. When ON: the union of per-strategy sets replaces
        # the clamped universe for the ONE panel load (memory invariant), and
        # run_strategies slices prices/aux per strategy. Whole-build failure
        # fails open to the legacy shared universe.
        strategy_universes = None
        if os.environ.get('OPENCLAW_LIVE_UNIVERSE_RESOLVER') == '1':
            try:
                from execution.live_universe import build_strategy_universes
                _built = build_strategy_universes(
                    [s.id for s in strategies], run_date, list(universe))
                strategy_universes = {sid: info['universe']
                                      for sid, info in _built.items()}
                universe = sorted(set().union(
                    *[set(u) for u in strategy_universes.values()]))
                _n_err = sum(1 for i in _built.values() if i['error'])
                logger.info(f"live-universe ON: union {len(universe)} tickers, "
                            f"{len(strategy_universes)} strategies, {_n_err} fail-open")
            except Exception as e:
                logger.error(f"live-universe build failed — fail-open to shared "
                             f"clamped universe: {e}")
                strategy_universes = None
```

and pass it at the call site (~line 1553):

```python
        strategy_results = run_strategies(strategies, prices, regime, universe,
                                          aux_data, strategy_universes=strategy_universes)
```

(`run_date` at engine.py:1479 is the date object from `_parse_run_date()` — it is the resolver `as_of`, matching the decision-day convention.)

Known-latent note (do NOT fix in this task, just preserve): the `last_price` inject at engine.py:1546-1552 calls `prices.groupby('ticker')` on the WIDE pivoted frame (no ticker column) → its except swallows it → the inject is already a silent no-op live. `_slice_aux` neither worsens nor masks this; leave a one-line comment near `_slice_aux` noting it.

- [ ] **Step 6: Run new tests + the engine regression suites**

Run: `python3 -m pytest tests/test_engine_live_universe_gate.py -v`
Expected: 3 PASS
Run (per-file, sequential — OOM discipline): `for f in tests/test_engine_run_stats.py tests/test_engine_regime_gate.py tests/test_engine_sentiment_aux.py tests/test_engine_override.py tests/execution/test_universe_clamp.py; do python3 -m pytest $f -v || break; done`
Expected: ALL PASS (gate-OFF default → byte-identical)

- [ ] **Step 7: Commit**

```bash
git add src/execution/engine.py tests/test_engine_live_universe_gate.py
git commit -m "feat(sp7-phase-c): engine gate OPENCLAW_LIVE_UNIVERSE_RESOLVER — per-strategy panel/aux slicing, one union panel"
```

---

### Task 7: Shadow sidecar in the signals step

**Files:**
- Modify: `src/execution/engine.py` main() (immediately after the Task 6 block)
- Test: `tests/test_engine_shadow_sidecar.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_shadow_sidecar.py
"""SP-7 Phase C Task 7 — shadow parity is a NON-FATAL, zero-delta sidecar."""
from pathlib import Path


def test_shadow_block_present_and_gated():
    src = Path("src/execution/engine.py").read_text()
    assert "OPENCLAW_LIVE_UNIVERSE_SHADOW" in src
    block = src[src.index("OPENCLAW_LIVE_UNIVERSE_SHADOW") - 600:
                src.index("OPENCLAW_LIVE_UNIVERSE_SHADOW") + 1200]
    # shadow only runs while the live gate is OFF (shadow-vs-clamp comparison)
    assert "OPENCLAW_LIVE_UNIVERSE_RESOLVER" in block
    assert "write_shadow_parity" in block
    # non-fatal: wrapped in try/except with a warning, never a raise
    assert "non-fatal" in block


def test_shadow_block_mutates_no_engine_state():
    """Zero-behavior-delta pin (spec §7): the sidecar block reads engine state
    but never assigns to it — shadow ON vs OFF cannot change signals."""
    src = Path("src/execution/engine.py").read_text()
    start = src.index("OPENCLAW_LIVE_UNIVERSE_SHADOW")
    block = src[start:start + 900]
    block = block[:block.index("prices   = load_prices")] if "prices   = load_prices" in block else block
    for lhs in ("universe =", "strategies =", "strategy_universes =", "aux_data ="):
        assert lhs not in block, f"shadow sidecar must not assign engine state: {lhs}"


def test_shadow_failure_does_not_raise(monkeypatch):
    """Simulate the sidecar call pattern: a raising writer must be swallowed."""
    import execution.live_universe as lu

    def boom(*a, **k):
        raise RuntimeError("shadow db down")
    monkeypatch.setattr(lu, "write_shadow_parity", boom)
    # The engine wraps the call; replicate the wrapper contract here:
    try:
        try:
            lu.write_shadow_parity("2026-06-08", [], [])
        except Exception:
            pass  # engine logs a warning and continues
    except Exception:
        raise AssertionError("sidecar exception escaped")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_engine_shadow_sidecar.py -v`
Expected: `test_shadow_block_present_and_gated` FAILS (no shadow block yet)

- [ ] **Step 3: Insert the sidecar** in engine.py main(), AFTER the Task 6 gate block, BEFORE `prices = load_prices(universe)`:

```python
        # SP-7 Phase C shadow parity (spec §3.5): resolver-vs-clamp diff rows,
        # written by a non-fatal sidecar. Only meaningful while the live
        # resolver gate is OFF — once it flips, there is no clamp to diff.
        if (os.environ.get('OPENCLAW_LIVE_UNIVERSE_SHADOW') == '1'
                and os.environ.get('OPENCLAW_LIVE_UNIVERSE_RESOLVER') != '1'):
            try:
                from execution.live_universe import write_shadow_parity
                write_shadow_parity(run_date, [s.id for s in strategies],
                                    list(universe))
            except Exception as e:  # noqa: BLE001 — non-fatal sidecar
                logger.warning(f"universe shadow parity failed (non-fatal): {e}")
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_engine_shadow_sidecar.py tests/test_engine_live_universe_gate.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/engine.py tests/test_engine_shadow_sidecar.py
git commit -m "feat(sp7-phase-c): shadow-parity sidecar in signals step (gate OPENCLAW_LIVE_UNIVERSE_SHADOW, non-fatal)"
```

---

### Task 8: system_check `universe_shadow_parity`

Severity contract: **drift on un-adopted = WARN** (data semantics — operator reads it in the flip runbook; must not redline CI while shadow legitimately surfaces diffs), **resolve_error rows = FAIL** (code malfunction — SHOULD block merge/CI). Flip prereq = the check reports `PASS` (3 days clean); WARN blocks the flip too.

**Files:**
- Create: `src/system_checks/checks/universe_shadow_parity.py`
- Modify: `src/system_checks/checks/__init__.py` (import the new module — mirror how `universe_resolution` is imported)
- Test: `tests/test_universe_shadow_parity_check.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_universe_shadow_parity_check.py
"""SP-7 Phase C Task 8 — shadow-parity system check severity contract."""
import subprocess


def test_check_registered_and_skips_when_gate_off(monkeypatch):
    monkeypatch.delenv("OPENCLAW_LIVE_UNIVERSE_SHADOW", raising=False)
    from src.system_checks.checks.universe_shadow_parity import _universe_shadow_parity
    from src.system_checks.types import Status
    status, detail = _universe_shadow_parity()
    assert status == Status.SKIP
    assert "gate off" in detail


def test_check_appears_in_registry():
    out = subprocess.run(
        ["python3", "-m", "src.system_checks", "--check", "universe_shadow_parity", "--json"],
        capture_output=True, text=True, timeout=60, cwd="/root/openclaw")
    assert "universe_shadow_parity" in out.stdout
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_universe_shadow_parity_check.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Create the check** (contract per `src/system_checks/README.md`; pattern: `checks/universe_resolution.py`)

```python
"""universe_shadow_parity — SP-7 Phase C C1 flip gate (spec §3.5).

PASS  = last ≤3 shadow run-dates have ZERO universe-diff and zero
        resolve_error for every is_adopted=FALSE strategy.
WARN  = un-adopted drift found (data semantics — diagnose via
        added/removed tickers; remedies: widen default predicate, adopt
        the strategy, or fix category metadata). Blocks the C1 flip,
        EXCEPT the documented sub-floor case: removed names that have
        <60 bars in prices.parquet (clamp keeps them, the resolver floor
        excludes them; they can't fill strategy lookbacks so the spec's
        zero-SIGNAL-delta still holds). Classification SQL + decision
        rule: docs/sp7-phase-c-runbook.md §3. Verified-empty 2026-06-07.
FAIL  = resolve_error rows present (the builder failed-open — code bug).
SKIP  = gate off / no DB.

Flip prereq (runbook §5): PASS, or WARN classified all-sub-floor per §3.
"""
import os

from ..registry import check
from ..types import Status


@check(name='universe_shadow_parity', tags=['strategies'], requires=['db'])
def _universe_shadow_parity():
    if os.environ.get('OPENCLAW_LIVE_UNIVERSE_SHADOW') != '1':
        return Status.SKIP, 'OPENCLAW_LIVE_UNIVERSE_SHADOW gate off'
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return Status.SKIP, 'POSTGRES_URI not set'
    import psycopg2
    with psycopg2.connect(uri) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT run_date FROM universe_shadow_parity "
                    "ORDER BY run_date DESC LIMIT 3")
        days = [r[0] for r in cur.fetchall()]
        if not days:
            return Status.WARN, 'no shadow rows yet (first gated cycle pending?)'
        cur.execute("""
            SELECT strategy_id, run_date, resolve_error,
                   jsonb_array_length(added_tickers)
                 + jsonb_array_length(removed_tickers) AS drift
              FROM universe_shadow_parity
             WHERE run_date = ANY(%s) AND is_adopted = FALSE
               AND (resolve_error IS NOT NULL
                    OR jsonb_array_length(added_tickers)
                     + jsonb_array_length(removed_tickers) > 0)
             ORDER BY drift DESC
        """, (days,))
        bad = cur.fetchall()
    errors = [b for b in bad if b[2]]
    if errors:
        worst = '; '.join(f'{b[0]}@{b[1]}' for b in errors[:3])
        return Status.FAIL, f'{len(errors)} resolve_error rows (code bug): {worst}'
    if bad:
        worst = '; '.join(f'{b[0]}@{b[1]} drift={b[3]}' for b in bad[:3])
        return Status.WARN, f'{len(bad)} un-adopted parity breaks in last {len(days)}d: {worst}'
    return Status.PASS, f'{len(days)} day(s) clean for un-adopted strategies'
```

Then add the import to `src/system_checks/checks/__init__.py` exactly where sibling check modules are imported (open the file; mirror the `universe_resolution` import line):

```python
from . import universe_shadow_parity  # noqa: F401
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_universe_shadow_parity_check.py -v`
Expected: 2 PASS
Run: `cd /root/openclaw && python3 -m src.system_checks --check universe_shadow_parity`
Expected: `SKIP — gate off` (gate unset in this shell)

- [ ] **Step 5: Commit**

```bash
git add src/system_checks/checks/universe_shadow_parity.py src/system_checks/checks/__init__.py tests/test_universe_shadow_parity_check.py
git commit -m "feat(sp7-phase-c): universe_shadow_parity system check (WARN=drift blocks flip, FAIL=resolve errors)"
```

---

### Task 9: Resolver `envelope_universe()` + CLI `--envelope`

**Files:**
- Modify: `src/strategies/universe_resolver.py` (`resolve` → `_resolve(apply_floor)`; new `envelope_universe`; CLI flag)
- Test: `tests/test_envelope_universe.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_envelope_universe.py
"""SP-7 Phase C Task 9 — no-floor envelope union (spec §4)."""
from datetime import date

from src.strategies.universe_meta import TickerMetadata
from src.strategies.universe_resolver import UniverseResolver

_TODAY = date(2026, 6, 8)


def _row(sym, in_sp500=True):
    r = type("Row", (), {})()
    r.snapshot_date = date(2026, 6, 1)
    r.symbol = sym
    r.metadata = TickerMetadata(
        symbol=sym, asset_class="us_equity", exchange="NYSE", status="active",
        tradable=True, shortable=True, fractionable=True, easy_to_borrow=True,
        market_cap=1e10, adv_usd_20d=1e8, sector="X", industry="Y",
        options_eligible=True, in_sp500=in_sp500, in_r1000=True, in_r3000=True,
        listed_date=date(2026, 5, 1), delisted_date=None)
    return r


class FakeDB:
    def fetch_metadata_as_of(self, as_of):
        return [_row("AAPL"), _row("NEWT")]   # NEWT: new listing, no coverage floor


class FloorOnlyAAPL:
    def has_floor(self, symbol, as_of):
        return symbol == "AAPL"


def _resolver():
    manifest = {"strategies": {"S1": {"state": "live"}}}  # default sp500 predicate
    return UniverseResolver(db=FakeDB(), coverage=FloorOnlyAAPL(),
                            manifest_loader=lambda: manifest,
                            today_fn=lambda: _TODAY)


def test_union_applies_floor_envelope_does_not():
    r = _resolver()
    assert r.union_universe(_TODAY) == ["AAPL"]              # floored (strategy resolve)
    assert r.envelope_universe(_TODAY) == ["AAPL", "NEWT"]   # no-floor (fetch envelope)


def test_resolve_public_behavior_unchanged():
    r = _resolver()
    assert r.resolve("S1", _TODAY) == ["AAPL"]


def test_cli_has_envelope_flag():
    from pathlib import Path
    src = Path("src/strategies/universe_resolver.py").read_text()
    assert "--envelope" in src
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_envelope_universe.py -v`
Expected: FAIL — `AttributeError: 'UniverseResolver' object has no attribute 'envelope_universe'`

- [ ] **Step 3: Implement** — refactor `resolve` into `_resolve` with `apply_floor` (cache key gains the flag); public `resolve` is a one-line delegate:

```python
    def resolve(self, strategy_id: str, as_of: _date) -> list[str]:
        return self._resolve(strategy_id, as_of, apply_floor=True)

    def _resolve(self, strategy_id: str, as_of: _date, apply_floor: bool = True) -> list[str]:
        if as_of > self._today_fn():
            raise AsOfInFutureError(f"as_of {as_of} > today {self._today_fn()}")
        key = (strategy_id, as_of, apply_floor)
        if key in self._cache:
            return self._cache[key]
        predicate = self._load_predicate(strategy_id)
        rows = self._db.fetch_metadata_as_of(as_of)
        out = []
        for row in rows:
            meta = row.metadata if hasattr(row, "metadata") else TickerMetadata.from_row(row)
            try:
                if predicate(meta, as_of) and (
                        not apply_floor or self._coverage.has_floor(meta.symbol, as_of)):
                    out.append(meta.symbol)
            except Exception:
                # Defensive: a broken predicate skips the ticker; lifecycle
                # sandbox check should have caught this earlier.
                continue
        out.sort()
        self._cache[key] = out
        return out

    def envelope_universe(self, as_of: _date, states: tuple[str, ...] = ("live",)) -> list[str]:
        """SP-7 Phase C C2 fetch envelope: predicate-only union, NO coverage
        floor (spec §4). The floor gates strategy resolve; the fetch envelope
        must include newly adopted tiers so their data accrues — otherwise the
        coverage-floor chicken-and-egg never dies."""
        seen: set[str] = set()
        for sid in self._live_strategy_ids(states):
            seen.update(self._resolve(sid, as_of, apply_floor=False))
        return sorted(seen)
```

CLI block — add the flag and dispatch:

```python
    ap.add_argument("--envelope", action="store_true",
                    help="No-floor fetch envelope (SP-7 C2) instead of the floored union")
    ...
    if args.strategy:
        out = resolver.resolve(args.strategy, as_of=as_of)
    elif args.envelope:
        out = resolver.envelope_universe(as_of=as_of, states=states)
    else:
        out = resolver.union_universe(as_of=as_of, states=states)
```

- [ ] **Step 4: Run new + existing resolver tests**

Run: `python3 -m pytest tests/test_envelope_universe.py tests/test_universe_resolver.py tests/test_resolver_cli.py -v`
Expected: `tests/test_universe_resolver.py` has ONE known breakage — line ~55 asserts the 2-tuple cache key `('S5', date(2026, 6, 1)) in resolver._cache`. Update that assertion to the 3-tuple `('S5', date(2026, 6, 1), True) in resolver._cache` (True = apply_floor default from public `resolve()`), include the test file in this task's commit, and note the key-shape change in the commit message. Everything else PASSES unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/universe_resolver.py tests/test_envelope_universe.py tests/test_universe_resolver.py
git commit -m "feat(sp7-phase-c): envelope_universe (no-floor union) + CLI --envelope (C2); resolver cache key gains apply_floor"
```

---

### Task 10: Collector envelope caller (prices fetch list)

Fixes two latent bugs in the dead `readUnionUniverseFromRedis` while wiring it: (a) `{ EX: 14400 }` is node-redis-v4 syntax — the client is **ioredis** (`src/database/redis.js:3`), which needs positional `'EX', 14400`; (b) its error fallback returned `getUniverse('SP500')` — a silent SHRINK; it now returns `null` and the caller keeps the universe_config list.

**Files:**
- Modify: `src/pipeline/collector.js` (`readUnionUniverseFromRedis` ~146; daily universe block ~1492; EOD block ~1825; exports ~1906)
- Modify: `src/pipeline/store.js` (add `getInactiveTickers`)
- Test: `test/collector-envelope-smoke.js`

- [ ] **Step 1: Write the failing JS smoke** (pattern: `test/collector-eod-freshness-smoke.js` — `Module._load` hijack, `node:assert`, exit 0/1)

```js
// test/collector-envelope-smoke.js
// SP-7 Phase C Task 10 — resolver envelope merge: union + active=false hard
// exclusion + dot→dash bridge + never-shrink fail-open.
// Run: node test/collector-envelope-smoke.js
'use strict';

const assert = require('node:assert');
const path = require('node:path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..');

let execCalls = [];
let execResult = JSON.stringify(['AAPL', 'BRK.B', 'NEWT', 'BADCO']);
let execThrows = false;
const redisStore = {};

const origLoad = Module._load;
Module._load = function (request, parent, ...rest) {
  if (request === 'child_process') {
    return {
      execSync: (cmd, opts) => {
        execCalls.push(cmd);
        if (execThrows) throw new Error('resolver down');
        return execResult;
      },
    };
  }
  if (request.includes('database/redis')) {
    return {
      getClient: () => ({
        get: async (k) => redisStore[k] || null,
        set: async (k, v, exFlag, exSecs) => {
          assert.strictEqual(exFlag, 'EX', 'ioredis positional EX required');
          assert.strictEqual(typeof exSecs, 'number');
          redisStore[k] = v;
        },
      }),
    };
  }
  if (request.includes('database/postgres')) {
    return {
      query: async (text) => {
        if (/active = false/.test(text)) return { rows: [{ ticker: 'BADCO' }] };
        if (/active = true/.test(text)) {
          return { rows: [
            { ticker: 'AAPL', category: 'equity', has_options: true, has_fundamentals: true },
            { ticker: 'SPY', category: 'etf', has_options: true, has_fundamentals: false },
          ] };
        }
        return { rows: [] };
      },
    };
  }
  if (request.includes('data/parquet_store')) {
    return new Proxy({}, { get: () => async () => ({}) });
  }
  if (request.includes('budget/enforcer')) {
    return { checkBudget: async () => ({ mode: 'GREEN' }), enforceBudget: () => ({}) };
  }
  return origLoad.call(this, request, parent, ...rest);
};

const collector = require(path.join(ROOT, 'src/pipeline/collector.js'));

(async () => {
  const base = ['AAPL'];

  // 1. Gate OFF → identity
  delete process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE;
  assert.deepStrictEqual(await collector.applyResolverEnvelope(base, '2026-06-08'), base);
  assert.strictEqual(execCalls.length, 0, 'no resolver call when gate off');

  // 2. Gate ON → union + dot→dash + active=false exclusion
  process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE = '1';
  const merged = await collector.applyResolverEnvelope(base, '2026-06-08');
  assert.ok(merged.includes('AAPL'));
  assert.ok(merged.includes('BRK-B'), 'dot→dash bridge');
  assert.ok(merged.includes('NEWT'), 'envelope name fetched');
  assert.ok(!merged.includes('BADCO'), 'active=false is a hard exclusion');
  assert.ok(merged.length >= base.length, 'never shrink');
  assert.ok(execCalls[0].includes('--envelope'), 'uses the no-floor envelope');

  // 3. Resolver failure → fail-open to config list
  execThrows = true;
  delete redisStore['universe:envelope:2026-06-08:live'];
  assert.deepStrictEqual(await collector.applyResolverEnvelope(base, '2026-06-08'), base);

  console.log('collector-envelope-smoke: ALL PASS');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL:', e.message);
  process.exit(1);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/openclaw && node test/collector-envelope-smoke.js`
Expected: FAIL — `collector.applyResolverEnvelope is not a function`

- [ ] **Step 3: Fix + generalize `readUnionUniverseFromRedis`** (collector.js ~146) — replace the function:

```js
async function readUnionUniverseFromRedis(redis, dateStr, states = 'live', kind = 'union') {
  const key = `universe:${kind}:${dateStr}:${states}`;
  try {
    const cached = await redis.get(key);
    if (cached) return JSON.parse(cached);
  } catch (e) {
    console.warn(`[collector] redis universe read failed: ${e.message}`);
  }
  try {
    const { execSync } = require('child_process');
    const envelopeFlag = kind === 'envelope' ? ' --envelope' : '';
    const out = execSync(
      `python3 -m src.strategies.universe_resolver --as-of ${dateStr} --states ${states} --json${envelopeFlag}`,
      { encoding: 'utf8', timeout: 120000, cwd: '/root/openclaw' },
    );
    const tickers = JSON.parse(out);
    // ioredis takes positional EX args; the old `{ EX: 14400 }` object form is
    // node-redis-v4 syntax that ioredis ignores (latent bug — this function
    // had zero callers until SP-7 Phase C).
    try { await redis.set(key, JSON.stringify(tickers), 'EX', 14400); } catch {}
    return tickers;
  } catch (e) {
    console.warn(`[collector] universe ${kind} resolution failed: ${e.message}`);
    // Caller decides the fallback. NEVER substitute a narrower set here —
    // the previous getUniverse('SP500') fallback was a silent-shrink risk.
    return null;
  }
}
```

- [ ] **Step 4: Add `getInactiveTickers` to store.js** (below `getActiveUniverse`, ~line 100) and export it alongside it:

```js
// SP-7 Phase C: operator hard-exclusion overlay for the resolver envelope
async function getInactiveTickers() {
  const res = await query(
    `SELECT ticker FROM universe_config WHERE active = false`
  ).catch(() => null);
  return (res?.rows || []).map(r => r.ticker);
}
```

- [ ] **Step 5: Add `applyResolverEnvelope` to collector.js** (near `readUnionUniverseFromRedis`) and export both:

```js
// SP-7 Phase C (C2): widen the PRICE fetch list to the resolver's no-floor
// envelope (spec §4). universe_config demotes to operator overlay: its
// active=true equities stay in (union); active=false rows are a HARD
// exclusion applied after the union. Fail-open = keep the config list; the
// fetch envelope must never silently shrink.
async function applyResolverEnvelope(equityTickers, dateStr) {
  if (process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE !== '1') return equityTickers;
  try {
    const { getClient } = require('../database/redis');
    const envelope = await readUnionUniverseFromRedis(getClient(), dateStr, 'live', 'envelope');
    if (!envelope || envelope.length === 0) {
      console.warn('[collector] envelope unavailable — keeping universe_config list');
      return equityTickers;
    }
    // resolver emits metadata dot-form (BRK.B); parquet/universe_config use dash
    const envDash = envelope.map(t => t.replace(/\./g, '-'));
    const inactive = new Set(await store.getInactiveTickers());
    const merged = [...new Set([...equityTickers, ...envDash])]
      .filter(t => !inactive.has(t)).sort();
    if (merged.length < equityTickers.length) {
      console.warn(`[collector] envelope would SHRINK ${equityTickers.length}→${merged.length} — keeping universe_config list`);
      return equityTickers;
    }
    console.log(`[collector] envelope: resolver=${envelope.length} config=${equityTickers.length} excluded=${inactive.size} final=${merged.length}`);
    return merged;
  } catch (e) {
    console.warn(`[collector] envelope merge failed — keeping universe_config list: ${e.message}`);
    return equityTickers;
  }
}
```

- [ ] **Step 6: Wire BOTH cycle sites.** In the daily-cycle universe block (~1492) introduce a price-scoped list — `equityTickers` itself stays config-scoped (news/insider keep it until Task 11 decides their scope):

```js
  const equityTickers     = fullUniverse.filter(u => u.category === 'equity').map(u => u.ticker);
  // SP-7 C2: PRICES fetch on the wide no-floor envelope; news/insider scope
  // decided separately (Task 11 / spec §5 envelope hierarchy).
  const priceEquityTickers = await applyResolverEnvelope(
    equityTickers, new Date().toISOString().slice(0, 10));
```

Then swap the THREE price-path consumers in the daily-cycle function to `priceEquityTickers` (verbatim current code at each site; leave `runNewsCollection(equityTickers)` and `runInsiderTransactions(equityTickers)` untouched):

1. Gap-scan input (~line 1527): `priceTickers: [...equityTickers, ...marketTickers]` → `priceTickers: [...priceEquityTickers, ...marketTickers]`
2. Phase 2a fallback (~line 1591): `const priceEquityNeeded = gaps?.prices.tickers.filter(t => equityTickers.includes(t)) ?? equityTickers;` → filter against AND fall back to `priceEquityTickers` (the `gaps` filter membership check also uses the widened list so envelope names missing coverage aren't dropped):
   `const priceEquityNeeded = gaps?.prices.tickers.filter(t => priceEquityTickers.includes(t)) ?? priceEquityTickers;`
3. EOD freshness verification (~line 1604): `_verifyEquityFreshness(equityTickers, ...)` → `_verifyEquityFreshness(priceEquityTickers, ...)` (its retry `refetchFn` feeds `runHistoricalPrices` — same scope).

In `runEodRefresh` (~1825) apply the same wrap:

```js
  const equityTickers = fullUniverse.filter(u => u.category === 'equity').map(u => u.ticker);
  const priceEquityTickers = await applyResolverEnvelope(
    equityTickers, new Date().toISOString().slice(0, 10));
```

and pass `priceEquityTickers` to `runHistoricalPrices(historyDays, ...)` at ~line 1845, plus `const tickerSet = new Set([...priceEquityTickers, ...marketTickers])` at ~line 1828 (aligns the coverage-diff report).
Finally add to the exports block (~1906, after `readUnionUniverseFromRedis`): `applyResolverEnvelope,`
(The deleted `require('./universe')` inside the old error fallback does NOT orphan `./universe` — another function-scoped require of it exists at ~line 57; no top-level import to clean up.)

- [ ] **Step 7: Run the smoke + the existing collector smokes**

Run: `node test/collector-envelope-smoke.js && node test/collector-eod-freshness-smoke.js && node test/daily-cycle-universe-smoke.js`
Expected: all print ALL PASS (gate off in env → existing smokes byte-identical)

- [ ] **Step 8: Commit**

```bash
git add src/pipeline/collector.js src/pipeline/store.js test/collector-envelope-smoke.js
git commit -m "feat(sp7-phase-c): collector price fetch on no-floor resolver envelope (gate OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE) + fix latent ioredis EX + shrink-fallback bugs"
```

---

### Task 11: Fundamentals/insider scoped to adopted-union

Parent decision 4 / spec §5: expensive per-ticker fetchers track the **floored adopted-union** (what strategies actually use), not the wide envelope. Expansion-only union with the config lists → never-shrink by construction. News stays config-scoped (not in parent decision 4 — note in code comment).

**Files:**
- Modify: `src/pipeline/collector.js` (daily-cycle Phase 4 + Phase 7 scope, ~1641-1667)
- Test: extend `test/collector-envelope-smoke.js`

- [ ] **Step 1: Extend the smoke** (append before the final `console.log`):

```js
  // 4. Adopted-union scope helper (fundamentals/insider — spec §5)
  execThrows = false;
  execCalls = [];
  execResult = JSON.stringify(['AAPL', 'BRK.B', 'ADOPTED1']);
  const scoped = await collector.adoptedUnionScope(['AAPL'], '2026-06-08');
  assert.ok(scoped.includes('ADOPTED1'), 'adopted name in scope');
  assert.ok(scoped.includes('AAPL'), 'config name kept (expansion-only)');
  assert.ok(!scoped.includes('BADCO'), 'active=false excluded');
  assert.ok(!execCalls[0].includes('--envelope'), 'fundamentals use the FLOORED union');

  // 5. Union failure → config scope unchanged
  execThrows = true;
  delete redisStore['universe:union:2026-06-08:live'];
  assert.deepStrictEqual(await collector.adoptedUnionScope(['AAPL'], '2026-06-08'), ['AAPL']);
```

Run: `node test/collector-envelope-smoke.js` — Expected: FAIL (`adoptedUnionScope is not a function`)

- [ ] **Step 2: Add `adoptedUnionScope` to collector.js** (next to `applyResolverEnvelope`; export it):

```js
// SP-7 Phase C (C3 / parent decision 4): expensive per-ticker fetchers
// (fundamentals FMP, insider EDGAR) scope to the FLOORED adopted-union —
// what strategies actually resolve — not the wide fetch envelope.
// Expansion-only: union with the config list, so never-shrink holds.
async function adoptedUnionScope(configTickers, dateStr) {
  if (process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE !== '1') return configTickers;
  try {
    const { getClient } = require('../database/redis');
    const union = await readUnionUniverseFromRedis(getClient(), dateStr, 'live', 'union');
    if (!union || union.length === 0) return configTickers;
    const unionDash = union.map(t => t.replace(/\./g, '-'));
    const inactive = new Set(await store.getInactiveTickers());
    return [...new Set([...configTickers, ...unionDash])]
      .filter(t => !inactive.has(t)).sort();
  } catch (e) {
    console.warn(`[collector] adopted-union scope failed — config scope kept: ${e.message}`);
    return configTickers;
  }
}
```

- [ ] **Step 3: Wire the daily-cycle consumers.** Where Phase 4 computes `fundNeeded` (~1642) and Phase 7 calls `runInsiderTransactions(equityTickers)` (~1666), introduce scoped lists once, above Phase 4:

```js
  // SP-7 C2/C3: fundamentals + insider follow the adopted-union (spec §5
  // envelope hierarchy). News stays config-scoped (not in parent decision 4).
  const fundamentalScope = await adoptedUnionScope(fundamentalTickers, new Date().toISOString().slice(0, 10));
  const insiderScope     = await adoptedUnionScope(equityTickers, new Date().toISOString().slice(0, 10));
```

then `fundNeeded = gaps?.fundamentals.tickers ?? fundamentalScope;` and `runInsiderTransactions(insiderScope);`.

- [ ] **Step 4: Run smokes**

Run: `node test/collector-envelope-smoke.js && node test/daily-cycle-universe-smoke.js`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/collector.js test/collector-envelope-smoke.js
git commit -m "feat(sp7-phase-c): fundamentals+insider scope to floored adopted-union (parent decision 4)"
```

---

### Task 12: Doctor check `collector_envelope_freshness`

**Files:**
- Modify: `src/maintenance/doctor.py` (new check; pattern: `check_redis_reachable` at ~316)
- Test: `tests/test_doctor_envelope_check.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor_envelope_check.py
"""SP-7 Phase C Task 12 — doctor envelope freshness check."""


def test_gate_off_is_ok(monkeypatch):
    monkeypatch.delenv("OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE", raising=False)
    from src.maintenance.doctor import PASS, check_collector_envelope_freshness
    out = check_collector_envelope_freshness()
    assert out["severity"] == PASS   # doctor severities are lowercase strings ('pass')
    assert "gate off" in out["detail"]


def test_check_is_registered_slow():
    from src.maintenance.doctor import check_collector_envelope_freshness as fn
    assert fn._check_name == "collector_envelope_freshness"
    assert fn._slow is True
```

(doctor.py severity constants are lowercase strings: `PASS='pass'`, `WARN='warn'`, `FAIL='fail'` — import `PASS` and compare directly.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_doctor_envelope_check.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement in doctor.py** (place near `check_redis_reachable`):

```python
@_check('collector_envelope_freshness', slow=True)
def check_collector_envelope_freshness():
    """SP-7 Phase C C2: today's no-floor envelope is cached in Redis and sane.

    TZ note: the key date here is date.today() (process-local TZ) while the
    collector writes with JS toISOString() (UTC). The VPS runs UTC so they
    agree; if the box TZ ever changes, a spurious 'no key' WARN near midnight
    is the symptom."""
    if os.environ.get('OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE') != '1':
        return _ok('collector_envelope_freshness', 'gate off — universe_config envelope')
    try:
        import json as _json
        from datetime import date as _date
        import redis as _redis
        r = _redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                            socket_connect_timeout=3)
        key = f'universe:envelope:{_date.today().isoformat()}:live'
        raw = r.get(key)
    except Exception as exc:
        return _warn('collector_envelope_freshness', f'{type(exc).__name__}: {exc}'[:80])
    if not raw:
        return _warn('collector_envelope_freshness',
                     f'no {key} — collector may not have run yet today')
    n = len(_json.loads(raw))
    if n < 200:
        return _fail('collector_envelope_freshness', f'envelope suspiciously small: {n}')
    return _ok('collector_envelope_freshness', f'envelope {n} tickers cached')
```

- [ ] **Step 4: Run**

Run: `python3 -m pytest tests/test_doctor_envelope_check.py -v && python3 src/maintenance/doctor.py --quick 2>&1 | tail -5`
Expected: tests PASS; doctor still exits cleanly (new check is slow=True → excluded from --quick)

- [ ] **Step 5: Commit**

```bash
git add src/maintenance/doctor.py tests/test_doctor_envelope_check.py
git commit -m "feat(sp7-phase-c): doctor collector_envelope_freshness (slow)"
```

---

### Task 13: Sentiment step — resolver-widened universe (gated)

**Files:**
- Modify: `src/pipeline/run_sentiment_step.py` (main(), after the `current_universe` stage ~line 207)
- Test: `tests/test_sentiment_resolver_universe.py`

⚠️ Merge note: the LIVE checkout carries uncommitted `_append_parquet` changes in this file (disjoint region). Build in the worktree; at merge time keep both hunks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sentiment_resolver_universe.py
"""SP-7 Phase C Task 13 — sentiment universe widened by adopted-union (gated)."""
from datetime import date


def test_widen_helper_unions_and_bridges(monkeypatch):
    import src.pipeline.run_sentiment_step as step

    class FakeResolver:
        def union_universe(self, as_of, states):
            assert states == ("live", "candidate")
            return ["BRK.B", "ADOPTED1"]

    monkeypatch.setenv("OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE", "1")
    out = step._widen_with_resolver(["AAPL"], today=date(2026, 6, 8),
                                    resolver=FakeResolver())
    assert set(out) == {"AAPL", "BRK-B", "ADOPTED1"}   # union + dot→dash


def test_gate_off_identity(monkeypatch):
    import src.pipeline.run_sentiment_step as step
    monkeypatch.delenv("OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE", raising=False)
    out = step._widen_with_resolver(["AAPL"], today=date(2026, 6, 8),
                                    resolver=None)
    assert out == ["AAPL"]


def test_resolver_failure_fails_open(monkeypatch):
    import src.pipeline.run_sentiment_step as step

    class Boom:
        def union_universe(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setenv("OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE", "1")
    out = step._widen_with_resolver(["AAPL"], today=date(2026, 6, 8),
                                    resolver=Boom())
    assert out == ["AAPL"]                              # sentiment must not die
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sentiment_resolver_universe.py -v`
Expected: FAIL — no `_widen_with_resolver`

- [ ] **Step 3: Implement.** In `run_sentiment_step.py`, next to the existing `_select_sentiment_universe` helper (~line 102):

```python
def _widen_with_resolver(universe, today=None, resolver=None):
    """SP-7 Phase C (C3): widen the 3-source sentiment universe with the
    adopted-union (live+candidate) resolver universe. Gated; fail-open —
    sentiment must never die on resolver issues."""
    if os.environ.get('OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE') != '1':
        return list(universe)
    try:
        if resolver is None:
            from src.execution.live_universe import build_resolver
            resolver = build_resolver()
        as_of = today or date.today()
        extra = _select_sentiment_universe(as_of, resolver)
        extra_dash = {t.replace('.', '-') for t in extra}   # metadata dot → repo dash
        widened = sorted(set(universe) | extra_dash)
        logger.info('sentiment: resolver universe +%d (%d→%d)',
                    len(widened) - len(universe), len(universe), len(widened))
        return widened
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning('sentiment resolver-universe failed (fail-open): %s', e)
        return list(universe)
```

and in `main()` insert BETWEEN the `logger.info('sentiment: universe %d tickers', ...)` line (~211) and `universe_set = set(universe)` (~212), so the widened universe feeds the rest of the step:

```python
    universe = _widen_with_resolver(universe)
```

- [ ] **Step 4: Run new + existing sentiment tests**

Run: `python3 -m pytest tests/test_sentiment_resolver_universe.py tests/test_run_sentiment_step.py tests/test_resolve_sentiment_universe.py -v`
Expected: ALL PASS (gate off by default in tests → byte-identical)

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/run_sentiment_step.py tests/test_sentiment_resolver_universe.py
git commit -m "feat(sp7-phase-c): sentiment universe widened by adopted-union (gate OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE)"
```

---

### Task 14: Options archive — options-eligible ∩ live union (gated)

**Files:**
- Modify: `src/pipeline/backfillers/alpaca_options.py` (main() ~247; new `_resolver_archive_universe`)
- Test: `tests/test_options_archive_resolver.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_archive_resolver.py
"""SP-7 Phase C Task 14 — archive universe = options-eligible ∩ live union (gated)."""


def _meta(sym, eligible):
    m = type("M", (), {})()
    m.options_eligible = eligible
    return m


class FakeResolver:
    def __init__(self):
        class DB:
            def fetch_metadata_as_of(self, as_of):
                rows = []
                for sym, el in (("AAPL", True), ("NOOPT", False)):
                    r = type("R", (), {})()
                    r.symbol = sym
                    r.metadata = _meta(sym, el)
                    rows.append(r)
                return rows
        self._db = DB()
    def union_universe(self, as_of, states):
        return ["AAPL", "NOOPT", "GHOST"]   # GHOST absent from metadata


def test_gate_off_returns_none(monkeypatch):
    monkeypatch.delenv("OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE", raising=False)
    from src.pipeline.backfillers.alpaca_options import _resolver_archive_universe
    assert _resolver_archive_universe("2026-06-08", resolver=FakeResolver()) is None


def test_gate_on_filters_options_eligible(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE", "1")
    from src.pipeline.backfillers.alpaca_options import _resolver_archive_universe
    out = _resolver_archive_universe("2026-06-08", resolver=FakeResolver())
    assert out == ["AAPL"]      # NOOPT not eligible; GHOST no metadata → excluded


def test_failure_returns_none(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE", "1")

    class Boom:
        def union_universe(self, *a, **k):
            raise RuntimeError("down")
        _db = None

    from src.pipeline.backfillers.alpaca_options import _resolver_archive_universe
    assert _resolver_archive_universe("2026-06-08", resolver=Boom()) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_options_archive_resolver.py -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement** in `alpaca_options.py` (below `_select_archive_universe`):

```python
def _resolver_archive_universe(date_str: str, resolver=None) -> list[str] | None:
    """SP-7 Phase C (C3): gated archive universe = options-eligible ∩ live
    adopted-union, via the Phase-A helper _select_archive_universe.
    Returns None on gate-off or ANY failure → caller falls back to
    _load_universe() (fail-open; the archive must keep accruing)."""
    if os.environ.get('OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE') != '1':
        return None
    try:
        from datetime import date as _d
        as_of = _d.fromisoformat(date_str)
        if resolver is None:
            from src.execution.live_universe import build_resolver
            resolver = build_resolver()
        # Reuse the resolver's (memoized) adapter for the eligibility lookup.
        meta_rows = resolver._db.fetch_metadata_as_of(as_of)
        meta_map = {r.symbol: r.metadata for r in meta_rows}

        class _NoMeta:           # absent from metadata → not options-eligible
            options_eligible = False

        def meta_lookup(sym, _as_of):
            return meta_map.get(sym, _NoMeta)

        out = _select_archive_universe(as_of, resolver, meta_lookup)
        return out or None
    except Exception as e:  # noqa: BLE001 — fail-open to universe_config
        log.warning('archive resolver-universe failed (fail-open): %s', e)
        return None
```

and in `main()` (line ~247) replace `universe = _load_universe()` with:

```python
    universe = _resolver_archive_universe(date) or _load_universe()
```

- [ ] **Step 4: Run new + existing archive tests**

Run: `python3 -m pytest tests/test_options_archive_resolver.py tests/test_options_archive.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/backfillers/alpaca_options.py tests/test_options_archive_resolver.py
git commit -m "feat(sp7-phase-c): options archive on options-eligible ∩ live union (gate OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE)"
```

---

### Task 15: Consumer envelope-assertion pins (redeploy, screener)

Spec §5: every consumer in the disposition table gets one explicit envelope assertion. Tasks 10–14 covered the wired ones; this pins the two no-change consumers so silent drift is caught.

**Files:**
- Test: `tests/test_sp7_phase_c_consumer_envelopes.py`

- [ ] **Step 1: Write the tests** (these should PASS immediately — they pin CURRENT behavior; if any fails, the codebase drifted from the audit and the task is to investigate, not force-pass)

```python
# tests/test_sp7_phase_c_consumer_envelopes.py
"""SP-7 Phase C Task 15 — envelope assertions for no-change consumers (spec §5)."""
import re
from pathlib import Path

ROOT = Path("/root/openclaw")


def test_redeploy_inherits_engine_universe():
    """Redeploy re-runs the engine signals step — it must have NO independent
    universe authority (it inherits C1 automatically)."""
    src = (ROOT / "scripts/redeploy_pipeline.py").read_text()
    assert "REDEPLOY_STEPS = 'signals,handoff,trade,alpaca,reconcile'" in src
    assert "universe_resolver" not in src
    assert "universe_config" not in src


def test_screener_inserts_inactive_only():
    """Screener candidates land active=false — the operator overlay stays the
    sole promotion path, and active=false remains a hard exclusion (C2)."""
    src = (ROOT / "src/pipeline/alpaca_screener.js").read_text()
    insert = re.search(r"INSERT INTO universe_config[\s\S]{0,400}", src)
    assert insert, "screener no longer inserts into universe_config?"
    assert re.search(r"\bfalse\b", insert.group(0)), \
        "screener INSERT no longer pins active=false"


def test_sentiment_default_path_is_three_source_union():
    """Gate off → current_universe (universe_config ∪ positions ∪ 7d signals)."""
    src = (ROOT / "src/pipeline/run_sentiment_step.py").read_text()
    assert "current_universe(pg_uri)" in src
    assert "_widen_with_resolver" in src     # Task 13 wiring present, gated


def test_options_archive_default_path_is_universe_config():
    src = (ROOT / "src/pipeline/backfillers/alpaca_options.py").read_text()
    assert "_resolver_archive_universe(date) or _load_universe()" in src
```

- [ ] **Step 2: Run**

Run: `python3 -m pytest tests/test_sp7_phase_c_consumer_envelopes.py -v`
Expected: 4 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_sp7_phase_c_consumer_envelopes.py
git commit -m "test(sp7-phase-c): envelope-assertion pins for redeploy + screener + gated-default paths"
```

---

### Task 16: Runbook, .env documentation, full regression sweep

**Files:**
- Create: `docs/sp7-phase-c-runbook.md`
- Modify: `docs/superpowers/specs/2026-06-07-sp7-phase-c-live-wiring-design.md` (append "as-built" note only if anything deviated)

- [ ] **Step 1: Write `docs/sp7-phase-c-runbook.md`** (mirror Phase B's numbered-section structure):

```markdown
# SP-7 Phase C — Live Wiring Activation Runbook

Operator-gated. Every gate is default-OFF; merge changes nothing behavioral.
Spec: docs/superpowers/specs/2026-06-07-sp7-phase-c-live-wiring-design.md

## 0. GATES INTRODUCED (all default-OFF)
| Gate | Effect when =1 |
|---|---|
| OPENCLAW_LIVE_UNIVERSE_SHADOW | signals step writes universe_shadow_parity diff rows (non-fatal sidecar) |
| OPENCLAW_LIVE_UNIVERSE_RESOLVER | engine builds per-strategy universes via resolver; clamp superseded |
| OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE | collector prices fetch = no-floor envelope; fundamentals/insider = adopted-union |
| OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE | sentiment universe widened by adopted-union (live+candidate) |
| OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE | options archive = options-eligible ∩ live union |

## 1. MERGE + MIGRATE
- merge feat/sp7-phase-c-live-wiring → live branch; push
- `npm run db:migrate` then VERIFY (runner wart): `python3 -c "import os,psycopg2;from dotenv import load_dotenv;load_dotenv('.env');c=psycopg2.connect(os.environ['POSTGRES_URI']);cur=c.cursor();cur.execute(\"SELECT to_regclass('universe_shadow_parity')\");print(cur.fetchone())"`
- ⚠️ merge-conflict watch: run_sentiment_step.py has live uncommitted hunks (parquet datetime fix) — keep BOTH

## 2. SHADOW ON (same day as merge is fine — zero behavior change)
- .env: add `OPENCLAW_LIVE_UNIVERSE_SHADOW=1`
- `systemctl --user restart johnbot` (USER service — a stale SYSTEM unit exists, do not start it)
- next 10:00 ET cycle: confirm `universe_shadow_parity` rows ≈ strategy count
  `SELECT run_date, count(*), count(*) FILTER (WHERE NOT is_adopted AND jsonb_array_length(added_tickers)+jsonb_array_length(removed_tickers)>0) AS unadopted_drift FROM universe_shadow_parity GROUP BY 1 ORDER BY 1 DESC;`

## 3. SHADOW WATCH (≥3 trading days)
- daily: `python3 -m src.system_checks --check universe_shadow_parity`
- target: PASS "3 day(s) clean". WARN = drift (diagnose per-ticker via added/removed columns; remedies: widen default predicate / adopt the strategy / fix universe_config category). FAIL = resolve_error → code bug, fix before proceeding.
- DECISION RULE for the one known systematic diff (clamp keeps in_sp500 names with <60 bars; resolver's floor excludes them — verified EMPTY at plan time 2026-06-07, but a reconstitution adding a recent IPO can create it mid-shadow): a WARN is ACCEPTABLE FOR FLIP iff every removed ticker across all un-adopted rows is sub-floor. Classify with:
  `python3 -c "import pandas as pd; c=pd.read_parquet('data/master/prices.parquet',columns=['ticker']).groupby('ticker').size(); print(sorted(c[c<60].index))"`
  vs `SELECT DISTINCT jsonb_array_elements_text(removed_tickers) FROM universe_shadow_parity WHERE NOT is_adopted AND run_date >= current_date - 3;`
  Rationale: <60-bar names cannot fill strategy lookback windows, so zero-SIGNAL-delta (the spec's criterion) still holds. Record the classification in the flip-prereq notes. Any removed name NOT sub-floor = real drift, no flip.
- adoptions landing mid-window (ladder auto-adopt) do NOT reset the clock — adopted rows never gate.

## 4. C2 FLIP (collector envelope) — AFTER ladder drained + adoptions decided, ≥1 trading day BEFORE C1
- prereq: `redis-cli get sp7:ladder:last_full_run` non-nil; adoptions decided (universe-recs all resolved)
- .env: `OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE=1`; restart johnbot
- NOTE this gate also moves fundamentals (FMP) + insider (EDGAR) scope to the floored adopted-union at the same moment (spec §5) — confirm `adopted-union scope: fundamentals=N insider=M` in the log
- next collect: grep log `envelope: resolver=N config=M excluded=K final=F`; watch collect wall time (first post-tier_liquid-adoption run is the soak point)
- `python3 src/maintenance/doctor.py | grep collector_envelope`

## 5. C1 FLIP (engine per-strategy universes)
- PREREQS (all): ladder drained; adoptions decided; `universe_shadow_parity` = PASS 3 days; C2 ON ≥1 trading day; `universe_tier_coherence` PASS
- .env: `OPENCLAW_LIVE_UNIVERSE_RESOLVER=1`; restart johnbot
- observe 1 cycle: log `live-universe ON: union N tickers, 67 strategies, 0 fail-open`; signal counts sane vs prior day; NO empty-universe warnings
- rollback: gate =0 + restart (shadow resumes)

## 6. CLAMP DELETION (immediately after a clean flipped cycle — DELETE, not gate-off)
- `git rm src/execution/universe_clamp.py tests/execution/test_universe_clamp.py`
- adapt `tests/execution/test_live_universe.py::test_unadopted_equals_clamp_output` — it imports `clamp_universe` for the differential check and would orphan post-deletion; by deletion time it has served its purpose: replace the clamp call with the expected literal set (or delete the test, keeping the other 6)
- engine.py: remove the two clamp lines (`from execution.universe_clamp import clamp_universe` / `universe = clamp_universe(universe)`) and the now-dead OPENCLAW_LIVE_UNIVERSE_SHADOW sidecar block (shadow has nothing to diff post-clamp)
- .env: remove OPENCLAW_ENGINE_UNIVERSE_CLAMP + OPENCLAW_LIVE_UNIVERSE_SHADOW
- grep-verify: `grep -rn "universe_clamp\|ENGINE_UNIVERSE_CLAMP" src/ tests/ scripts/` → only historical docs
- commit + restart; universe_shadow_parity TABLE stays (audit history — never delete)

## 7. C3 FLIPS (individually, any time after C2; each independently revertible)
- `OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE=1` → next sentiment cycle: log `sentiment: resolver universe +N`
- `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE=1` → next 16:30 ET archive: log `tickers=N` grows to eligible∩union
- fundamentals/insider scoping rides the C2 gate (already ON)

## 8. ROLLBACK MATRIX
| Symptom | Action |
|---|---|
| shadow rows missing / errors | it's non-fatal — check logs; OPENCLAW_LIVE_UNIVERSE_SHADOW=0 silences |
| collect step slow / quota burn | OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE=0 + restart |
| signals anomalies post-C1-flip | OPENCLAW_LIVE_UNIVERSE_RESOLVER=0 + restart (pre-deletion); post-deletion: git revert the deletion commit |
| sentiment/archive issues | respective gate =0 |
```

- [ ] **Step 2: Full regression sweep** (chunked per-file, sequential; OUTSIDE 01:00–13:00 UTC)

```bash
cd /root/openclaw
for f in tests/test_coverage_index.py tests/test_db_adapters_memo.py \
         tests/test_resolver_perf_smoke.py tests/test_envelope_universe.py \
         tests/test_universe_resolver.py tests/test_resolver_cli.py \
         tests/test_quarantine_filter_integration.py \
         tests/execution/test_live_universe.py tests/execution/test_universe_clamp.py \
         tests/test_engine_live_universe_gate.py tests/test_engine_shadow_sidecar.py \
         tests/test_engine_run_stats.py tests/test_engine_regime_gate.py \
         tests/test_engine_sentiment_aux.py tests/test_engine_override.py \
         tests/test_universe_shadow_parity_check.py tests/test_doctor_envelope_check.py \
         tests/test_sentiment_resolver_universe.py tests/test_run_sentiment_step.py \
         tests/test_resolve_sentiment_universe.py tests/test_options_archive_resolver.py \
         tests/test_options_archive.py tests/test_sp7_phase_c_consumer_envelopes.py \
         tests/test_sp2_smoke.py; do
  python3 -m pytest "$f" -v -m "not integration" || { echo "FAILED: $f"; break; }
done
node test/collector-envelope-smoke.js
node test/collector-eod-freshness-smoke.js
node test/daily-cycle-universe-smoke.js
```

Expected: every file PASS. Known pre-existing flake: `test_sp2_smoke.py::test_system_checks_pass` `universe_resolution` latency under load (LRN: post-Task-3 the CLI is fast — this flake should now be GONE; if it persists, report, don't exempt).

- [ ] **Step 3: Commit**

```bash
git add docs/sp7-phase-c-runbook.md
git commit -m "docs(sp7-phase-c): activation runbook (shadow → C2 → C1 → clamp deletion → C3)"
```

---

## Spec-coverage map (self-review)

| Spec § | Task(s) |
|---|---|
| §3.1 CoverageIndex hoist | 1 |
| §3.2 shared-conn/memo + perf <10s | 2, 3, 5(Step 6) |
| §3.3 live_universe mirror-clamp | 5 |
| §3.4 engine gate + slicing + one panel | 6 |
| §3.5 shadow + mig 133 + system_check | 4, 7, 8 |
| §3.6 flip + clamp deletion | runbook (16) — operator-executed |
| §4 envelope_universe + collector + never-shrink + doctor | 9, 10, 12 |
| §5 dispositions: fundamentals/insider, sentiment, archive, redeploy, screener pins | 11, 13, 14, 15 |
| §6 error handling (fail-open per strategy / never-shrink / non-fatal sidecar / split-source visibility) | 5 (fail-open test), 7 (sidecar), 10 (never-shrink test), 5+8 (predicate recorded per row) |
| §7 testing (incl. byte-identical-when-OFF, JS smokes) | every task; gate-OFF identity: 6 (engine), 10 (collector), 13 (sentiment), 14 (archive) |
| §8 sequencing | task order + runbook |
| §10 acceptance | 8 (3-day parity check), runbook §4 log-verify (envelope), 11 (adopted-union scoping), 15 (consumer assertions) |
