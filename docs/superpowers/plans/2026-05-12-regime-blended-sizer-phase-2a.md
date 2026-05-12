# Phase 2A — Per-(Strategy, Regime) Parameter Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate eligibility out of `manifest.json` into a new DB table `strategy_regime_params` keyed by `(strategy_id, regime_state)`; add four nullable columns (`size_scalar`, `stop_pct`, `target_pct`, `max_hold_days`) that future Phase 2B will populate.

**Architecture:** New DB tables (migrations 076, 077). Tiny data-access layer + 30s in-process cache. Consumer rewrites: `regime_gate.is_eligible()` and `trade_handoff_builder.regime_scale` read from DB with backward-compat fallback. Dashboard regime cells POST to a new `/api/regime-params/:strategy/:regime` route; old `/api/regime-eligibility/:strategy` keeps working via a shim. `eligibility_manager.py` rewritten to use DB transactions instead of file locks.

**Tech Stack:** PostgreSQL, Python 3 (psycopg2, pytest), Node/Express, vanilla JS dashboard.

Spec: `docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md`

---

## File Structure

**New files:**
- `src/database/migrations/076_strategy_regime_params.sql`
- `src/database/migrations/077_strategy_regime_param_changes.sql`
- `src/execution/regime_param_resolver.py` — data-access + cache + public read API
- `scripts/seed_strategy_regime_params.py` — one-time seed from current manifest
- `src/channels/api/routes_regime_params.js` — new POST/GET endpoints
- `tests/test_regime_param_resolver.py`
- `tests/test_regime_gate_db.py`
- `tests/test_eligibility_manager_db.py`
- `tests/test_seed_strategy_regime_params.py`
- `tests/test_doctor_strategy_regime_params_consistency.py`

**Modified files:**
- `src/strategies/regime_gate.py` — switch to DB read
- `src/strategies/eligibility_manager.py` — switch from manifest write to DB write
- `src/execution/trade_handoff_builder.py` — replace hardcoded `scale` dict with resolver call
- `src/maintenance/doctor.py` — add `strategy_regime_params_consistency`; repurpose `manifest_eligibility_drift`
- `src/channels/api/server.js` — mount new router; back-compat shim on `routes_regime_eligibility.js`
- `src/channels/api/routes_regime_eligibility.js` — back-compat shim (writes to DB instead of manifest)

**Out of scope for 2A (deferred to 2B):** wiring `stop_pct` / `target_pct` / `max_hold_days` overrides into the sizer/bracket-builder. The columns exist and the resolver exposes getters returning `None`-or-value; no consumer calls those getters yet because 2A doesn't populate the rows. Spec §3 acknowledges this.

---

## Task 1: Migrations 076 + 077

**Files:**
- Create: `src/database/migrations/076_strategy_regime_params.sql`
- Create: `src/database/migrations/077_strategy_regime_param_changes.sql`

- [ ] **Step 1: Write migration 076**

```sql
-- 076_strategy_regime_params.sql
-- Per-(strategy, regime) parameter row. Source of truth for regime_gate.is_eligible
-- and (Phase 2B onwards) per-regime size/stop/target/max-hold overrides.
-- Migrates ownership of eligible_regimes OUT of manifest.json. Append-only by
-- convention: every write also lands an audit row in strategy_regime_param_changes.

CREATE TABLE IF NOT EXISTS strategy_regime_params (
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,  -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    eligible        BOOLEAN      NOT NULL,
    size_scalar     NUMERIC,                 -- NULL = fall back to Phase 1 regime-only static scalar
    stop_pct        NUMERIC,                 -- NULL = inherit strategy-wide default (Phase 2B populates)
    target_pct      NUMERIC,                 -- NULL = inherit strategy-wide default
    max_hold_days   INTEGER,                 -- NULL = inherit strategy-wide default
    set_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    set_by          TEXT         NOT NULL,   -- 'migration:from-manifest-2026-05-12' | 'operator:<name>' | 'mastermind' (future)
    PRIMARY KEY (strategy_id, regime_state)
);
```

- [ ] **Step 2: Write migration 077**

```sql
-- 077_strategy_regime_param_changes.sql
-- Append-only audit of every write to strategy_regime_params. JSONB before/after
-- snapshots make rollback trivial. Replaces regime_eligibility_changes (which stays
-- read-only for historical reference).

CREATE TABLE IF NOT EXISTS strategy_regime_param_changes (
    id              BIGSERIAL    PRIMARY KEY,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor           TEXT         NOT NULL,
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,
    before_row      JSONB,                    -- full prior row; NULL on first set
    after_row       JSONB        NOT NULL,
    reason          TEXT,
    source          TEXT                       -- 'dashboard' | 'cli' | 'mastermind' | 'migration'
);

CREATE INDEX IF NOT EXISTS idx_srpc_strategy_regime_time
    ON strategy_regime_param_changes (strategy_id, regime_state, changed_at DESC);
```

- [ ] **Step 3: Apply migrations**

```bash
docker exec -i openclaw-postgres psql -U openclaw -d openclaw \
    < /root/openclaw/src/database/migrations/076_strategy_regime_params.sql
docker exec -i openclaw-postgres psql -U openclaw -d openclaw \
    < /root/openclaw/src/database/migrations/077_strategy_regime_param_changes.sql
```

Expected: two `CREATE TABLE` + one `CREATE INDEX` line, no errors.

- [ ] **Step 4: Smoke**

```bash
docker exec openclaw-postgres psql -U openclaw -d openclaw -c "
    SELECT COUNT(*) AS params, (SELECT COUNT(*) FROM strategy_regime_param_changes) AS audit
      FROM strategy_regime_params;"
```

Expected: `0 | 0`.

- [ ] **Step 5: Commit**

```bash
git add src/database/migrations/076_strategy_regime_params.sql \
        src/database/migrations/077_strategy_regime_param_changes.sql
git commit -m "feat(db): migrations 076+077 — strategy_regime_params + audit"
```

---

## Task 2: Resolver module — data-access + cache + read API

**Files:**
- Create: `src/execution/regime_param_resolver.py`
- Test: `tests/test_regime_param_resolver.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_regime_param_resolver.py
"""Tests for regime_param_resolver — DB-backed param read API + cache."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import regime_param_resolver as rpr  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.executed = []

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()): self.executed.append((sql, params))
    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class FakeConn:
    def __init__(self, rows=()):
        self.cur = FakeCursor(rows)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): pass


def test_get_row_missing_returns_none(monkeypatch):
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[]))
    rpr._cache.clear()
    assert rpr.get_row('strat_x', 'LOW_VOL') is None


def test_get_row_present_returns_dict(monkeypatch):
    row = ('strat_x', 'LOW_VOL', True, 0.5, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    r = rpr.get_row('strat_x', 'LOW_VOL')
    assert r['strategy_id'] == 'strat_x'
    assert r['regime_state'] == 'LOW_VOL'
    assert r['eligible'] is True
    assert float(r['size_scalar']) == 0.5


def test_cache_hit_skips_db(monkeypatch):
    row = ('s1', 'LOW_VOL', True, None, None, None, None)
    calls = {'n': 0}

    def factory():
        calls['n'] += 1
        return FakeConn(rows=[row] if calls['n'] == 1 else [])
    monkeypatch.setattr(rpr, '_connect', factory)
    rpr._cache.clear()
    rpr.get_row('s1', 'LOW_VOL')   # populates
    rpr.get_row('s1', 'LOW_VOL')   # cache hit
    assert calls['n'] == 1


def test_cache_invalidate_forces_refetch(monkeypatch):
    row1 = ('s1', 'LOW_VOL', True, None, None, None, None)
    row2 = ('s1', 'LOW_VOL', False, None, None, None, None)
    calls = {'n': 0}

    def factory():
        calls['n'] += 1
        return FakeConn(rows=[row1 if calls['n'] == 1 else row2])
    monkeypatch.setattr(rpr, '_connect', factory)
    rpr._cache.clear()
    assert rpr.get_row('s1', 'LOW_VOL')['eligible'] is True
    rpr.invalidate('s1', 'LOW_VOL')
    assert rpr.get_row('s1', 'LOW_VOL')['eligible'] is False
    assert calls['n'] == 2


def test_is_eligible_missing_returns_true(monkeypatch):
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[]))
    rpr._cache.clear()
    assert rpr.is_eligible('unmigrated_strategy', 'LOW_VOL') is True


def test_is_eligible_false_when_row_false(monkeypatch):
    row = ('s1', 'HIGH_VOL', False, None, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    assert rpr.is_eligible('s1', 'HIGH_VOL') is False


def test_size_scalar_null_returns_phase1_default(monkeypatch):
    row = ('s1', 'LOW_VOL', True, None, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    assert rpr.size_scalar('s1', 'LOW_VOL') == 1.0
    assert rpr.size_scalar('s1', 'TRANSITIONING') == 0.55
    assert rpr.size_scalar('s1', 'HIGH_VOL') == 0.35
    assert rpr.size_scalar('s1', 'CRISIS') == 0.15


def test_size_scalar_non_null_overrides(monkeypatch):
    row = ('s1', 'HIGH_VOL', True, 0.5, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    assert rpr.size_scalar('s1', 'HIGH_VOL') == 0.5
```

- [ ] **Step 2: Run tests, expect ImportError**

```bash
cd /root/openclaw && pytest tests/test_regime_param_resolver.py -v 2>&1 | tail -10
```

Expected: ImportError on `regime_param_resolver`.

- [ ] **Step 3: Implement the resolver module**

```python
# src/execution/regime_param_resolver.py
"""DB-backed reader for per-(strategy, regime) eligibility + tunable params.

Caches (strategy_id, regime_state) → row with a 30s TTL plus explicit
invalidate(). All writers (dashboard, CLI, future Mastermind) MUST call
invalidate() after committing a write so the same-process consumer sees
the new value within milliseconds; cross-process freshness is bounded by
the TTL.

Backward-compat semantics:
- Row missing (strategy not yet migrated, or never seeded) → is_eligible
  returns True; size_scalar returns the Phase 1 regime-only static default.
- size_scalar/stop_pct/target_pct/max_hold_days are NULL by default — the
  *_override getters return None and callers fall back to existing logic.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

# Phase 1 regime-only static scalars (canonical source: trade_handoff_builder.py
# line 156). Duplicated here for backward-compat fallback only. The dict in
# trade_handoff_builder gets replaced by a call to size_scalar() in Task 5.
PHASE1_REGIME_SCALARS: dict[str, float] = {
    'LOW_VOL':       1.0,
    'TRANSITIONING': 0.55,
    'HIGH_VOL':      0.35,
    'CRISIS':        0.15,
}

CACHE_TTL_SECONDS = 30.0
_COLUMNS = ('strategy_id', 'regime_state', 'eligible',
            'size_scalar', 'stop_pct', 'target_pct', 'max_hold_days')


class _Cache:
    def __init__(self):
        self._d: dict[tuple, tuple] = {}   # (sid, regime) -> (row_or_None, expires_at)
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._d.get(key)
            if entry is None:
                return _MISS
            row, expires_at = entry
            if time.monotonic() >= expires_at:
                self._d.pop(key, None)
                return _MISS
            return row

    def set(self, key, row):
        with self._lock:
            self._d[key] = (row, time.monotonic() + CACHE_TTL_SECONDS)

    def invalidate(self, key):
        with self._lock:
            self._d.pop(key, None)

    def clear(self):
        with self._lock:
            self._d.clear()


_MISS = object()
_cache = _Cache()


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _fetch_row(strategy_id: str, regime_state: str) -> Optional[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, eligible,
                       size_scalar, stop_pct, target_pct, max_hold_days
                  FROM strategy_regime_params
                 WHERE strategy_id = %s AND regime_state = %s
            """, (strategy_id, regime_state))
            row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(_COLUMNS, row))


def get_row(strategy_id: str, regime_state: str) -> Optional[dict]:
    """Cached read of a single row. None when no row exists."""
    key = (strategy_id, regime_state)
    cached = _cache.get(key)
    if cached is _MISS:
        row = _fetch_row(strategy_id, regime_state)
        _cache.set(key, row)
        return row
    return cached


def invalidate(strategy_id: str, regime_state: str) -> None:
    _cache.invalidate((strategy_id, regime_state))


# ── Public read API ────────────────────────────────────────────────────────

def is_eligible(strategy_id: str, regime_state: str) -> bool:
    row = get_row(strategy_id, regime_state)
    if row is None:
        return True   # backward-compat: not migrated → eligible everywhere
    return bool(row['eligible'])


def size_scalar(strategy_id: str, regime_state: str) -> float:
    row = get_row(strategy_id, regime_state)
    if row is None or row['size_scalar'] is None:
        return PHASE1_REGIME_SCALARS.get(regime_state, PHASE1_REGIME_SCALARS['TRANSITIONING'])
    return float(row['size_scalar'])


def stop_pct_override(strategy_id: str, regime_state: str) -> Optional[float]:
    row = get_row(strategy_id, regime_state)
    if row is None or row['stop_pct'] is None:
        return None
    return float(row['stop_pct'])


def target_pct_override(strategy_id: str, regime_state: str) -> Optional[float]:
    row = get_row(strategy_id, regime_state)
    if row is None or row['target_pct'] is None:
        return None
    return float(row['target_pct'])


def max_hold_days_override(strategy_id: str, regime_state: str) -> Optional[int]:
    row = get_row(strategy_id, regime_state)
    if row is None or row['max_hold_days'] is None:
        return None
    return int(row['max_hold_days'])
```

- [ ] **Step 4: Run tests, expect all 8 to pass**

```bash
cd /root/openclaw && pytest tests/test_regime_param_resolver.py -v 2>&1 | tail -15
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_param_resolver.py tests/test_regime_param_resolver.py
git commit -m "feat(execution): regime_param_resolver — DB read API + cache"
```

---

## Task 3: Seed strategy_regime_params from current manifest

**Files:**
- Create: `scripts/seed_strategy_regime_params.py`
- Test: `tests/test_seed_strategy_regime_params.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_seed_strategy_regime_params.py
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import seed_strategy_regime_params as seed  # noqa: E402


def test_compute_rows_eligible_field_present():
    manifest = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']},
    }}
    rows = seed.compute_rows(manifest)
    by_regime = {r['regime_state']: r for r in rows if r['strategy_id'] == 's1'}
    assert by_regime['LOW_VOL']['eligible'] is True
    assert by_regime['TRANSITIONING']['eligible'] is True
    assert by_regime['HIGH_VOL']['eligible'] is False
    assert by_regime['CRISIS']['eligible'] is False


def test_compute_rows_no_field_means_all_eligible():
    manifest = {'strategies': {'legacy': {}}}
    rows = seed.compute_rows(manifest)
    for r in rows:
        assert r['eligible'] is True


def test_compute_rows_empty_list_means_all_eligible():
    """Backward-compat: empty list under Phase 1 gate semantics returned True.
    Migration preserves that interpretation."""
    manifest = {'strategies': {'edge': {'eligible_regimes': []}}}
    rows = seed.compute_rows(manifest)
    for r in rows:
        assert r['eligible'] is True


def test_compute_rows_produces_four_per_strategy():
    manifest = {'strategies': {
        's1': {'eligible_regimes': ['LOW_VOL']},
        's2': {'eligible_regimes': None},
    }}
    rows = seed.compute_rows(manifest)
    assert len(rows) == 8
    assert {r['regime_state'] for r in rows} == {
        'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}


def test_compute_rows_set_by_tagged_as_migration():
    manifest = {'strategies': {'s1': {'eligible_regimes': ['LOW_VOL']}}}
    rows = seed.compute_rows(manifest)
    for r in rows:
        assert r['set_by'].startswith('migration:')
```

- [ ] **Step 2: Run tests, expect ImportError**

```bash
cd /root/openclaw && pytest tests/test_seed_strategy_regime_params.py -v 2>&1 | tail -10
```

Expected: ImportError.

- [ ] **Step 3: Implement the seed script**

```python
#!/usr/bin/env python3
# scripts/seed_strategy_regime_params.py
"""One-time idempotent seed of strategy_regime_params from manifest.json.

Reads current manifest.eligible_regimes (or absence) and writes one row
per (strategy, canonical_regime) with backward-compat semantics:
  - eligible_regimes is None / missing → eligible = True for all 4 regimes
  - eligible_regimes is []             → eligible = True for all 4 regimes
                                          (matches Phase 1 gate backward-compat)
  - eligible_regimes is list           → eligible = (regime in list)

Idempotent via ON CONFLICT DO NOTHING. Re-running inserts zero new rows
once seeded.

Usage:
    python3 scripts/seed_strategy_regime_params.py
    python3 scripts/seed_strategy_regime_params.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
TODAY = datetime.now(timezone.utc).date().isoformat()


def compute_rows(manifest: dict) -> list[dict]:
    rows: list[dict] = []
    tag = f'migration:from-manifest-{TODAY}'
    for sid, rec in (manifest.get('strategies') or {}).items():
        declared = rec.get('eligible_regimes')
        for regime in CANONICAL_REGIMES:
            if declared is None or declared == []:
                eligible = True
            else:
                eligible = regime in declared
            rows.append({
                'strategy_id':  sid,
                'regime_state': regime,
                'eligible':     eligible,
                'set_by':       tag,
            })
    return rows


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def upsert(rows: list[dict]) -> int:
    import psycopg2
    sql = """
        INSERT INTO strategy_regime_params
            (strategy_id, regime_state, eligible, set_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (strategy_id, regime_state) DO NOTHING
    """
    n_inserted = 0
    with psycopg2.connect(_db_uri()) as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(sql, (r['strategy_id'], r['regime_state'],
                                   r['eligible'], r['set_by']))
                n_inserted += cur.rowcount
        conn.commit()
    return n_inserted


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    rows = compute_rows(manifest)
    print(f'computed {len(rows)} rows from {len(manifest.get("strategies") or {})} strategies')
    if args.dry_run:
        print('[dry-run] no rows written')
        return 0
    inserted = upsert(rows)
    print(f'[seed] inserted {inserted} new rows (existing rows untouched)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run tests, expect 5/5 pass**

```bash
cd /root/openclaw && pytest tests/test_seed_strategy_regime_params.py -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 5: Run the seed against the live DB**

```bash
cd /root/openclaw && python3 scripts/seed_strategy_regime_params.py
```

Expected: `inserted N new rows` where N = (# strategies) × 4. With 98 strategies, expect 392.

- [ ] **Step 6: Verify row count + a sample**

```bash
docker exec openclaw-postgres psql -U openclaw -d openclaw -c "
    SELECT COUNT(*) AS total,
           COUNT(*) FILTER (WHERE eligible) AS eligible_true,
           COUNT(*) FILTER (WHERE NOT eligible) AS eligible_false
      FROM strategy_regime_params;
    SELECT strategy_id, regime_state, eligible
      FROM strategy_regime_params
     WHERE strategy_id = 'momentum_12_1'
     ORDER BY regime_state;"
```

Expected: total = 392; momentum_12_1 has eligible=True for LOW_VOL/TRANSITIONING, False for HIGH_VOL/CRISIS (matches its committed manifest).

- [ ] **Step 7: Re-run seed to confirm idempotency**

```bash
cd /root/openclaw && python3 scripts/seed_strategy_regime_params.py
```

Expected: `inserted 0 new rows`.

- [ ] **Step 8: Commit**

```bash
git add scripts/seed_strategy_regime_params.py tests/test_seed_strategy_regime_params.py
git commit -m "feat(scripts): one-time seed of strategy_regime_params from manifest"
```

---

## Task 4: regime_gate.is_eligible() switches to DB

**Files:**
- Modify: `src/strategies/regime_gate.py`
- Test: `tests/test_regime_gate_db.py`

- [ ] **Step 1: Write the new gate tests**

```python
# tests/test_regime_gate_db.py
"""Tests for regime_gate.is_eligible after DB switch. Module mocks the
resolver so we exercise gate-logic decisions, not the DB itself."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import regime_gate  # noqa: E402


def test_eligible_when_resolver_returns_true(monkeypatch):
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible',
                        lambda sid, r: True)
    assert regime_gate.is_eligible('s1', 'LOW_VOL') is True


def test_not_eligible_when_resolver_returns_false(monkeypatch):
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible',
                        lambda sid, r: False)
    assert regime_gate.is_eligible('s1', 'HIGH_VOL') is False


def test_unknown_regime_state_rejected(monkeypatch):
    """Gate semantics: bogus regime string → False regardless of resolver."""
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible',
                        lambda sid, r: True)
    assert regime_gate.is_eligible('s1', 'NOT_A_REGIME') is False


def test_resolver_unavailable_falls_back_to_true(monkeypatch):
    """If the resolver raises (DB down), gate fails-open (returns True).
    The doctor check + cycle preflight catch the underlying issue."""
    def boom(sid, r): raise RuntimeError('db down')
    monkeypatch.setattr(regime_gate, '_resolver_is_eligible', boom)
    assert regime_gate.is_eligible('s1', 'LOW_VOL') is True
```

- [ ] **Step 2: Run tests, expect AttributeError on `_resolver_is_eligible`**

```bash
cd /root/openclaw && pytest tests/test_regime_gate_db.py -v 2>&1 | tail -10
```

- [ ] **Step 3: Rewrite regime_gate.py**

Replace entire contents of `/root/openclaw/src/strategies/regime_gate.py` with:

```python
"""Per-strategy regime-eligibility gate.

Called by `engine.run_strategies()` immediately before invoking each
strategy's `compute_signals()`. Source of truth: the
`strategy_regime_params` table via `execution.regime_param_resolver`.

Backward compat: if the resolver returns no row for a (strategy, regime)
pair — typically a freshly-added strategy not yet seeded — the gate
returns True ("eligible everywhere"). This matches Phase 1 manifest
semantics where a strategy without an `eligible_regimes` field was
treated as eligible in every regime.

Failure semantics: if the resolver raises (DB unavailable), the gate
fails-open (returns True). Skipping a strategy because of an infrastructure
hiccup would be worse than running it; the doctor check
`strategy_regime_params_consistency` + cycle preflight surfaces the
underlying problem.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure src/ is importable when this module is run/imported from various
# entry points.
_THIS_FILE = Path(__file__).resolve()
_SRC = _THIS_FILE.parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from execution.regime_param_resolver import is_eligible as _resolver_is_eligible  # noqa: E402

logger = logging.getLogger(__name__)

ALL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def is_eligible(strategy_id: str, regime_state: str) -> bool:
    """True if strategy_id should compute signals under current regime."""
    if regime_state not in ALL_REGIMES:
        logger.warning('regime_gate: unknown regime_state=%r; rejecting',
                       regime_state)
        return False
    try:
        return _resolver_is_eligible(strategy_id, regime_state)
    except Exception as exc:
        logger.error('regime_gate: resolver failed (%s); failing OPEN', exc)
        return True
```

- [ ] **Step 4: Run all gate-related tests**

```bash
cd /root/openclaw && pytest tests/test_regime_gate_db.py tests/test_regime_gate.py \
    tests/test_lifecycle_regime_gate.py -v 2>&1 | tail -15
```

Expected: 4 new gate-DB tests pass. `tests/test_regime_gate.py` was authored against the manifest-reading version — examine output; tests asserting "missing field → True" should still pass against the new resolver (since `is_eligible` returns True when resolver returns True OR no row exists). Tests asserting manifest-specific behavior may need to be updated.

- [ ] **Step 5: Adapt or remove manifest-coupled tests in test_regime_gate.py**

If `tests/test_regime_gate.py` has tests that monkeypatch the manifest reader (e.g., `_load_manifest`), update them to monkeypatch `_resolver_is_eligible` instead. Read the file first; for each test, decide:
- Test asserts manifest-path-specific behavior (regex on file path, etc.) → delete the test, add a note in commit message.
- Test asserts gate decision given an eligibility tuple → rewrite to stub `_resolver_is_eligible` directly.

After updates, run again:

```bash
cd /root/openclaw && pytest tests/test_regime_gate.py tests/test_regime_gate_db.py -v 2>&1 | tail -15
```

Expected: all tests pass.

- [ ] **Step 6: Parity smoke — DRY-RUN cycle produces the same signal set**

```bash
# Capture a baseline strategy list as the gate sees it now.
cd /root/openclaw && PYTHONPATH=/root/openclaw/src python3 -c "
from strategies.regime_gate import is_eligible, ALL_REGIMES
import json
m = json.load(open('src/strategies/manifest.json'))
for sid in sorted(m['strategies']):
    for r in ALL_REGIMES:
        print(f'{sid}|{r}|{is_eligible(sid, r)}')
" > /tmp/gate_after.txt
wc -l /tmp/gate_after.txt
# Compare a known case
grep momentum_12_1 /tmp/gate_after.txt
```

Expected: 392 lines. `momentum_12_1` rows: LOW_VOL=True, TRANSITIONING=True, HIGH_VOL=False, CRISIS=False (matches committed manifest).

- [ ] **Step 7: Commit**

```bash
git add src/strategies/regime_gate.py tests/test_regime_gate_db.py tests/test_regime_gate.py
git commit -m "feat(regime-gate): switch is_eligible to DB-backed resolver"
```

---

## Task 5: trade_handoff_builder switches to resolver size_scalar

**Files:**
- Modify: `src/execution/trade_handoff_builder.py:156-157`

- [ ] **Step 1: Inspect current code**

```bash
sed -n '145,170p' /root/openclaw/src/execution/trade_handoff_builder.py
```

Confirm lines 156-157 still hold the hardcoded scale dict.

- [ ] **Step 2: Replace the hardcoded dict with a resolver call**

Locate the function that produces the regime dict. The relevant lines:

```python
    scale  = {'LOW_VOL':1.0, 'TRANSITIONING':0.55,
              'HIGH_VOL':0.35, 'CRISIS':0.15}.get(state, 0.55)
```

Replace with:

```python
    # Phase 2A: resolve regime scalar via DB-backed resolver. Today the
    # resolver returns the same Phase 1 static values for every strategy
    # (NULL size_scalar → PHASE1_REGIME_SCALARS[state]). Once Phase 2B
    # populates per-strategy overrides, this same call returns the
    # strategy-specific scalar without further code change.
    #
    # NB: the handoff is constructed before per-strategy iteration in
    # this path, so we use a sentinel strategy_id = '__regime_default__'
    # to get the regime-only fallback (no row → static default).
    from execution.regime_param_resolver import size_scalar as _size_scalar
    scale  = _size_scalar('__regime_default__', state)
```

- [ ] **Step 3: Run trade_handoff_builder smoke + adjacent tests**

```bash
cd /root/openclaw && find tests -name 'test_*handoff*' -o -name 'test_*trade_handoff*' 2>&1
```

If tests exist, run them:

```bash
pytest tests/<test_file_found_above> -v 2>&1 | tail -10
```

Then sanity-check the function returns a dict including `scale` of the right value for each state:

```bash
PYTHONPATH=/root/openclaw/src python3 -c "
from execution.regime_param_resolver import size_scalar
for s in ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']:
    print(s, size_scalar('__regime_default__', s))
"
```

Expected:
```
LOW_VOL 1.0
TRANSITIONING 0.55
HIGH_VOL 0.35
CRISIS 0.15
```

- [ ] **Step 4: Commit**

```bash
git add src/execution/trade_handoff_builder.py
git commit -m "feat(handoff): regime scale via DB resolver (NULL → Phase 1 defaults)"
```

---

## Task 6: Rewrite eligibility_manager.py to write DB

**Files:**
- Modify: `src/strategies/eligibility_manager.py`
- Test: `tests/test_eligibility_manager_db.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eligibility_manager_db.py
"""Tests for eligibility_manager after DB switch. The old test file
(test_eligibility_manager.py) tested the manifest-write path; it is
being replaced by these DB-write tests."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import eligibility_manager as em  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.executed: list = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()): self.executed.append((sql, params))
    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class FakeConn:
    def __init__(self, rows=()): self.cur = FakeCursor(rows)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): self.committed = True


def test_set_params_writes_audit_then_params(monkeypatch):
    """Single transaction: audit insert THEN params upsert."""
    conn = FakeConn(rows=[('s1', 'LOW_VOL', True, None, None, None, None)])
    monkeypatch.setattr(em, '_connect', lambda: conn)
    invalidations: list = []
    monkeypatch.setattr(em, '_invalidate_cache',
                        lambda sid, r: invalidations.append((sid, r)))
    em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                  eligible=False, actor='operator:t', reason='', source='cli')
    # 3 statements: SELECT FOR UPDATE, INSERT audit, INSERT params (with ON CONFLICT)
    sql_starts = [e[0].strip().split()[0].upper() for e in conn.cur.executed]
    assert sql_starts == ['SELECT', 'INSERT', 'INSERT']
    # Audit insert must reference the audit table
    assert 'strategy_regime_param_changes' in conn.cur.executed[1][0]
    # Upsert references the params table
    assert 'strategy_regime_params' in conn.cur.executed[2][0]
    assert 'ON CONFLICT' in conn.cur.executed[2][0]
    assert invalidations == [('s1', 'LOW_VOL')]


def test_set_params_rejects_unknown_regime():
    with pytest.raises(ValueError, match='invalid regime'):
        em.set_params(strategy_id='s1', regime_state='BOGUS',
                      eligible=True, actor='t', reason='', source='cli')


def test_set_params_requires_at_least_one_field(monkeypatch):
    """At least one of eligible/size_scalar/stop_pct/target_pct/max_hold_days
    must be specified, otherwise the call is a no-op (rejected)."""
    monkeypatch.setattr(em, '_connect', lambda: FakeConn())
    with pytest.raises(ValueError, match='at least one'):
        em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                      actor='t', reason='', source='cli')


def test_set_params_merges_partial_update(monkeypatch):
    """NULL caller arg means 'keep existing'; non-None overrides."""
    existing = ('s1', 'LOW_VOL', True, 0.5, None, None, None)
    conn = FakeConn(rows=[existing])
    monkeypatch.setattr(em, '_connect', lambda: conn)
    monkeypatch.setattr(em, '_invalidate_cache', lambda sid, r: None)
    em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                  size_scalar=0.7,   # caller wants to change just this
                  actor='t', reason='', source='cli')
    # The params upsert's params tuple should contain the merged row.
    upsert_call = conn.cur.executed[2]
    # 8 params: strategy_id, regime, eligible, size, stop, target, max_hold, set_by
    assert upsert_call[1][2] is True       # eligible preserved
    assert float(upsert_call[1][3]) == 0.7  # size_scalar updated
    assert upsert_call[1][4] is None       # stop_pct preserved (was None)


def test_list_rows_queries_all():
    pass  # smoke-only: covered by integration tests / live smoke
```

- [ ] **Step 2: Run tests, expect failures**

```bash
cd /root/openclaw && pytest tests/test_eligibility_manager_db.py -v 2>&1 | tail -15
```

- [ ] **Step 3: Rewrite `eligibility_manager.py`**

Replace the entire contents of `/root/openclaw/src/strategies/eligibility_manager.py` with:

```python
#!/usr/bin/env python3
"""DB-backed operator interface for per-(strategy, regime) params.

Source of truth: strategy_regime_params table. Every write goes through
set_params() which:
  1. Validates regime + ensures ≥1 field specified
  2. Opens a transaction with SELECT ... FOR UPDATE on the existing row
  3. Inserts an audit row to strategy_regime_param_changes
  4. Upserts the params row
  5. Commits, then invalidates the resolver cache

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

# Match the resolver's path resolution.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _invalidate_cache(strategy_id: str, regime_state: str) -> None:
    """Best-effort: invalidate the same-process resolver cache. Cross-process
    caches expire on TTL (30s)."""
    try:
        from execution.regime_param_resolver import invalidate as _inv
        _inv(strategy_id, regime_state)
    except Exception as exc:
        logger.debug('cache invalidate skipped: %s', exc)


def _row_to_json(row) -> Optional[str]:
    if row is None:
        return None
    # Row is a 7-tuple from the SELECT below.
    keys = ('strategy_id', 'regime_state', 'eligible',
            'size_scalar', 'stop_pct', 'target_pct', 'max_hold_days')
    d = dict(zip(keys, row))
    # NUMERIC types from psycopg2 are Decimal; jsonify by str.
    for k in ('size_scalar', 'stop_pct', 'target_pct'):
        if d[k] is not None:
            d[k] = float(d[k])
    return json.dumps(d)


def set_params(*,
               strategy_id: str,
               regime_state: str,
               eligible: Optional[bool] = None,
               size_scalar: Optional[float] = None,
               stop_pct: Optional[float] = None,
               target_pct: Optional[float] = None,
               max_hold_days: Optional[int] = None,
               actor: str,
               reason: str = '',
               source: str = 'cli') -> dict:
    """Upsert one (strategy, regime) row. None args mean 'keep existing'.

    Raises:
        ValueError: invalid regime or no fields specified.
    """
    if regime_state not in CANONICAL_REGIMES:
        raise ValueError(f'invalid regime {regime_state!r}; must be one of {CANONICAL_REGIMES}')

    if all(v is None for v in (eligible, size_scalar, stop_pct,
                                target_pct, max_hold_days)):
        raise ValueError('at least one of eligible/size_scalar/stop_pct/'
                         'target_pct/max_hold_days must be specified')

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, eligible,
                       size_scalar, stop_pct, target_pct, max_hold_days
                  FROM strategy_regime_params
                 WHERE strategy_id = %s AND regime_state = %s
                 FOR UPDATE
            """, (strategy_id, regime_state))
            before = cur.fetchone()

            # Merge: caller's non-None overrides; missing rows assume eligible=True.
            if before is None:
                merged_eligible      = True if eligible is None else eligible
                merged_size_scalar   = size_scalar
                merged_stop_pct      = stop_pct
                merged_target_pct    = target_pct
                merged_max_hold_days = max_hold_days
            else:
                # before = (sid, regime, eligible, size, stop, target, max_hold)
                merged_eligible      = before[2] if eligible is None else eligible
                merged_size_scalar   = before[3] if size_scalar is None else size_scalar
                merged_stop_pct      = before[4] if stop_pct is None else stop_pct
                merged_target_pct    = before[5] if target_pct is None else target_pct
                merged_max_hold_days = before[6] if max_hold_days is None else max_hold_days

            after_row = (strategy_id, regime_state, merged_eligible,
                         merged_size_scalar, merged_stop_pct,
                         merged_target_pct, merged_max_hold_days)

            cur.execute("""
                INSERT INTO strategy_regime_param_changes
                    (actor, strategy_id, regime_state,
                     before_row, after_row, reason, source)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
            """, (actor, strategy_id, regime_state,
                  _row_to_json(before), _row_to_json(after_row),
                  reason, source))

            cur.execute("""
                INSERT INTO strategy_regime_params
                    (strategy_id, regime_state, eligible,
                     size_scalar, stop_pct, target_pct, max_hold_days, set_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (strategy_id, regime_state) DO UPDATE
                   SET eligible      = EXCLUDED.eligible,
                       size_scalar   = EXCLUDED.size_scalar,
                       stop_pct      = EXCLUDED.stop_pct,
                       target_pct    = EXCLUDED.target_pct,
                       max_hold_days = EXCLUDED.max_hold_days,
                       set_at        = NOW(),
                       set_by        = EXCLUDED.set_by
            """, (strategy_id, regime_state, merged_eligible,
                  merged_size_scalar, merged_stop_pct, merged_target_pct,
                  merged_max_hold_days, actor))
        conn.commit()

    _invalidate_cache(strategy_id, regime_state)

    return {
        'strategy_id':  strategy_id,
        'regime_state': regime_state,
        'before':       None if before is None else dict(zip(
            ('strategy_id', 'regime_state', 'eligible', 'size_scalar',
             'stop_pct', 'target_pct', 'max_hold_days'), before)),
        'after':        dict(zip(
            ('strategy_id', 'regime_state', 'eligible', 'size_scalar',
             'stop_pct', 'target_pct', 'max_hold_days'), after_row)),
        'audited_at':   datetime.now(timezone.utc).isoformat(),
    }


def list_rows() -> list[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, regime_state, eligible,
                       size_scalar, stop_pct, target_pct, max_hold_days
                  FROM strategy_regime_params
                 ORDER BY strategy_id, regime_state
            """)
            cols = ('strategy_id', 'regime_state', 'eligible',
                    'size_scalar', 'stop_pct', 'target_pct', 'max_hold_days')
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def recent_audit(limit: int = 25) -> list[dict]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT changed_at, actor, strategy_id, regime_state,
                       before_row, after_row, reason, source
                  FROM strategy_regime_param_changes
                 ORDER BY changed_at DESC
                 LIMIT %s
            """, (limit,))
            cols = ('changed_at', 'actor', 'strategy_id', 'regime_state',
                    'before_row', 'after_row', 'reason', 'source')
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument('--list', action='store_true')
    sub.add_argument('--set', nargs=2, metavar=('STRATEGY', 'REGIME'))
    sub.add_argument('--audit', action='store_true')
    p.add_argument('--eligible', dest='eligible_flag',
                    action='store_const', const=True, default=None)
    p.add_argument('--ineligible', dest='eligible_flag',
                    action='store_const', const=False)
    p.add_argument('--size', type=float, default=None)
    p.add_argument('--stop', type=float, default=None)
    p.add_argument('--target', type=float, default=None)
    p.add_argument('--max-hold', type=int, default=None)
    p.add_argument('--actor', default='cli')
    p.add_argument('--reason', default='')
    p.add_argument('--source', default='cli')
    p.add_argument('--limit', type=int, default=25)
    args = p.parse_args()

    if args.list:
        for r in list_rows():
            print(f"{r['strategy_id']:<40} {r['regime_state']:<14} "
                  f"eligible={r['eligible']!s:<5} "
                  f"size={r['size_scalar']} stop={r['stop_pct']} "
                  f"target={r['target_pct']} maxhold={r['max_hold_days']}")
        return 0
    if args.audit:
        for r in recent_audit(limit=args.limit):
            print(f"{r['changed_at']} {r['actor']:>16}  "
                  f"{r['strategy_id']}/{r['regime_state']}  "
                  f"src={r['source']} reason={r['reason'] or ''}")
        return 0
    if args.set:
        strategy, regime = args.set
        result = set_params(strategy_id=strategy, regime_state=regime,
                             eligible=args.eligible_flag,
                             size_scalar=args.size,
                             stop_pct=args.stop,
                             target_pct=args.target,
                             max_hold_days=args.max_hold,
                             actor=args.actor, reason=args.reason,
                             source=args.source)
        print(json.dumps(result, indent=2, default=str))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run the new tests**

```bash
cd /root/openclaw && pytest tests/test_eligibility_manager_db.py -v 2>&1 | tail -10
```

Expected: 4 passing (the `test_list_rows_queries_all` smoke is intentionally a no-op assertion — covered by live smoke in step 5).

- [ ] **Step 5: Delete the old manifest-write tests**

```bash
rm /root/openclaw/tests/test_eligibility_manager.py
```

Those tests covered atomic file write + audit-on-disk, which the new code intentionally replaces with DB transaction semantics.

- [ ] **Step 6: Live CLI smoke**

```bash
# List should print 392 rows (98 strategies × 4 regimes).
cd /root/openclaw && PYTHONPATH=/root/openclaw/src python3 \
    -m strategies.eligibility_manager --list 2>&1 | wc -l
```

Expected: 392.

```bash
# Toggle then revert to validate the round-trip.
PYTHONPATH=/root/openclaw/src python3 -m strategies.eligibility_manager \
    --set momentum_12_1 CRISIS --eligible \
    --actor 'operator:smoke' --reason 'plan smoke step 6' --source cli
PYTHONPATH=/root/openclaw/src python3 -m strategies.eligibility_manager \
    --set momentum_12_1 CRISIS --ineligible \
    --actor 'operator:smoke' --reason 'plan smoke step 6 rollback' --source cli
PYTHONPATH=/root/openclaw/src python3 -m strategies.eligibility_manager \
    --audit --limit 5
```

Expected: 2 new audit rows for `momentum_12_1/CRISIS`; final state has `eligible=False`.

- [ ] **Step 7: Commit**

```bash
git add src/strategies/eligibility_manager.py tests/test_eligibility_manager_db.py
git rm tests/test_eligibility_manager.py
git commit -m "feat(eligibility-manager): DB-backed write path; drop manifest path"
```

---

## Task 7: API — new POST /api/regime-params + back-compat shim

**Files:**
- Create: `src/channels/api/routes_regime_params.js`
- Modify: `src/channels/api/routes_regime_eligibility.js` — shim to call new endpoint internally
- Modify: `src/channels/api/server.js` — mount the new router

- [ ] **Step 1: Write the new router**

```javascript
// src/channels/api/routes_regime_params.js
'use strict';

/**
 * /api/regime-params/* — operator surface for per-(strategy, regime) params.
 *
 * GET  /                              list all rows
 * POST /:strategy/:regime             upsert one row
 * GET  /audit?limit=N                 recent audit rows
 *
 * All writes shell out to the Python eligibility_manager so the audit-then-
 * upsert transaction + cache invalidation stays in one canonical place.
 */
const express = require('express');
const path = require('path');
const { spawn } = require('child_process');
const { query } = require('../../database/postgres');

const router = express.Router();

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const PY_BIN = process.env.PYTHON_BIN || '/usr/bin/python3';
const PY_ENV = {
  ...process.env,
  PYTHONPATH: process.env.PYTHONPATH
    ? `${path.join(REPO_ROOT, 'src')}:${process.env.PYTHONPATH}`
    : path.join(REPO_ROOT, 'src'),
};

const REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

function runPython(args, { timeoutMs = 15_000 } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PY_BIN, args, { cwd: REPO_ROOT, env: PY_ENV });
    let stdout = '', stderr = '';
    const t = setTimeout(() => {
      try { proc.kill('SIGKILL'); } catch (_) {}
      reject(new Error(`python timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    proc.stdout.on('data', (c) => { stdout += c; });
    proc.stderr.on('data', (c) => { stderr += c; });
    proc.on('error', (e) => { clearTimeout(t); reject(e); });
    proc.on('close', (code) => {
      clearTimeout(t);
      if (code === 0) resolve(stdout);
      else reject(new Error(`python exit ${code}: ${stderr || stdout}`));
    });
  });
}

router.get('/', async (req, res) => {
  try {
    const result = await query(`
      SELECT strategy_id, regime_state, eligible,
             size_scalar::float AS size_scalar,
             stop_pct::float    AS stop_pct,
             target_pct::float  AS target_pct,
             max_hold_days
        FROM strategy_regime_params
       ORDER BY strategy_id, regime_state
    `);
    res.json({ rows: result.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/:strategy/:regime', async (req, res) => {
  const { strategy, regime } = req.params;
  const body = req.body || {};
  if (!REGIMES.includes(regime)) {
    return res.status(400).json({ error: `invalid regime: ${regime}` });
  }
  if (!/^[A-Za-z0-9_:.-]+$/.test(strategy)) {
    return res.status(400).json({ error: 'invalid strategy id' });
  }
  if (typeof body.actor !== 'string' || !body.actor.trim()) {
    return res.status(400).json({ error: 'actor required' });
  }
  const args = ['-m', 'strategies.eligibility_manager',
                '--set', strategy, regime,
                '--actor', body.actor,
                '--reason', body.reason || '',
                '--source', body.source || 'dashboard'];
  if (body.eligible === true)  args.push('--eligible');
  if (body.eligible === false) args.push('--ineligible');
  if (typeof body.size_scalar   === 'number') args.push('--size', String(body.size_scalar));
  if (typeof body.stop_pct      === 'number') args.push('--stop', String(body.stop_pct));
  if (typeof body.target_pct    === 'number') args.push('--target', String(body.target_pct));
  if (typeof body.max_hold_days === 'number') args.push('--max-hold', String(body.max_hold_days));

  try {
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (err) {
    const msg = err.message || String(err);
    if (/ValueError/.test(msg)) {
      const m = msg.match(/ValueError:\s*([^\n]+)/);
      return res.status(400).json({ error: m ? m[1].trim() : 'invalid input' });
    }
    res.status(500).json({ error: msg });
  }
});

router.get('/audit', async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit, 10) || 50, 500);
  try {
    const result = await query(`
      SELECT changed_at, actor, strategy_id, regime_state,
             before_row, after_row, reason, source
        FROM strategy_regime_param_changes
       ORDER BY changed_at DESC
       LIMIT $1
    `, [limit]);
    res.json(result.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
```

- [ ] **Step 2: Mount the new router in server.js**

Find the existing line (added in commit `01582c8`):

```javascript
app.use('/api/regime-eligibility', require('./routes_regime_eligibility'));
```

Add immediately after it:

```javascript
// Phase 2A — per-(strategy, regime) parameter table
app.use('/api/regime-params', require('./routes_regime_params'));
```

- [ ] **Step 3: Rewrite the back-compat shim**

Replace the contents of `src/channels/api/routes_regime_eligibility.js` with a thin shim that forwards each regime in the old `{regimes: [...]}` body to the new param endpoint:

```javascript
'use strict';

/**
 * Back-compat shim for Phase 1 /api/regime-eligibility/* clients. Translates
 * the old "set eligible_regimes to this list" semantics into a series of
 * per-regime upserts on strategy_regime_params via the new endpoint logic.
 *
 * Keeps old callers (CLI scripts, dashboard cells from before the Phase 2A
 * rewrite if any are still cached) working without code changes.
 */
const express = require('express');
const path = require('path');
const { spawn } = require('child_process');
const { query } = require('../../database/postgres');

const router = express.Router();

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const PY_BIN = process.env.PYTHON_BIN || '/usr/bin/python3';
const PY_ENV = {
  ...process.env,
  PYTHONPATH: process.env.PYTHONPATH
    ? `${path.join(REPO_ROOT, 'src')}:${process.env.PYTHONPATH}`
    : path.join(REPO_ROOT, 'src'),
};
const REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

function runPython(args, { timeoutMs = 15_000 } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PY_BIN, args, { cwd: REPO_ROOT, env: PY_ENV });
    let stdout = '', stderr = '';
    const t = setTimeout(() => { try { proc.kill('SIGKILL'); } catch (_) {} reject(new Error(`python timeout after ${timeoutMs}ms`)); }, timeoutMs);
    proc.stdout.on('data', (c) => { stdout += c; });
    proc.stderr.on('data', (c) => { stderr += c; });
    proc.on('error', (e) => { clearTimeout(t); reject(e); });
    proc.on('close', (code) => { clearTimeout(t); code === 0 ? resolve(stdout) : reject(new Error(`python exit ${code}: ${stderr || stdout}`)); });
  });
}

router.get('/', async (req, res) => {
  // Return a /api/regime-eligibility-shaped payload synthesized from the DB.
  try {
    // Load manifest for strategy metadata + Phase 1 metrics-side merge.
    const fs = require('fs');
    const mfPath = path.join(REPO_ROOT, 'src/strategies/manifest.json');
    const manifest = JSON.parse(fs.readFileSync(mfPath, 'utf8'));
    const result = await query(`
      SELECT strategy_id, regime_state, eligible
        FROM strategy_regime_params
    `);
    const byStrat = {};
    for (const row of result.rows) {
      byStrat[row.strategy_id] = byStrat[row.strategy_id] || new Set();
      if (row.eligible) byStrat[row.strategy_id].add(row.regime_state);
    }
    const strategies = Object.entries(manifest.strategies || {}).map(([sid]) => ({
      strategy_id:      sid,
      eligible_regimes: byStrat[sid] ? [...byStrat[sid]].filter(r => REGIMES.includes(r))
                                                       .sort((a, b) => REGIMES.indexOf(a) - REGIMES.indexOf(b))
                                     : null,
      metrics: {},   // Phase 1 metrics surface; populated by the live-pnl rollup endpoint if needed
    }));
    res.json({ strategies });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/:strategy', express.json(), async (req, res) => {
  const { strategy } = req.params;
  const { regimes, actor, reason, source } = req.body || {};
  if (!Array.isArray(regimes) || regimes.length === 0) {
    return res.status(400).json({ error: 'regimes must be a non-empty array' });
  }
  if (typeof actor !== 'string' || !actor.trim()) {
    return res.status(400).json({ error: 'actor required' });
  }
  if (!/^[A-Za-z0-9_:.-]+$/.test(strategy)) {
    return res.status(400).json({ error: 'invalid strategy id' });
  }
  for (const r of regimes) {
    if (!REGIMES.includes(r)) return res.status(400).json({ error: `invalid regime: ${r}` });
  }
  // Translate to per-regime upserts: regimes in `regimes` → eligible=true,
  // regimes NOT in `regimes` → eligible=false.
  const errors = [];
  for (const regime of REGIMES) {
    const eligibleFlag = regimes.includes(regime) ? '--eligible' : '--ineligible';
    const args = ['-m', 'strategies.eligibility_manager',
                  '--set', strategy, regime, eligibleFlag,
                  '--actor', actor, '--reason', reason || '',
                  '--source', source || 'shim'];
    try { await runPython(args); }
    catch (err) {
      const msg = err.message || String(err);
      if (/KeyError/.test(msg)) return res.status(404).json({ error: 'unknown strategy' });
      if (/ValueError/.test(msg)) {
        const m = msg.match(/ValueError:\s*([^\n]+)/);
        return res.status(400).json({ error: m ? m[1].trim() : 'invalid input' });
      }
      errors.push(msg);
    }
  }
  if (errors.length) return res.status(500).json({ error: errors.join('; ') });
  res.json({
    strategy_id:    strategy,
    after_regimes:  regimes,
    note: 'Phase 1 shim wrote to DB via /api/regime-params; ' +
          'old /api/regime-eligibility surface preserved.',
  });
});

router.get('/audit', async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit, 10) || 50, 500);
  try {
    const result = await query(`
      SELECT changed_at, actor, strategy_id,
             before_row->'eligible' AS before_eligible,
             after_row->'eligible'  AS after_eligible,
             regime_state, reason, source
        FROM strategy_regime_param_changes
       ORDER BY changed_at DESC
       LIMIT $1
    `, [limit]);
    res.json(result.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
```

- [ ] **Step 4: Restart johnbot, smoke both surfaces**

```bash
systemctl restart johnbot.service && sleep 3 && journalctl -u johnbot.service \
    --since "10s ago" --no-pager | grep -iE "error|exception" | head -5
echo "---NEW API---"
curl -s http://localhost:3000/api/regime-params | python3 -c \
    "import sys, json; d = json.load(sys.stdin); print('rows:', len(d['rows']))"
echo "---SHIM API---"
curl -s http://localhost:3000/api/regime-eligibility | python3 -c \
    "import sys, json; d = json.load(sys.stdin); print('strategies:', len(d['strategies']))"
```

Expected: `rows: 392`, `strategies: 98`, no errors in journal.

- [ ] **Step 5: Smoke a POST via each surface**

```bash
# New surface
curl -s -X POST http://localhost:3000/api/regime-params/momentum_12_1/CRISIS \
    -H 'Content-Type: application/json' \
    -d '{"eligible":true,"actor":"operator:plan-smoke","reason":"task 7 smoke"}' | head -1
# Revert via the new surface
curl -s -X POST http://localhost:3000/api/regime-params/momentum_12_1/CRISIS \
    -H 'Content-Type: application/json' \
    -d '{"eligible":false,"actor":"operator:plan-smoke","reason":"task 7 rollback"}' | head -1
# Verify shim list reflects state
curl -s http://localhost:3000/api/regime-eligibility | python3 -c "
import sys, json
d = json.load(sys.stdin)
m = [s for s in d['strategies'] if s['strategy_id']=='momentum_12_1'][0]
print('momentum_12_1 eligible:', m['eligible_regimes'])"
```

Expected: 2 successful POSTs; final list shows `['LOW_VOL', 'TRANSITIONING']`.

- [ ] **Step 6: Commit**

```bash
git add src/channels/api/routes_regime_params.js \
        src/channels/api/routes_regime_eligibility.js \
        src/channels/api/server.js
git commit -m "feat(api): /api/regime-params; back-compat shim on /api/regime-eligibility"
```

---

## Task 8: Dashboard regime cells write to new endpoint

**Files:**
- Modify: `src/channels/api/server.js` — the `_stToggleRegimeEligibility` JS function inside the dashboard HTML template

- [ ] **Step 1: Locate `_stToggleRegimeEligibility` in server.js**

```bash
grep -n "_stToggleRegimeEligibility\|/api/regime-eligibility" /root/openclaw/src/channels/api/server.js | head -10
```

The function POSTs to `/api/regime-eligibility/:strategy` with a `{regimes: [...]}` body. The back-compat shim from Task 7 means the dashboard keeps working without changes, but updating it to call `/api/regime-params/:strategy/:regime` is cleaner and removes a shim hop.

- [ ] **Step 2: Edit the toggle to call the new endpoint directly**

In `_stToggleRegimeEligibility`, replace the `fetch` call:

```javascript
    const res = await fetch('/api/regime-eligibility/' + encodeURIComponent(sid), {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        regimes: [...next],
        actor:   'operator:' + actor,
        reason,
        source:  'strategies-page',
      }),
    });
```

with:

```javascript
    // Phase 2A: POST to per-regime endpoint instead of the legacy list-set endpoint.
    // We need to flip just the toggled regime; `next` is the full new eligibility
    // set, so derive whether the clicked `regime` ended up in or out of it.
    const eligibleAfter = next.has(regime);
    const res = await fetch(
      '/api/regime-params/' + encodeURIComponent(sid) + '/' + encodeURIComponent(regime),
      {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          eligible: eligibleAfter,
          actor:    'operator:' + actor,
          reason,
          source:   'strategies-page',
        }),
      },
    );
```

- [ ] **Step 3: Restart, parse-check, smoke**

```bash
systemctl restart johnbot.service && sleep 3
# Parse-check the served dashboard JS
curl -s http://localhost:3000/ > /tmp/dash.html
python3 -c "
import re
html=open('/tmp/dash.html').read()
biggest=max(re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>', html, re.DOTALL), key=len)
open('/tmp/dash.js','w').write(biggest)
" && node --check /tmp/dash.js 2>&1 | head -3 && echo "(silent = parse OK)"
```

Expected: silent parse OK.

- [ ] **Step 4: End-to-end live toggle**

```bash
# Mimic the UI click via the new endpoint
curl -s -X POST http://localhost:3000/api/regime-params/momentum_12_1/CRISIS \
    -H 'Content-Type: application/json' \
    -d '{"eligible":true,"actor":"operator:plan-task8","reason":"task 8 e2e","source":"strategies-page"}'
# Verify gate immediately reflects (cache invalidate happened)
PYTHONPATH=/root/openclaw/src python3 -c "
from strategies.regime_gate import is_eligible
print('momentum_12_1 in CRISIS:', is_eligible('momentum_12_1', 'CRISIS'))"
# Revert
curl -s -X POST http://localhost:3000/api/regime-params/momentum_12_1/CRISIS \
    -H 'Content-Type: application/json' \
    -d '{"eligible":false,"actor":"operator:plan-task8","reason":"task 8 rollback","source":"strategies-page"}'
PYTHONPATH=/root/openclaw/src python3 -c "
from strategies.regime_gate import is_eligible
print('momentum_12_1 in CRISIS:', is_eligible('momentum_12_1', 'CRISIS'))"
```

Expected: True after first POST, False after rollback. Note: across process boundaries cache TTL is 30s, but within one Python invocation the value is fresh.

- [ ] **Step 5: Commit**

```bash
git add src/channels/api/server.js
git commit -m "feat(dashboard): regime cell toggle uses /api/regime-params directly"
```

---

## Task 9: Doctor — `strategy_regime_params_consistency`

**Files:**
- Modify: `src/maintenance/doctor.py`
- Test: `tests/test_doctor_strategy_regime_params_consistency.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_doctor_strategy_regime_params_consistency.py
"""Tests for doctor.check_strategy_regime_params_consistency."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub(monkeypatch, registry_ids, param_rows, raise_=None):
    """registry_ids: set of strategy_ids in strategy_registry.
    param_rows: list of (strategy_id, regime_state) tuples in strategy_regime_params.
    """
    def fake_query(sql, params=()):
        if raise_:
            raise raise_
        if 'strategy_registry' in sql:
            return list(registry_ids)
        if 'strategy_regime_params' in sql:
            return list(param_rows)
        return []
    monkeypatch.setattr(doc, '_query_consistency', fake_query)


REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def test_full_grid_returns_pass(monkeypatch):
    sids = {'s1', 's2'}
    rows = [(s, r) for s in sids for r in REGIMES]
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.PASS


def test_few_missing_returns_warn(monkeypatch):
    sids = {'s1', 's2'}
    rows = [(s, r) for s in sids for r in REGIMES if not (s == 's1' and r == 'CRISIS')]
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.WARN
    assert '1 missing' in r['detail'] or 's1' in r['detail']


def test_many_missing_returns_fail(monkeypatch):
    sids = {'s1', 's2', 's3', 's4'}
    rows = [('s1', 'LOW_VOL')]  # only one of 16 expected
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.FAIL


def test_orphan_params_row_returns_warn(monkeypatch):
    sids = {'s1'}
    rows = [(s, r) for s in sids for r in REGIMES] + [('orphan', 'LOW_VOL')]
    _stub(monkeypatch, sids, rows)
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.WARN
    assert 'orphan' in r['detail'].lower()


def test_db_error_returns_warn(monkeypatch):
    _stub(monkeypatch, set(), [], raise_=RuntimeError('db down'))
    r = doc.check_strategy_regime_params_consistency()
    assert r['severity'] == doc.WARN
```

- [ ] **Step 2: Run tests, expect AttributeError**

```bash
cd /root/openclaw && pytest tests/test_doctor_strategy_regime_params_consistency.py -v 2>&1 | tail -10
```

- [ ] **Step 3: Add the check to doctor.py**

Append to `src/maintenance/doctor.py` near the other regime checks (right after `check_regime_live_rollup_freshness`):

```python
PARAMS_CONSISTENCY_FAIL_THRESHOLD = 5


def _query_consistency(sql: str, params: tuple = ()):
    """Indirection seam so tests can stub registry + params queries."""
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI')
           or 'postgresql://openclaw:password@localhost:5432/openclaw')
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


@_check('strategy_regime_params_consistency')
def check_strategy_regime_params_consistency():
    """Each strategy in strategy_registry × each of 4 canonical regimes must
    have exactly one row in strategy_regime_params. Catches partial migrations
    + orphans (rows pointing at strategies that no longer exist)."""
    name = 'strategy_regime_params_consistency'
    REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
    try:
        registry_rows = _query_consistency('SELECT id FROM strategy_registry')
        registry_ids = {r[0] for r in registry_rows} if registry_rows else set()
        # Fall back to manifest if strategy_registry is empty (early-bootstrap envs).
        if not registry_ids:
            import json
            mf = json.loads((ROOT / 'src' / 'strategies' / 'manifest.json').read_text(encoding='utf-8'))
            registry_ids = set(mf.get('strategies') or {})
        param_rows = _query_consistency(
            'SELECT strategy_id, regime_state FROM strategy_regime_params')
        param_set = {(r[0], r[1]) for r in param_rows}
    except Exception as exc:
        return _warn(name, f'query failed: {exc!s}')
    expected = {(sid, r) for sid in registry_ids for r in REGIMES}
    missing  = expected - param_set
    orphans  = {sid for sid, _ in param_set} - registry_ids
    if not missing and not orphans:
        return _ok(name,
                   f'{len(registry_ids)} strategies × 4 regimes = {len(expected)} rows, all present')
    parts = []
    if missing:
        first = next(iter(missing))
        parts.append(f'{len(missing)} missing (e.g. {first[0]}/{first[1]})')
    if orphans:
        parts.append(f'{len(orphans)} orphan row(s): {", ".join(list(orphans)[:3])}'
                     + (' …' if len(orphans) > 3 else ''))
    detail = '; '.join(parts)
    if len(missing) >= PARAMS_CONSISTENCY_FAIL_THRESHOLD:
        return _fail(name, detail)
    return _warn(name, detail)
```

- [ ] **Step 4: Run the new tests**

```bash
cd /root/openclaw && pytest tests/test_doctor_strategy_regime_params_consistency.py -v 2>&1 | tail -10
```

Expected: 5 passed.

- [ ] **Step 5: Live smoke**

```bash
PYTHONPATH=/root/openclaw/src python3 -m maintenance.doctor 2>&1 | \
    grep strategy_regime_params_consistency
```

Expected: PASS (98 strategies × 4 = 392 rows, all present).

- [ ] **Step 6: Commit**

```bash
git add src/maintenance/doctor.py tests/test_doctor_strategy_regime_params_consistency.py
git commit -m "feat(doctor): strategy_regime_params_consistency preflight check"
```

---

## Task 10: Doctor — repurpose `manifest_eligibility_drift`

**Files:**
- Modify: `src/maintenance/doctor.py` — `check_manifest_eligibility_drift`
- Modify: `tests/test_doctor_regime_live_metrics.py` — adjust drift tests for new semantics

- [ ] **Step 1: Replace the existing check_manifest_eligibility_drift body**

The check previously compared per-strategy eligible_regimes between HEAD and the working tree. Its new job is to warn whenever **any** strategy in the working tree manifest *still has* an `eligible_regimes` field — that field is now deprecated and indicates a stale writer.

Replace the body of `check_manifest_eligibility_drift` in `src/maintenance/doctor.py` with:

```python
@_check('manifest_eligibility_drift')
def check_manifest_eligibility_drift():
    """Detect stale writes to the deprecated manifest.eligible_regimes field.

    Phase 2A moved eligibility ownership into strategy_regime_params (DB).
    `manifest.eligible_regimes` is no longer authoritative; its presence on
    any strategy entry indicates either: (a) a stale code path still
    writing it, or (b) a transitional row that hasn't been cleaned up
    yet. Either is worth flagging until manifest is fully retired.

    PASS: no strategy has the field
    WARN: 1+ strategies have the field (DRY-RUN)
    FAIL: 1+ AND OPENCLAW_REGIME_BLENDED_LIVE=1 (LIVE-mode operator visibility)
    WARN: manifest unparseable / unreadable
    """
    name = 'manifest_eligibility_drift'
    repo_root = os.environ.get('OPENCLAW_REPO_ROOT', MANIFEST_REPO_ROOT_DEFAULT)
    live = os.environ.get('OPENCLAW_REGIME_BLENDED_LIVE') == '1'
    wrk_path = Path(repo_root) / 'src' / 'strategies' / 'manifest.json'
    try:
        wrk = json.loads(wrk_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        return _warn(name, f'working manifest unreadable: {exc!s}')
    strategies = wrk.get('strategies') or {}
    stale = [sid for sid, rec in strategies.items()
             if rec.get('eligible_regimes') is not None]
    if not stale:
        return _ok(name, 'no strategy carries deprecated eligible_regimes field')
    stale.sort()
    preview = ', '.join(stale[:4]) + (f' +{len(stale)-4} more' if len(stale) > 4 else '')
    summary = (f'{len(stale)} strategy(ies) still carry deprecated '
               f'eligible_regimes field: {preview}')
    if live:
        return _fail(name, summary)
    return _warn(name, summary)
```

- [ ] **Step 2: Rewrite the manifest drift tests**

The previous tests stubbed `git show HEAD:...` to test HEAD↔working comparison. New semantics only look at the working manifest. Replace the drift section of `tests/test_doctor_regime_live_metrics.py`:

```python
# Replace the manifest-drift section near the bottom of the file with:

def _write_manifest(tmp_path, payload):
    repo = tmp_path / 'repo'
    (repo / 'src' / 'strategies').mkdir(parents=True)
    (repo / 'src' / 'strategies' / 'manifest.json').write_text(
        json.dumps(payload), encoding='utf-8')
    return repo


def test_manifest_no_deprecated_field_returns_pass(monkeypatch, tmp_path):
    repo = _write_manifest(tmp_path, {'strategies': {
        's1': {'state': 'live', 'metadata': {}},
    }})
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.PASS


def test_manifest_with_deprecated_field_returns_warn(monkeypatch, tmp_path):
    repo = _write_manifest(tmp_path, {'strategies': {
        's1': {'state': 'live', 'eligible_regimes': ['LOW_VOL']},
    }})
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    monkeypatch.delenv('OPENCLAW_REGIME_BLENDED_LIVE', raising=False)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 's1' in r['detail']


def test_manifest_with_deprecated_field_in_live_returns_fail(monkeypatch, tmp_path):
    repo = _write_manifest(tmp_path, {'strategies': {
        's1': {'state': 'live', 'eligible_regimes': ['LOW_VOL']},
    }})
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.FAIL


def test_unparseable_manifest_returns_warn(monkeypatch, tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'src' / 'strategies').mkdir(parents=True)
    (repo / 'src' / 'strategies' / 'manifest.json').write_text('not json{',
                                                                  encoding='utf-8')
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(repo))
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN


def test_missing_working_manifest_returns_warn(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENCLAW_REPO_ROOT', str(tmp_path))
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'unreadable' in r['detail'].lower()
```

Delete the older tests that tested `git show` interactions for this check (they referenced the now-removed comparison logic).

- [ ] **Step 3: Run tests**

```bash
cd /root/openclaw && pytest tests/test_doctor_regime_live_metrics.py -v 2>&1 | tail -15
```

Expected: all tests pass.

- [ ] **Step 4: Live smoke**

```bash
PYTHONPATH=/root/openclaw/src python3 -m maintenance.doctor 2>&1 | grep manifest_eligibility_drift
```

Expected: **WARN** with N=5 (the 5 momentum strategies' manifest entries still carry the deprecated `eligible_regimes` field — they're committed but the field is now the deprecation flag). After follow-up spec strips the field, this PASSes.

- [ ] **Step 5: Commit**

```bash
git add src/maintenance/doctor.py tests/test_doctor_regime_live_metrics.py
git commit -m "feat(doctor): manifest_eligibility_drift repurposed to flag deprecated field"
```

---

## Task 11: End-to-end integration smoke

**Files:** none (read-only verification)

- [ ] **Step 1: Toggle eligibility, run signal step, verify zero output**

```bash
# Pick a candidate strategy that already runs in TRANSITIONING.
SID="S_intl_momentum_attention_regime"

# Make it ineligible in TRANSITIONING.
cd /root/openclaw && PYTHONPATH=/root/openclaw/src python3 \
    -m strategies.eligibility_manager --set ${SID} TRANSITIONING --ineligible \
    --actor 'operator:plan-task11' --reason 'integration smoke'

# Confirm via the resolver
PYTHONPATH=/root/openclaw/src python3 -c "
from strategies.regime_gate import is_eligible
print('${SID} in TRANSITIONING:', is_eligible('${SID}', 'TRANSITIONING'))"
```

Expected: `False`.

- [ ] **Step 2: Re-enable and confirm**

```bash
SID="S_intl_momentum_attention_regime"
cd /root/openclaw && PYTHONPATH=/root/openclaw/src python3 \
    -m strategies.eligibility_manager --set ${SID} TRANSITIONING --eligible \
    --actor 'operator:plan-task11' --reason 'integration smoke rollback'
PYTHONPATH=/root/openclaw/src python3 -c "
from strategies.regime_gate import is_eligible
print('${SID} in TRANSITIONING:', is_eligible('${SID}', 'TRANSITIONING'))"
```

Expected: `True`.

- [ ] **Step 3: Full doctor pass**

```bash
PYTHONPATH=/root/openclaw/src python3 -m maintenance.doctor 2>&1 | tail -25
```

Expected: 19 checks total (18 from Phase 1 + 1 new `strategy_regime_params_consistency`); `strategy_regime_params_consistency` PASSes; `manifest_eligibility_drift` may WARN (depending on whether the 5 momentum strategies still have the deprecated field on disk).

- [ ] **Step 4: Full test suite for this plan**

```bash
cd /root/openclaw && pytest \
    tests/test_regime_param_resolver.py \
    tests/test_seed_strategy_regime_params.py \
    tests/test_regime_gate_db.py \
    tests/test_regime_gate.py \
    tests/test_eligibility_manager_db.py \
    tests/test_doctor_strategy_regime_params_consistency.py \
    tests/test_doctor_regime_live_metrics.py \
    tests/test_lifecycle_eligible_regimes_preservation.py \
    -v 2>&1 | tail -5
```

Expected: all passing.

- [ ] **Step 5: Audit trail check**

```bash
docker exec openclaw-postgres psql -U openclaw -d openclaw -c "
    SELECT COUNT(*) FROM strategy_regime_param_changes;
    SELECT actor, COUNT(*) FROM strategy_regime_param_changes GROUP BY actor;"
```

Expected: at least 4 audit rows from the smoke tests in tasks 6, 7, 8, and 11.

---

## Task 12: Update spec + memory + runbook

**Files:**
- Modify: `docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md` — add "Implementation complete" footer
- Modify: `docs/runbooks/regime-eligibility-operator-runbook.md` — update commands to use new CLI flags
- Modify: `/root/.claude/projects/-root/memory/project_regime_blended_sizer.md` — note Phase 2A landed

- [ ] **Step 1: Append implementation-complete section to the spec**

Append to the end of the design spec:

```markdown
---

## Implementation complete — 2026-05-12

Phase 2A landed. Commits:
- Migrations 076 + 077 — schema
- regime_param_resolver — read API + cache
- Seed migration (~392 rows from current manifest)
- regime_gate.is_eligible switched to DB
- trade_handoff_builder regime scale via resolver
- eligibility_manager rewritten to DB
- /api/regime-params endpoint + back-compat shim on /api/regime-eligibility
- Dashboard regime cells call new endpoint directly
- Doctor: strategy_regime_params_consistency + repurposed manifest_eligibility_drift

Doctor: 19 checks pass; new check confirms 98 × 4 = 392 rows present.

Followups in Phase 2B (separate spec): Mastermind extension for proposals,
operator approval workflow.

Followup spec (post-2B stable): remove manifest.eligible_regimes field
entirely; lifecycle.py.StrategyRecord drops the attribute. Doctor's
`manifest_eligibility_drift` retires then.
```

- [ ] **Step 2: Update the operator runbook**

Replace the CLI section in `docs/runbooks/regime-eligibility-operator-runbook.md`:

Find the block starting with `**Via CLI:**` and replace with:

```markdown
**Via CLI:**
```bash
cd /root/openclaw && PYTHONPATH=/root/openclaw/src \
    python3 -m strategies.eligibility_manager \
    --set <strategy_id> <regime> [--eligible | --ineligible] \
    [--size <float>] [--stop <float>] [--target <float>] [--max-hold <int>] \
    --actor 'operator:<name>' --reason '<reason>'
```

The `<regime>` is one of `LOW_VOL`, `TRANSITIONING`, `HIGH_VOL`, `CRISIS`.
You can set just eligibility, just one numeric param, or any combination.
NULL values are inherited from the prior row.
```

Also append a note section:

```markdown
## Phase 2A — params now in DB (2026-05-12)

Source of truth moved from `manifest.json` to the `strategy_regime_params`
DB table. The CLI surface above writes directly to the DB; the dashboard
clicks now call `/api/regime-params/:strategy/:regime`. The
`manifest.eligible_regimes` field on disk is deprecated and will be
removed in a follow-up spec — the `manifest_eligibility_drift` doctor
check WARNs as long as any strategy still has the field on disk.
```

- [ ] **Step 3: Update memory**

Edit `/root/.claude/projects/-root/memory/project_regime_blended_sizer.md`. Replace this block:

```markdown
- Manifest commits durable: ...
- Drift currently: PASS for all 98 strategies (`6ce7360` rewrote drift check ...)
```

with:

```markdown
- Phase 2A shipped 2026-05-12: per-(strategy, regime) params now live in
  `strategy_regime_params` DB table (migration 076), audited via
  `strategy_regime_param_changes` (077). Resolver at
  `src/execution/regime_param_resolver.py` with 30s in-process cache.
  regime_gate + trade_handoff_builder switched to DB. CLI is now
  per-(strategy, regime): `python3 -m strategies.eligibility_manager
  --set <strategy> <regime> [--eligible|--ineligible] [--size N] ...`.
  Doctor adds `strategy_regime_params_consistency`; repurposes
  `manifest_eligibility_drift` to flag stale manifest writes.
  Phase 2B (Mastermind proposer) is a separate spec; manifest.eligible_regimes
  field removal is a follow-up cleanup spec.
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md \
        docs/runbooks/regime-eligibility-operator-runbook.md \
        /root/.claude/projects/-root/memory/project_regime_blended_sizer.md
git commit -m "docs: Phase 2A implementation complete — spec footer + runbook + memory"
```

---

## Self-review

**1. Spec coverage** (against spec §1-§8):
- §2 schema → Task 1 ✓
- §3 consumers + cache → Tasks 2-8 ✓ (resolver, gate, sizer, eligibility_manager, API, dashboard)
- §3 doctor checks → Tasks 9-10 ✓
- §4 manifest deprecation → Task 10 (repurposed drift check) + Task 12 (runbook note) ✓
- §5 testing — 5 test files: param_resolver (8), seed (5), gate (4), eligibility_manager_db (4), doctor consistency (5), revised drift (5) = 31 ≥ spec's 28 ✓
- §6 rollout sequence — Tasks 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 (12 tasks for 9 rollout steps; some tasks combine multiple steps; mapping is 1:1 for the behavioral switches) ✓
- §7 risks — Task 1 seed comment + Task 6 invalidate hook + Task 11 audit verify ✓

**2. Placeholder scan**: every code block contains complete code or full commands; no TBD/TODO/"similar to" references; method names consistent (`set_params`, `is_eligible`, `size_scalar`, `invalidate`, `_resolver_is_eligible`).

**3. Type consistency**:
- `regime_param_resolver.get_row` returns `Optional[dict]` (Task 2); consumers in Tasks 4, 5 check `is None` correctly.
- `eligibility_manager.set_params` keyword-only kwargs (Task 6); CLI in same task passes those exact kwargs; back-compat shim (Task 7) passes them via subprocess args.
- Cache invalidate API `_cache.invalidate((sid, regime))` is internal; public API is `invalidate(sid, regime)` — consistent.
