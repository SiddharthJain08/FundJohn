# SP-7 Phase B: Tier-Ladder Universe-Determination Backtest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the metadata tier history (B0), then run a once-per-strategy nested tier-ladder backtest (sp500 ⊂ tier_r1000 ⊂ tier_r3000 ⊂ tier_liquid) over all 67 registry-approved strategies, with deterministic selection, adoptable recommendation rows, √ln(N) threshold proposals, and sentinel/dashboard recompute triggers.

**Architecture:** Approach C (operator-locked): a one-time per-run tier-membership precompute (frozen parquet artifact) feeds a `PrecomputedResolver` (dict-lookup per bar — kills the measured 8–9 min/cell DB+parquet resolution overhead), and each (strategy, tier) cell runs as an isolated sequential subprocess managed by a resumable Postgres-backed queue inside a 01:00→13:00 UTC nightly window. Selection and rec-persist run incrementally as each strategy's cells finish.

**Tech Stack:** Python 3 (psycopg2, pandas, pytest), plain-SQL migrations, systemd user timers, Express inline routes (two distinct dashboards), redis-py, Discord webhooks.

**Spec:** `docs/superpowers/specs/2026-06-06-sp7-phase-b-tier-ladder-design.md` (operator-approved 2026-06-06)

---

## Worker context (read first, every task)

- **Repo:** `/root/openclaw`. **Branch:** create/use `feat/sp7-phase-b-tier-ladder` off the live branch `feat/sp6-phase-a-eod-open-execution` (HEAD ≈ `8b8126a`). NEVER `git reset --hard` (3 live-critical uncommitted files exist in the parent checkout: `src/pipeline/run_sentiment_step.py`, `src/strategies/manifest.json`, `src/strategies/strategy_signatures.json` — do not touch or commit them).
- **DB:** `psycopg2.connect(os.environ['POSTGRES_URI'])`. `psql` binary is NOT installed; use python or `docker exec openclaw-postgres psql -U openclaw -d openclaw`. Redis: `redis.from_url(os.environ.get('REDIS_URL','redis://localhost:6379'), decode_responses=True)`; `redis-cli` NOT installed.
- **Tests:** `python3 -m pytest tests/<file> -v` (pytest.ini sets `pythonpath = src`). conftest.py auto-restores os.environ per test — set env INSIDE tests via `unittest.mock.patch.dict`.
- **Migrations:** plain SQL, NO BEGIN/COMMIT, idempotent (`IF NOT EXISTS`), leading `--` comment block. Runner `npm run db:migrate` re-runs EVERY file and only swallows "already exists" — non-idempotent DML will double-apply. Numbers 131/132 verified unused at plan time; **grep `src/database/migrations/` to re-confirm before creating** (122 and 128 are historical collisions).
- **Two dashboards:** `:3000` = `src/channels/api/server.js` (johnbot-embedded, 10k-line monolith, inline HTML/JS, module spawn style `python3 -m pkg.mod` + `env PYTHONPATH=src`). `:7870` = `src/channels/dashboard/server.js` (control room, localhost-only, `public/index.html`, shell-out style `python3 -m src.pkg.mod` with `cwd=repoRoot`). Match each server's own conventions.
- **Discord from Python:** webhook lives in Postgres `agent_registry.webhook_urls['universe-recs']` (NOT .env). MUST send explicit `User-Agent` header (Cloudflare 1010 bans default python UA). Mirror `src/execution/fold_report.py:33-57,113-134`.
- **Box:** 2-core / 8 GB / no swap. Anything heavy: sequential, `nice -n 19`, subprocess-per-item.
- **Commit style:** `feat(sp7-phase-b): <what>` / `test(sp7-phase-b): ...` / `docs(sp7-phase-b): ...`, frequent.

## File map

| File | Action | Responsibility |
|---|---|---|
| `src/strategies/universe_default.py` | Modify | +4 predicates (liquid_tradable, tier_r1000, tier_r3000, tier_liquid) |
| `src/database/migrations/131_universe_ladder_runs.sql` | Create | ladder cell queue/audit table |
| `src/database/migrations/132_universe_threshold_proposals.sql` | Create | B3 proposal table |
| `scripts/sp7_b0_repair_metadata.py` | Create | B0 month + dailies repair (UPDATE-based supersede) |
| `src/system_checks/checks/universe_tier_coherence.py` | Create | B0 acceptance probe (regression guard) |
| `scripts/build_tier_membership.py` | Create | per-run tier-membership precompute artifact |
| `src/backtest/precomputed_resolver.py` | Create | dict-lookup resolver (no DB/parquet per bar) |
| `src/backtest/universe_grid_cli.py` | Modify | `--membership-artifact/--tier` + trade_sha output |
| `src/backtest/universe_ladder_selection.py` | Create | pure deterministic selection rule |
| `src/backtest/universe_ladder_recs.py` | Create | rec INSERT + Discord post (python) |
| `scripts/supersede_legacy_universe_recs.py` | Create | one-shot: tag the 58 stale 2026-05-25 rows |
| `scripts/run_universe_ladder.py` | Create | queue driver: seed / drain |
| `scripts/overnight_ladder.sh` | Create | nightly window wrapper (sentinel-armed) |
| `docs/sp7-ladder.{service,timer}` | Create | systemd user units (install via runbook) |
| `src/execution/universe_threshold_proposals.py` | Create | B3 union-N computation + write |
| `src/strategies/lifecycle_universe_adoption.py` | Modify | post-adopt hook → B3 recompute (best-effort) |
| `src/channels/api/server.js` | Modify | B3 GET/POST endpoints + Conviction Gates proposals UI |
| `scripts/check_ladder_saturday.py` | Create | 12th-Saturday sentinel check |
| `src/maintenance/weekend_saturday.sh` | Modify | step 8 re-point |
| `src/channels/dashboard/server.js` | Modify | :7870 GET ladder summary + POST recompute |
| `src/channels/dashboard/public/index.html` | Modify | ladder tile |
| `docs/sp7-phase-b-runbook.md` | Create | ordered activation runbook |

---

### Task 1: Tier predicates + nesting property tests

**Files:**
- Modify: `src/strategies/universe_default.py`
- Test: `tests/test_sp7_tier_predicates.py`

- [ ] **Step 1: Verify TickerMetadata field defaults before writing the test factory**

Run: `sed -n '1,30p' src/strategies/universe_meta.py`
Expected: frozen dataclass with fields `symbol, asset_class, exchange, status, tradable, shortable, fractionable, easy_to_borrow, market_cap, adv_usd_20d, sector, industry, options_eligible, in_sp500, in_r1000, in_r3000, listed_date, delisted_date`. If fields lack defaults, the factory below must pass them all.

- [ ] **Step 2: Write the failing test**

```python
"""SP-7 Phase B Task 1 — tier predicates: nesting by construction."""
from __future__ import annotations
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.strategies.universe_meta import TickerMetadata
from src.strategies import universe_default as ud


def _meta(**over):
    base = dict(
        symbol='TEST', asset_class='us_equity', exchange='NASDAQ',
        status='active', tradable=True, shortable=True, fractionable=True,
        easy_to_borrow=True, market_cap=None, adv_usd_20d=None,
        sector=None, industry=None, options_eligible=False,
        in_sp500=False, in_r1000=False, in_r3000=False,
        listed_date=None, delisted_date=None,
    )
    base.update(over)
    return TickerMetadata(**base)


def test_liquid_tradable_definition():
    assert ud.liquid_tradable(_meta(), None) is True
    assert ud.liquid_tradable(_meta(tradable=False), None) is False
    assert ud.liquid_tradable(_meta(status='inactive'), None) is False
    assert ud.liquid_tradable(_meta(easy_to_borrow=False), None) is False


def test_tier_unions():
    # sp500-only name is in every tier
    m = _meta(in_sp500=True, tradable=False, easy_to_borrow=False)
    assert ud.sp500(m, None) and ud.tier_r1000(m, None)
    assert ud.tier_r3000(m, None) and ud.tier_liquid(m, None)
    # r1000-only
    m = _meta(in_r1000=True, tradable=False, easy_to_borrow=False)
    assert not ud.sp500(m, None) and ud.tier_r1000(m, None) and ud.tier_r3000(m, None)
    # liquid-only (not in any index)
    m = _meta()
    assert not ud.tier_r3000(m, None) and ud.tier_liquid(m, None)


def test_nesting_property_exhaustive():
    """For EVERY combination of the 6 driving booleans, nesting holds."""
    for sp, r1, r3, tr, etb, act in itertools.product([True, False], repeat=6):
        m = _meta(in_sp500=sp, in_r1000=r1, in_r3000=r3,
                  tradable=tr, easy_to_borrow=etb,
                  status='active' if act else 'inactive')
        chain = [ud.sp500(m, None), ud.tier_r1000(m, None),
                 ud.tier_r3000(m, None), ud.tier_liquid(m, None)]
        for narrow, broad in zip(chain, chain[1:]):
            assert (not narrow) or broad, f'nesting violated for {m}'


def test_candidate_predicates_registered():
    for name in ('liquid_tradable', 'tier_r1000', 'tier_r3000', 'tier_liquid'):
        assert name in ud.CANDIDATE_PREDICATES
    # legacy 12 untouched
    assert len(ud.CANDIDATE_PREDICATES) == 16
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_sp7_tier_predicates.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'liquid_tradable'`

- [ ] **Step 4: Implement the predicates**

Append to `src/strategies/universe_default.py` (after `top500_by_adv`, before `CANDIDATE_PREDICATES`; remember the module BANS datetime/time/os imports — these need none):

```python
# --- SP-7 Phase B tier ladder (nested by construction) ---

def liquid_tradable(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.tradable and meta.status == "active" and meta.easy_to_borrow)

def tier_r1000(meta: TickerMetadata, as_of) -> bool:
    return sp500(meta, as_of) or bool(meta.in_r1000)

def tier_r3000(meta: TickerMetadata, as_of) -> bool:
    return tier_r1000(meta, as_of) or bool(meta.in_r3000)

def tier_liquid(meta: TickerMetadata, as_of) -> bool:
    return tier_r3000(meta, as_of) or liquid_tradable(meta, as_of)
```

And add to the `CANDIDATE_PREDICATES` dict (keep the original 12 entries verbatim):

```python
    "liquid_tradable": liquid_tradable,
    "tier_r1000": tier_r1000,
    "tier_r3000": tier_r3000,
    "tier_liquid": tier_liquid,
```

- [ ] **Step 5: Run tests + the predicate lint**

Run: `python3 -m pytest tests/test_sp7_tier_predicates.py -v && python3 scripts/lint_universe_predicates.py`
Expected: all PASS; lint "clean".

- [ ] **Step 6: Commit**

```bash
git add src/strategies/universe_default.py tests/test_sp7_tier_predicates.py
git commit -m "feat(sp7-phase-b): nested tier predicates + liquid_tradable (16 candidates)"
```

---

### Task 2: Migrations 131 (ladder runs) + 132 (threshold proposals)

**Files:**
- Create: `src/database/migrations/131_universe_ladder_runs.sql`
- Create: `src/database/migrations/132_universe_threshold_proposals.sql`
- Test: `tests/test_sp7_migrations.py`

- [ ] **Step 1: Confirm numbers are free**

Run: `ls src/database/migrations/ | grep -E '^13[12]_'`
Expected: no output. If taken, use next free numbers and update every reference in later tasks.

- [ ] **Step 2: Write migration 131**

```sql
-- 131: universe_ladder_runs (SP-7 Phase B, 2026-06-06)
--
-- One row per (run_id, strategy_id, tier) ladder cell. The nightly queue
-- driver (scripts/run_universe_ladder.py) claims queued cells sequentially,
-- runs universe_grid_cli per cell, and writes terminal status + metrics here.
-- Resumability: cells stuck 'running' are reset to 'queued' at drain start.

CREATE TABLE IF NOT EXISTS universe_ladder_runs (
    id            BIGSERIAL    PRIMARY KEY,
    run_id        TEXT         NOT NULL,           -- e.g. 'ladder-20260608'
    strategy_id   TEXT         NOT NULL,
    tier          TEXT         NOT NULL,           -- sp500|tier_r1000|tier_r3000|tier_liquid
    status        TEXT         NOT NULL DEFAULT 'queued',
                  -- queued | running | done | timeout | error | skipped_degenerate
    window_start  DATE,
    window_end    DATE,
    artifact_path TEXT,
    metrics       JSONB,                           -- 8-key blend + trades_n etc.
    trade_sha     TEXT,
    duration_s    NUMERIC,
    stderr_tail   TEXT,
    queued_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    UNIQUE (run_id, strategy_id, tier)
);

CREATE INDEX IF NOT EXISTS idx_ulr_run_status
    ON universe_ladder_runs (run_id, status);

CREATE INDEX IF NOT EXISTS idx_ulr_strategy
    ON universe_ladder_runs (strategy_id, queued_at DESC);
```

- [ ] **Step 3: Write migration 132** (shape mimics 078; targets `regime_sizer_params`, global per-regime — NO strategy_id)

```sql
-- 132: universe_threshold_proposals (SP-7 Phase B B3, 2026-06-06)
--
-- √ln(N) breadth-scaled min_cumulative_sharpe PROPOSALS (never direct writes).
-- proposed = current_base × √(ln N_union / ln N_sp500), clamped [1.0, 10.0].
-- Mimics strategy_regime_param_proposals' shape (mig 078) but targets the
-- GLOBAL regime_sizer_params table, so no strategy_id column.

CREATE TABLE IF NOT EXISTS universe_threshold_proposals (
    id                              BIGSERIAL    PRIMARY KEY,
    proposed_at                     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    proposer                        TEXT         NOT NULL,   -- 'sp7b:<trigger>'
    regime_state                    TEXT         NOT NULL,   -- LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS
    current_row                     JSONB,                   -- regime_sizer_params snapshot
    proposed_min_cumulative_sharpe  NUMERIC      NOT NULL,
    basis                           JSONB,                   -- {n_union, n_sp500, factor, trigger}
    status                          TEXT         NOT NULL DEFAULT 'pending',
                                    -- pending | approved | rejected | superseded
    decided_at                      TIMESTAMPTZ,
    decided_by                      TEXT,
    decision_reason                 TEXT,
    applied_row                     JSONB
);

CREATE INDEX IF NOT EXISTS idx_utp_status
    ON universe_threshold_proposals (status, proposed_at DESC);
```

- [ ] **Step 4: Write the round-trip test** (applies SQL in a transaction, rolls back — house pattern from migration 119's test)

```python
"""SP-7 Phase B Task 2 — migrations 131/132 round-trip (rollback)."""
from __future__ import annotations
import os
import sys
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / 'src' / 'database' / 'migrations'

URI = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL')
pytestmark = pytest.mark.skipif(not URI, reason='POSTGRES_URI not set')


@pytest.mark.parametrize('fname,table', [
    ('131_universe_ladder_runs.sql', 'universe_ladder_runs'),
    ('132_universe_threshold_proposals.sql', 'universe_threshold_proposals'),
])
def test_migration_round_trip(fname, table):
    sql = (MIG / fname).read_text()
    conn = psycopg2.connect(URI)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] >= 0
            cur.execute(sql)  # idempotency: re-apply inside same txn must not raise
    finally:
        conn.rollback()
        conn.close()
```

- [ ] **Step 5: Run the test**

Run: `python3 -m pytest tests/test_sp7_migrations.py -v`
Expected: 2 PASS (or 2 SKIP without POSTGRES_URI — run with the env from `.env`).

- [ ] **Step 6: Commit**

```bash
git add src/database/migrations/131_universe_ladder_runs.sql src/database/migrations/132_universe_threshold_proposals.sql tests/test_sp7_migrations.py
git commit -m "feat(sp7-phase-b): migrations 131 universe_ladder_runs + 132 universe_threshold_proposals"
```

NOTE: migrations are APPLIED at activation time via the runbook (`npm run db:migrate` + verify tables exist — the runner has no applied-tracking), not during this task.

---

### Task 3: B0 repair script (months + dailies, UPDATE-based supersede)

**Files:**
- Create: `scripts/sp7_b0_repair_metadata.py`
- Test: `tests/test_sp7_b0_repair.py`

The bug: the §11b metadata backfill used `ON CONFLICT DO NOTHING`, so the ~403 v1 symbols kept ghost rows (`in_sp500=true, in_r1000=false, in_r3000=false, market_cap=NULL`) shadowing v2's correct values for every month. PK is `(snapshot_date, symbol)`. The repair recomputes the DERIVED columns for the full per-month symbol set and UPDATEs rows in place (never DELETE — append-only ethos; supersede = value repair + source_tag flip to `backfill_5y_v3`).

Reused machinery (signatures verified 2026-06-06):
- `build_month_snapshot(snapshot_date: date, universe: list[str], pg, *, market_cap_lookup=None, fetch_market_cap=False) -> pd.DataFrame` (`src/pipeline/backfillers/universe_metadata.py:291`)
- `build_market_cap_lookup(symbols: list[str], as_of: date, *, shares_path=..., prices_path=...) -> dict[str, Optional[float]]` (`src/pipeline/market_cap_lookup.py:21`)
- `rank_in_r1000_r3000(rows_or_df) -> tuple[set[str], set[str]]` (`universe_metadata.py:239`; pool = tradable ∧ active ∧ market_cap non-None — this pool definition is WHY r3000 < 3000 in thin months; acceptance reports the pool)

- [ ] **Step 1: Write the failing tests** (pure parts: gate, month enumeration, update-row diffing; no live DB)

```python
"""SP-7 Phase B Task 3 — B0 repair: gate, month enumeration, diff logic."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import sp7_b0_repair_metadata as b0


def test_gate_refuses_without_env():
    with patch.dict('os.environ', {}, clear=False):
        import os
        os.environ.pop('OPENCLAW_BACKFILL_ALLOW_OVERWRITE', None)
        with pytest.raises(SystemExit) as e:
            b0.check_overwrite_gate()
        assert e.value.code == 2


def test_gate_passes_with_env():
    with patch.dict('os.environ', {'OPENCLAW_BACKFILL_ALLOW_OVERWRITE': '1'}):
        b0.check_overwrite_gate()  # no raise


def test_month_ends_span():
    ends = b0.month_ends(date(2021, 1, 1), date(2021, 3, 15))
    assert ends == [date(2021, 1, 31), date(2021, 2, 28), date(2021, 3, 15)]
    # final partial month capped at end date


def test_diff_updates_only_changed_rows():
    existing = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': False, 'in_r3000': False, 'market_cap': None},
        {'symbol': 'OK',   'in_sp500': False, 'in_r1000': True, 'in_r3000': True, 'market_cap': 5e9},
    ])
    rebuilt = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': True, 'in_r3000': True, 'market_cap': 2.9e12},
        {'symbol': 'OK',   'in_sp500': False, 'in_r1000': True, 'in_r3000': True, 'market_cap': 5e9},
    ])
    updates = b0.diff_derived(existing, rebuilt)
    assert [u['symbol'] for u in updates] == ['AAPL']
    assert updates[0]['in_r1000'] is True and updates[0]['market_cap'] == 2.9e12
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_b0_repair.py -v`
Expected: FAIL — `ModuleNotFoundError` / missing attributes.

- [ ] **Step 3: Implement**

```python
#!/usr/bin/env python3
"""SP-7 Phase B B0 — metadata coherence repair.

Repairs the v1/v2 ghost-row incoherence (mega-caps missing from historical
r1000/r3000; in_sp500 undercount) and the degenerate live_daily snapshots
2026-05-25..2026-06-04 (in_r3000=0, market_cap=0).

UPDATE-based supersede: recompute derived columns (in_sp500, in_r1000,
in_r3000, market_cap) per (snapshot_date, symbol) and UPDATE rows whose
values differ, flipping source_tag to 'backfill_5y_v3'. NEVER deletes.

Usage:
  OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/sp7_b0_repair_metadata.py \
      --months --start 2021-01-01 --end 2026-05-31 [--dry-run] [--resume]
  OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/sp7_b0_repair_metadata.py \
      --dailies [--dry-run]

Exit codes: 0 ok · 1 error · 2 gate refused.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

SOURCE_TAG = 'backfill_5y_v3'
DEGENERATE_DAILIES = [
    '2026-05-25', '2026-05-26', '2026-05-27', '2026-05-28', '2026-05-29',
    '2026-06-01', '2026-06-02', '2026-06-03', '2026-06-04',
]
DERIVED = ('in_sp500', 'in_r1000', 'in_r3000', 'market_cap')


def check_overwrite_gate() -> None:
    if os.environ.get('OPENCLAW_BACKFILL_ALLOW_OVERWRITE') != '1':
        print('[b0] REFUSED: set OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 '
              '(documented supersede gate)', file=sys.stderr)
        sys.exit(2)


def month_ends(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        last = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        out.append(min(last, end))
        cur = (last + timedelta(days=1))
    return out


def _norm(v):
    """pandas NaN -> None; passthrough otherwise."""
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    return v


def diff_derived(existing, rebuilt) -> list[dict]:
    """Rows (dicts incl. symbol) from `rebuilt` whose DERIVED cols differ
    from `existing` (bool compare for flags; $1 epsilon for market_cap).
    UPDATE-only: symbols absent from `existing` are ignored."""
    ex = existing.set_index('symbol')
    updates = []
    for row in rebuilt.itertuples():
        if row.symbol not in ex.index:
            continue
        old = ex.loc[row.symbol]
        upd, changed = {'symbol': row.symbol}, False
        for col in DERIVED:
            new_v, old_v = _norm(getattr(row, col)), _norm(old[col])
            if col == 'market_cap':
                differs = ((old_v is None) != (new_v is None)
                           or (old_v is not None and new_v is not None
                               and abs(float(old_v) - float(new_v)) > 1.0))
            else:
                differs = bool(old_v) != bool(new_v)
            upd[col] = new_v
            changed = changed or differs
        if changed:
            updates.append(upd)
    return updates


def _audit(cur, chunk_key: str, status: str, rows: int = 0, err: str | None = None):
    cur.execute(
        """INSERT INTO backfill_audit
             (target, chunk_key, started_at, ended_at, status, rows_written,
              source_tag, sha256, error_text)
           VALUES ('metadata', %s, NOW(), NOW(), %s, %s, %s, NULL, %s)""",
        (chunk_key, status, rows, SOURCE_TAG, err))


def repair_month(pg, snap: date, *, dry_run: bool) -> int:
    import pandas as pd
    from src.pipeline.backfillers.universe_metadata import build_month_snapshot
    from src.pipeline.market_cap_lookup import build_market_cap_lookup

    with pg.cursor() as cur:
        cur.execute(
            """SELECT symbol, in_sp500, in_r1000, in_r3000, market_cap
                 FROM ticker_metadata_snapshots WHERE snapshot_date = %s""",
            (snap,))
        cols = [d.name for d in cur.description]
        existing = pd.DataFrame(cur.fetchall(), columns=cols)
    if existing.empty:
        print(f'[b0] {snap} no rows — skip')
        return 0
    universe = sorted(existing.symbol)
    caps = build_market_cap_lookup(universe, snap)
    rebuilt = build_month_snapshot(snap, universe, pg, market_cap_lookup=caps)
    updates = diff_derived(existing, rebuilt)
    print(f'[b0] {snap} rows={len(existing)} changed={len(updates)}')
    if dry_run or not updates:
        return len(updates)
    with pg.cursor() as cur:
        from psycopg2.extras import execute_batch
        execute_batch(cur, f"""
            UPDATE ticker_metadata_snapshots
               SET in_sp500=%(in_sp500)s, in_r1000=%(in_r1000)s,
                   in_r3000=%(in_r3000)s, market_cap=%(market_cap)s,
                   source_tag='{SOURCE_TAG}'
             WHERE snapshot_date=%(snap)s AND symbol=%(symbol)s""",
            [{**u, 'snap': snap} for u in updates], page_size=500)
        _audit(cur, f'{snap.isoformat()}:metadata:repair_v3', 'promoted', len(updates))
    pg.commit()
    return len(updates)


def repair_dailies(pg, *, dry_run: bool) -> int:
    """Fill ONLY the failed derived columns on the 9 degenerate daily
    snapshots: market_cap (shares×close) then rank-based in_r1000/in_r3000.
    in_sp500 + observed columns untouched (they were written correctly)."""
    import pandas as pd
    from src.pipeline.market_cap_lookup import build_market_cap_lookup
    from src.pipeline.backfillers.universe_metadata import rank_in_r1000_r3000

    total = 0
    for iso in DEGENERATE_DAILIES:
        snap = date.fromisoformat(iso)
        with pg.cursor() as cur:
            cur.execute(
                """SELECT symbol, status, tradable, market_cap
                     FROM ticker_metadata_snapshots WHERE snapshot_date=%s""",
                (snap,))
            df = pd.DataFrame(cur.fetchall(),
                              columns=[d.name for d in cur.description])
        if df.empty:
            print(f'[b0-dailies] {iso} no rows — skip')
            continue
        caps = build_market_cap_lookup(sorted(df.symbol), snap)
        df['market_cap'] = df.symbol.map(lambda s: caps.get(s))
        r1000, r3000 = rank_in_r1000_r3000(df)
        df['in_r1000'] = df.symbol.isin(r1000)
        df['in_r3000'] = df.symbol.isin(r3000)
        n = int(df.in_r3000.sum())
        print(f'[b0-dailies] {iso} rows={len(df)} r1000={int(df.in_r1000.sum())} r3000={n}')
        if dry_run:
            continue
        with pg.cursor() as cur:
            from psycopg2.extras import execute_batch
            execute_batch(cur, """
                UPDATE ticker_metadata_snapshots
                   SET market_cap=%(market_cap)s, in_r1000=%(in_r1000)s,
                       in_r3000=%(in_r3000)s
                 WHERE snapshot_date=%(snap)s AND symbol=%(symbol)s""",
                [{'symbol': r.symbol,
                  'market_cap': None if r.market_cap != r.market_cap else r.market_cap,
                  'in_r1000': bool(r.in_r1000), 'in_r3000': bool(r.in_r3000),
                  'snap': snap} for r in df.itertuples()], page_size=500)
            _audit(cur, f'{iso}:metadata:repair_dailies_v3', 'promoted', len(df))
        pg.commit()
        total += len(df)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--months', action='store_true')
    ap.add_argument('--dailies', action='store_true')
    ap.add_argument('--start', default='2021-01-01')
    ap.add_argument('--end', default='2026-05-31')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--resume', action='store_true',
                    help='skip months already audited promoted for repair_v3')
    args = ap.parse_args()
    if not (args.months or args.dailies):
        ap.error('need --months and/or --dailies')
    check_overwrite_gate()

    import psycopg2
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        if args.months:
            done = set()
            if args.resume:
                with pg.cursor() as cur:
                    cur.execute("""SELECT chunk_key FROM backfill_audit
                                   WHERE source_tag=%s AND status='promoted'
                                     AND chunk_key LIKE '%%:metadata:repair_v3'""",
                                (SOURCE_TAG,))
                    done = {r[0].split(':')[0] for r in cur.fetchall()}
            for snap in month_ends(date.fromisoformat(args.start),
                                   date.fromisoformat(args.end)):
                if snap.isoformat() in done:
                    print(f'[b0] {snap} already promoted — resume-skip')
                    continue
                repair_month(pg, snap, dry_run=args.dry_run)
        if args.dailies:
            repair_dailies(pg, dry_run=args.dry_run)
        print('[b0] DONE')
        return 0
    finally:
        pg.close()


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_sp7_b0_repair.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Live dry-run sanity on ONE month (read-only)**

Run: `cd /root/openclaw && set -a; . <(grep -E '^POSTGRES_URI' .env); set +a; OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/sp7_b0_repair_metadata.py --months --start 2023-06-01 --end 2023-06-30 --dry-run`
Expected: `[b0] 2023-06-30 rows=~3900 changed=~400+` (the v1 ghost cohort) and exit 0. Record the changed-count in the task report.

- [ ] **Step 6: Commit**

```bash
git add scripts/sp7_b0_repair_metadata.py tests/test_sp7_b0_repair.py
git commit -m "feat(sp7-phase-b): B0 metadata coherence repair (months + degenerate dailies, UPDATE supersede)"
```

---

### Task 4: B0 acceptance probe — `universe_tier_coherence` system_check

**Files:**
- Create: `src/system_checks/checks/universe_tier_coherence.py`
- Modify: `src/system_checks/checks/__init__.py` (import for side-effect — required for registration)
- Test: `tests/test_sp7_tier_coherence_check.py`

- [ ] **Step 1: Write the failing test**

```python
"""SP-7 Phase B Task 4 — universe_tier_coherence probe registration + logic."""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


def test_probe_registered():
    from system_checks import registry
    import system_checks.checks  # side-effect imports
    names = {c.name for c in registry.all_checks()} if hasattr(registry, 'all_checks') else None
    # Fallback: run_one() raises KeyError for unknown names
    from system_checks import run_one
    res = run_one('universe_tier_coherence')
    assert res is not None  # SKIP without db is fine; KeyError means unregistered


@pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='no db')
def test_probe_runs_against_live_db():
    from system_checks import run_one
    res = run_one('universe_tier_coherence')
    # Pre-B0 this is expected FAIL (mega-caps missing from r1000 history).
    # Post-B0 it must PASS. Either way it must not ERROR.
    assert str(res.status if hasattr(res, 'status') else res[0]) not in ('Status.ERROR', 'ERROR')
```

NOTE: before writing, read `src/system_checks/README.md:7-67` and `src/system_checks/runner.py` to confirm the exact `run_one` return shape, and mirror `src/system_checks/checks/index_integrity.py` for structure. Adjust the assertions to the real API — the intent (registered; never ERROR) must hold.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_tier_coherence_check.py -v`
Expected: FAIL (probe not registered).

- [ ] **Step 3: Implement the probe**

```python
"""SP-7 Phase B — tier-coherence guard for ticker_metadata_snapshots.

Catches the v1/v2 ghost-row class (mega-caps absent from rank tiers) and
degenerate daily snapshots (rank flags never computed). See spec
docs/superpowers/specs/2026-06-06-sp7-phase-b-tier-ladder-design.md §3.
"""
from __future__ import annotations
import os

import psycopg2

from ..registry import check
from ..types import Status

MEGA_CAPS = ('AAPL', 'MSFT', 'NVDA', 'JPM')
PROBE_MONTHS = ('2021-07-31', '2023-06-30', '2025-06-30')


@check(name='universe_tier_coherence', tags=['strategies'], requires=['db'])
def _universe_tier_coherence():
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL', '')
    if not uri:
        return Status.FAIL, 'POSTGRES_URI not set'
    conn = psycopg2.connect(uri)
    try:
        cur = conn.cursor()
        problems = []
        # 1) mega-caps must be in_r1000 at every probe month (resolver's exact query)
        for snap in PROBE_MONTHS:
            cur.execute("""
                SELECT symbol FROM (
                  SELECT DISTINCT ON (symbol) symbol, in_r1000
                  FROM ticker_metadata_snapshots
                  WHERE snapshot_date <= %s AND symbol = ANY(%s)
                  ORDER BY symbol, snapshot_date DESC) t
                WHERE NOT in_r1000""", (snap, list(MEGA_CAPS)))
            missing = [r[0] for r in cur.fetchall()]
            if missing:
                problems.append(f'{snap}: {missing} not in_r1000')
        # 2) recent degenerate-daily detector: any snapshot in last 30d with
        #    >1000 rows where zero rows have in_r3000
        cur.execute("""
            SELECT snapshot_date FROM ticker_metadata_snapshots
            WHERE snapshot_date > CURRENT_DATE - 30
            GROUP BY snapshot_date
            HAVING count(*) > 1000 AND count(*) FILTER (WHERE in_r3000) = 0
            ORDER BY snapshot_date""")
        degenerate = [str(r[0]) for r in cur.fetchall()]
        if degenerate:
            problems.append(f'degenerate dailies (r3000=0): {degenerate[:5]}')
        if problems:
            return Status.FAIL, '; '.join(problems)[:200]
        return Status.PASS, (f'mega-caps in_r1000 at {len(PROBE_MONTHS)} probe months; '
                             'no degenerate dailies in 30d')
    except Exception as e:
        return Status.ERROR, f'tier-coherence sweep failed: {e}'
    finally:
        conn.close()
```

- [ ] **Step 4: Register in `checks/__init__.py`** — add `from . import universe_tier_coherence  # noqa: F401` matching the existing import list style.

- [ ] **Step 5: Run tests + the probe live (expected FAIL pre-B0 — that PROVES it detects the bug)**

Run: `python3 -m pytest tests/test_sp7_tier_coherence_check.py -v && set -a; . <(grep -E '^POSTGRES_URI' .env); set +a; python3 -m system_checks --check universe_tier_coherence`
Expected: pytest PASS; live probe **FAIL** with "AAPL... not in_r1000" (pre-B0 ground truth — record the output).

- [ ] **Step 6: Commit**

```bash
git add src/system_checks/checks/universe_tier_coherence.py src/system_checks/checks/__init__.py tests/test_sp7_tier_coherence_check.py
git commit -m "feat(sp7-phase-b): universe_tier_coherence system_check (B0 acceptance + regression guard)"
```

---

### Task 5: Tier-membership precompute artifact

**Files:**
- Create: `scripts/build_tier_membership.py`
- Test: `tests/test_sp7_tier_membership.py`

Semantics: replicate `MockResolver.resolve()` per snapshot date — `fetch_metadata_as_of(d)` rows → predicate → coverage floor (≥60 bars ≤ d) → sorted symbols — but computed ONCE per (tier, snapshot-date) instead of per bar per strategy. Snapshot dates = month-ends in window + the final window_end date (recent dailies). The coverage floor is hoisted into a (ticker × month) cumulative-count matrix built from ONE parquet read. KNOWN, DOCUMENTED semantic delta vs MockResolver: floor crossings mid-month snap to the next snapshot date (predicate inputs only change at snapshot dates anyway).

- [ ] **Step 1: Write the failing tests** (pure parts: coverage matrix, snapshot-date enumeration, nesting of output)

```python
"""SP-7 Phase B Task 5 — tier membership precompute (pure logic)."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import build_tier_membership as btm


def test_snapshot_dates():
    ds = btm.snapshot_dates(date(2021, 7, 1), date(2021, 9, 15))
    assert ds == [date(2021, 7, 31), date(2021, 8, 31), date(2021, 9, 15)]


def test_coverage_matrix_cumulative_floor():
    prices = pd.DataFrame({
        'ticker': ['A'] * 70 + ['B'] * 10,
        'date': [f'2021-{1 + i // 28:02d}-{1 + i % 28:02d}' for i in range(70)]
               + [f'2021-01-{i + 1:02d}' for i in range(10)],
    })
    cov = btm.CoverageIndex(prices, min_bars=60)
    # A has 70 bars by 2021-03-12; B never reaches 60
    assert cov.has_floor('A', date(2021, 3, 31)) is True
    assert cov.has_floor('A', date(2021, 1, 31)) is False   # only ~28 bars
    assert cov.has_floor('B', date(2021, 12, 31)) is False
    assert cov.has_floor('ZZZ', date(2021, 12, 31)) is False


def test_membership_nesting(monkeypatch):
    """Tiers built from the same rows must nest (predicates force it)."""
    from src.strategies.universe_meta import TickerMetadata

    def _m(sym, **over):
        base = dict(symbol=sym, asset_class='us_equity', exchange='NYSE',
                    status='active', tradable=True, shortable=True,
                    fractionable=True, easy_to_borrow=True, market_cap=None,
                    adv_usd_20d=None, sector=None, industry=None,
                    options_eligible=False, in_sp500=False, in_r1000=False,
                    in_r3000=False, listed_date=None, delisted_date=None)
        base.update(over)
        class R: pass
        r = R(); r.metadata = TickerMetadata(**base); r.symbol = sym
        return r

    rows = [_m('SPX1', in_sp500=True), _m('R1', in_r1000=True),
            _m('R3', in_r3000=True), _m('LIQ'),
            _m('DEAD', tradable=False)]

    class AllFloor:
        def has_floor(self, s, d): return True

    members = btm.tiers_for_rows(rows, date(2024, 1, 31), AllFloor())
    assert set(members['sp500']) <= set(members['tier_r1000'])
    assert set(members['tier_r1000']) <= set(members['tier_r3000'])
    assert set(members['tier_r3000']) <= set(members['tier_liquid'])
    assert 'DEAD' not in members['tier_liquid']
    assert members['tier_liquid'] == sorted(members['tier_liquid'])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_tier_membership.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
#!/usr/bin/env python3
"""SP-7 Phase B — one-time tier-membership precompute.

Builds data/universe_tier_membership_<run_id>.parquet with one row per
(tier, snapshot_date): the sorted member list after predicate + coverage
floor. Also writes a JSON sidecar with per-tier N series + data-level
nesting diagnostics (|in_sp500 ∧ ¬in_r1000| etc.).

Usage:
  python3 scripts/build_tier_membership.py --run-id ladder-20260608 \
      --start 2021-07-01 --end 2026-06-05
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

LADDER_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
MIN_BARS = 60  # mirrors ParquetCoverage min_bars (src/strategies/_db_adapters.py:45)


def snapshot_dates(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        last = date(cur.year, cur.month,
                    calendar.monthrange(cur.year, cur.month)[1])
        out.append(min(last, end))
        cur = last + timedelta(days=1)
    return out


class CoverageIndex:
    """(ticker × month) cumulative bar counts from ONE parquet read."""

    def __init__(self, prices_df, min_bars: int = MIN_BARS):
        import pandas as pd
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


def tiers_for_rows(rows, as_of: date, coverage) -> dict[str, list[str]]:
    from src.strategies import universe_default as ud
    preds = {t: getattr(ud, t) for t in LADDER_TIERS}
    out = {t: [] for t in LADDER_TIERS}
    for row in rows:
        meta = row.metadata
        if not coverage.has_floor(meta.symbol, as_of):
            continue
        for t, p in preds.items():
            try:
                if p(meta, as_of):
                    out[t].append(meta.symbol)
            except Exception:
                continue
    return {t: sorted(v) for t, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-id', required=True)
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--out-dir', default='data')
    args = ap.parse_args()

    import pandas as pd
    from src.strategies._db_adapters import PostgresMetadataDB

    db = PostgresMetadataDB(os.environ['POSTGRES_URI'])
    cov = CoverageIndex.from_parquet()
    dates = snapshot_dates(date.fromisoformat(args.start),
                           date.fromisoformat(args.end))
    records, n_series, diags = [], {t: {} for t in LADDER_TIERS}, []
    for snap in dates:
        rows = db.fetch_metadata_as_of(snap)
        members = tiers_for_rows(rows, snap, cov)
        for t in LADDER_TIERS:
            records.append({'run_id': args.run_id, 'tier': t,
                            'snapshot_date': snap.isoformat(),
                            'symbols': members[t]})
            n_series[t][snap.isoformat()] = len(members[t])
        # data-level diagnostic (predicates force nesting; this measures the RAW flags)
        raw = {m.metadata.symbol: m.metadata for m in rows}
        sp_not_r1 = sum(1 for m in raw.values() if m.in_sp500 and not m.in_r1000)
        diags.append({'snapshot_date': snap.isoformat(),
                      'sp500_not_in_r1000_raw': sp_not_r1,
                      'n_rows': len(rows)})
        print(f'[membership] {snap} ' +
              ' '.join(f'{t}={len(members[t])}' for t in LADDER_TIERS))

    out = Path(args.out_dir) / f'universe_tier_membership_{args.run_id}.parquet'
    pd.DataFrame(records).to_parquet(out, index=False)
    sidecar = out.with_suffix('.json')
    sidecar.write_text(json.dumps(
        {'run_id': args.run_id, 'window': [args.start, args.end],
         'n_series': n_series, 'diagnostics': diags}, indent=2))
    print(f'[membership] DONE artifact={out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_sp7_tier_membership.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_tier_membership.py tests/test_sp7_tier_membership.py
git commit -m "feat(sp7-phase-b): tier-membership precompute artifact + hoisted coverage index"
```

NOTE: do NOT run the full live build here — it needs B0-repaired data to be meaningful. The runbook sequences it.

---

### Task 6: PrecomputedResolver

**Files:**
- Create: `src/backtest/precomputed_resolver.py`
- Test: `tests/test_sp7_precomputed_resolver.py`

- [ ] **Step 1: Write the failing tests**

```python
"""SP-7 Phase B Task 6 — PrecomputedResolver: PIT bisect, future-guard, empty pre-window."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.precomputed_resolver import PrecomputedResolver
from src.strategies.universe_resolver import AsOfInFutureError


@pytest.fixture
def artifact(tmp_path):
    df = pd.DataFrame([
        {'run_id': 'r1', 'tier': 'sp500', 'snapshot_date': '2024-01-31', 'symbols': ['AAPL', 'MSFT']},
        {'run_id': 'r1', 'tier': 'sp500', 'snapshot_date': '2024-02-29', 'symbols': ['AAPL', 'MSFT', 'NVDA']},
        {'run_id': 'r1', 'tier': 'tier_liquid', 'snapshot_date': '2024-01-31', 'symbols': ['AAPL', 'MSFT', 'ZZZ']},
    ])
    p = tmp_path / 'art.parquet'
    df.to_parquet(p, index=False)
    return p


def test_bisect_most_recent_snapshot_leq(artifact):
    r = PrecomputedResolver(artifact, 'sp500', today_fn=lambda: date(2026, 1, 1))
    assert r.resolve('any_strategy', date(2024, 2, 15)) == ['AAPL', 'MSFT']
    assert r.resolve('any_strategy', date(2024, 3, 15)) == ['AAPL', 'MSFT', 'NVDA']


def test_pre_window_is_empty(artifact):
    r = PrecomputedResolver(artifact, 'sp500', today_fn=lambda: date(2026, 1, 1))
    assert r.resolve('s', date(2023, 6, 1)) == []


def test_future_guard(artifact):
    r = PrecomputedResolver(artifact, 'sp500', today_fn=lambda: date(2024, 6, 1))
    with pytest.raises(AsOfInFutureError):
        r.resolve('s', date(2024, 7, 1))


def test_tier_isolation_and_unknown_tier(artifact):
    r = PrecomputedResolver(artifact, 'tier_liquid', today_fn=lambda: date(2026, 1, 1))
    assert 'ZZZ' in r.resolve('s', date(2024, 2, 15))
    with pytest.raises(ValueError):
        PrecomputedResolver(artifact, 'no_such_tier')
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_precomputed_resolver.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""SP-7 Phase B — PrecomputedResolver.

Duck-types UniverseResolver.resolve(strategy_id, as_of) for the grid path
(_per_bar_simulate only calls .resolve), backed by the frozen membership
artifact: a dict lookup per bar — zero DB connections, zero parquet scans.
Replicates MockResolver's PIT semantics (most recent snapshot <= as_of) and
the AsOfInFutureError look-ahead guard.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date as _date
from pathlib import Path

from src.strategies.universe_resolver import AsOfInFutureError


class PrecomputedResolver:
    def __init__(self, artifact_path, tier: str,
                 today_fn=_date.today):
        import pandas as pd
        df = pd.read_parquet(Path(artifact_path))
        df = df[df['tier'] == tier]
        if df.empty:
            raise ValueError(f'tier {tier!r} not present in {artifact_path}')
        self._tier = tier
        pairs = sorted(
            (_date.fromisoformat(str(r.snapshot_date)[:10]), list(r.symbols))
            for r in df.itertuples())
        self._dates = [d for d, _ in pairs]
        self._members = {d: syms for d, syms in pairs}
        self._today_fn = today_fn

    def resolve(self, strategy_id: str, as_of: _date) -> list[str]:
        if as_of > self._today_fn():
            raise AsOfInFutureError(f'as_of {as_of} > today {self._today_fn()}')
        i = bisect_right(self._dates, as_of) - 1
        if i < 0:
            return []
        return self._members[self._dates[i]]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_sp7_precomputed_resolver.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/precomputed_resolver.py tests/test_sp7_precomputed_resolver.py
git commit -m "feat(sp7-phase-b): PrecomputedResolver (artifact-backed, zero per-bar IO)"
```

---

### Task 7: universe_grid_cli `--membership-artifact/--tier` + trade_sha

**Files:**
- Modify: `src/backtest/universe_grid_cli.py`
- Test: `tests/test_sp7_grid_cli_tier_mode.py`

- [ ] **Step 1: Write the failing tests**

```python
"""SP-7 Phase B Task 7 — grid CLI tier mode arg-wiring + trade_sha determinism."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import universe_grid_cli as cli


def test_trade_sha_deterministic_and_order_independent():
    t1 = {'ticker': 'AAPL', 'entry_date': '2024-01-03', 'direction': 'long', 'exit_date': '2024-01-10'}
    t2 = {'ticker': 'MSFT', 'entry_date': '2024-01-04', 'direction': 'short', 'exit_date': '2024-01-11'}
    assert cli.trade_sha([t1, t2]) == cli.trade_sha([t2, t1])
    assert cli.trade_sha([]) == cli.trade_sha([])
    assert cli.trade_sha([t1]) != cli.trade_sha([t2])


def test_main_rejects_mixed_modes(capsys):
    rc = cli.main_with_args(['--strategy', 'x', '--start', '2024-01-01',
                             '--end', '2024-02-01',
                             '--resolver-override', 'sp500',
                             '--membership-artifact', '/tmp/a.parquet',
                             '--tier', 'sp500'])
    assert rc == 2


def test_main_requires_tier_with_artifact():
    rc = cli.main_with_args(['--strategy', 'x', '--start', '2024-01-01',
                             '--end', '2024-02-01',
                             '--membership-artifact', '/tmp/a.parquet'])
    assert rc == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_grid_cli_tier_mode.py -v`
Expected: FAIL (`trade_sha`/`main_with_args` missing).

- [ ] **Step 3: Implement** — three changes to `universe_grid_cli.py`:

(a) Add near the top (after imports):

```python
import hashlib

def trade_sha(trades: list[dict]) -> str:
    """Deterministic SHA-256 over the trade list (order-independent).
    Used by the ladder driver's extremes-first degenerate detection."""
    lines = sorted(
        f"{t['ticker']}|{t['entry_date']}|{t['direction']}|{t.get('exit_date')}"
        for t in trades)
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()
```

(b) In `_simulate_grid`, change the return to include the SHA — `return per_regime, mean_universe_size, trade_sha(trades)` (update the function docstring + the tuple unpack in `main`).

(c) Rework `main()` → `main_with_args(argv=None)` (and `main()` delegates with `sys.argv[1:]`), adding the mutually-exclusive mode:

```python
def main_with_args(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='SP-2 Phase C grid / SP-7 Phase B tier-ladder cell')
    ap.add_argument('--strategy', required=True)
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--resolver-override',
                    help=f'legacy mode; one of: {sorted(CANDIDATE_PREDICATES)}')
    ap.add_argument('--membership-artifact',
                    help='SP-7 tier mode: path to the frozen membership parquet')
    ap.add_argument('--tier', help='SP-7 tier mode: tier name inside the artifact')
    ap.add_argument('--metrics-json', action='store_true')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args(argv)

    legacy = args.resolver_override is not None
    tiermode = args.membership_artifact is not None or args.tier is not None
    if legacy == tiermode:  # both or neither
        print('[universe_grid_cli] ERROR: pass EITHER --resolver-override '
              'OR (--membership-artifact AND --tier)', file=sys.stderr)
        return 2
    if tiermode and not (args.membership_artifact and args.tier):
        print('[universe_grid_cli] ERROR: tier mode needs BOTH '
              '--membership-artifact and --tier', file=sys.stderr)
        return 2

    try:
        if legacy:
            candidate = args.resolver_override
            if candidate not in CANDIDATE_PREDICATES:
                print(f'[universe_grid_cli] ERROR: unknown candidate "{candidate}". '
                      f'Valid choices: {sorted(CANDIDATE_PREDICATES)}', file=sys.stderr)
                return 2
            uri = os.environ.get('POSTGRES_URI')
            if not uri:
                raise RuntimeError('POSTGRES_URI not set')
            db = PostgresMetadataDB(uri)
            cov = ParquetCoverage()
            resolver = MockResolver(db=db, coverage=cov,
                                    predicate=CANDIDATE_PREDICATES[candidate],
                                    manifest_loader=_manifest_loader)
            label = candidate
        else:
            from backtest.precomputed_resolver import PrecomputedResolver
            resolver = PrecomputedResolver(args.membership_artifact, args.tier)
            label = args.tier

        per_regime, mus, tsha = _simulate_grid(args.strategy, args.start,
                                               args.end, resolver)
        day_freq = regime_day_frequency(REGIMES_PARQUET)
        metrics = blend_metrics(per_regime, day_freq, mean_universe_size=mus)
        metrics['trade_sha'] = tsha
        metrics['mode'] = 'tier' if tiermode else 'legacy'
        metrics['candidate'] = label
        print(json.dumps(metrics, sort_keys=True))
        return 0
    except FileNotFoundError as e:
        print(f'[universe_grid_cli] FAIL: {e}', file=sys.stderr)
        return 1
    except Exception as e:
        print(f'[universe_grid_cli] FAIL: {type(e).__name__}: {e}', file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1
```

Keep `if __name__ == '__main__': sys.exit(main())` working via `def main() -> int: return main_with_args()`.

- [ ] **Step 4: Run new tests + the legacy regression**

Run: `python3 -m pytest tests/test_sp7_grid_cli_tier_mode.py -v && grep -rl "universe_grid_cli" tests/ | xargs -r python3 -m pytest -v`
Expected: new tests PASS; any pre-existing grid-CLI tests still PASS (the legacy `--resolver-override` path is byte-equivalent: same resolver construction, same metrics keys plus three additive ones).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/universe_grid_cli.py tests/test_sp7_grid_cli_tier_mode.py
git commit -m "feat(sp7-phase-b): grid CLI tier mode (--membership-artifact/--tier) + trade_sha"
```

---

### Task 8: Deterministic selection rule

**Files:**
- Create: `src/backtest/universe_ladder_selection.py`
- Test: `tests/test_sp7_ladder_selection.py`

- [ ] **Step 1: Write the failing tests**

```python
"""SP-7 Phase B Task 8 — selection: narrowest eligible + ΔSharpe≥0.10 displacement."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.universe_ladder_selection import select_tier, LADDER_TIERS


def _m(sharpe, trades=100):
    return {'sharpe': sharpe, 'trades_n': trades}


def test_ladder_tiers_order():
    assert LADDER_TIERS == ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')


def test_narrowest_wins_on_tie_band():
    v = select_tier({'sp500': _m(1.00), 'tier_r1000': _m(1.05),
                     'tier_r3000': _m(1.09), 'tier_liquid': _m(0.5)})
    assert v['verdict'] == 'winner' and v['choice'] == 'sp500'  # +0.09 < 0.10


def test_broader_displaces_at_threshold():
    v = select_tier({'sp500': _m(1.00), 'tier_r1000': _m(1.10),
                     'tier_r3000': _m(1.15), 'tier_liquid': _m(1.12)})
    # r1000 displaces sp500 (Δ=0.10 vs sp500); r3000 does NOT displace r1000 (Δ=0.05)
    assert v['choice'] == 'tier_r1000'


def test_chained_displacement():
    v = select_tier({'sp500': _m(1.0), 'tier_r1000': _m(1.10),
                     'tier_r3000': _m(1.20), 'tier_liquid': _m(1.31)})
    assert v['choice'] == 'tier_liquid'


def test_none_and_low_trades_ineligible():
    v = select_tier({'sp500': _m(None), 'tier_r1000': _m(2.0, trades=10),
                     'tier_r3000': _m(1.0), 'tier_liquid': None})
    assert v['choice'] == 'tier_r3000'  # only eligible tier


def test_all_ineligible_is_no_signal():
    v = select_tier({'sp500': _m(None), 'tier_r1000': None,
                     'tier_r3000': _m(1.0, trades=5), 'tier_liquid': _m(None)})
    assert v['verdict'] == 'no_signal' and v['choice'] is None


def test_missing_tier_keys_treated_ineligible():
    v = select_tier({'sp500': _m(1.4)})
    assert v['choice'] == 'sp500'
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_ladder_selection.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""SP-7 Phase B — deterministic tier selection (no LLM).

Eligibility: blended sharpe non-None AND trades_n >= MIN_TRADES.
Winner: narrowest eligible tier; walking broader, a tier displaces the
current winner iff sharpe >= winner.sharpe + DELTA_SHARPE (parsimony).
"""
from __future__ import annotations

LADDER_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
DELTA_SHARPE = 0.10   # mirrors the weekend-coupling auto-apply threshold
MIN_TRADES = 30       # mirrors the weekend-coupling trade floor


def _eligible(m) -> bool:
    return (m is not None and m.get('sharpe') is not None
            and int(m.get('trades_n') or 0) >= MIN_TRADES)


def select_tier(metrics_by_tier: dict) -> dict:
    """metrics_by_tier: tier name -> metrics dict (or None for
    timeout/error/skipped cells). Returns a verdict dict."""
    eligible = [t for t in LADDER_TIERS if _eligible(metrics_by_tier.get(t))]
    comparisons = []
    if not eligible:
        return {'verdict': 'no_signal', 'choice': None,
                'eligible': [], 'comparisons': comparisons}
    winner = eligible[0]
    for t in eligible[1:]:
        w_s = float(metrics_by_tier[winner]['sharpe'])
        t_s = float(metrics_by_tier[t]['sharpe'])
        displaced = t_s >= w_s + DELTA_SHARPE
        comparisons.append({'challenger': t, 'incumbent': winner,
                            'delta': round(t_s - w_s, 4),
                            'displaced': displaced})
        if displaced:
            winner = t
    return {'verdict': 'winner', 'choice': winner,
            'eligible': eligible, 'comparisons': comparisons}
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_sp7_ladder_selection.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/universe_ladder_selection.py tests/test_sp7_ladder_selection.py
git commit -m "feat(sp7-phase-b): deterministic ladder selection (narrowest-eligible + ΔSharpe displacement)"
```

---

### Task 9: Rec persist + Discord post (python) + legacy supersede one-shot

**Files:**
- Create: `src/backtest/universe_ladder_recs.py`
- Create: `scripts/supersede_legacy_universe_recs.py`
- Test: `tests/test_sp7_ladder_recs.py`

Reuse contract (verified): rows must INSERT with `approved=NULL` (pending), `candidate_predicate` ∈ `CANDIDATE_PREDICATES`, `candidate_set_id` NOT NULL, `backtest_summary` jsonb NOT NULL; change-recs' Discord posts MUST end with `_footer: universe-rec:<id>_` (reaction parser regex `/universe-rec:(\d+)/`, `src/channels/discord/_universe_rec_reaction.js:13-37`); webhook from `agent_registry.webhook_urls['universe-recs']` with explicit User-Agent (`src/execution/fold_report.py:33-57,113-134`).

- [ ] **Step 1: Write the failing tests**

```python
"""SP-7 Phase B Task 9 — rec rows + Discord formatting contracts."""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import universe_ladder_recs as recs


GRID = [
    {'name': 'sp500', 'sharpe': 1.0, 'max_dd_pct': 12.0, 'win_rate': 0.55,
     'trades_n': 120, 'sortino': 1.4, 'calmar': 0.9, 'mean_holding_days': 4.0,
     'mean_universe_size': 350.0},
    {'name': 'tier_liquid', 'sharpe': 1.2, 'max_dd_pct': 14.0, 'win_rate': 0.54,
     'trades_n': 300, 'sortino': 1.6, 'calmar': 1.0, 'mean_holding_days': 4.2,
     'mean_universe_size': 4100.0},
]


def test_change_message_has_required_footer():
    msg = recs.format_change_message('momentum_12_1', 'sp500', 'tier_liquid',
                                     'displaced: Δ=+0.20', GRID, rec_id=987)
    assert msg.rstrip().endswith('_footer: universe-rec:987_')
    assert re.search(r'universe-rec:(\d+)', msg).group(1) == '987'
    assert '| `sp500` |' in msg and '| `tier_liquid` |' in msg
    assert len(msg) <= 1900


def test_summary_message_has_no_footer():
    msg = recs.format_summary_message([
        ('s1', 'no_signal'), ('s2', 'universe-independent'), ('s3', 'no_change')])
    assert 'universe-rec:' not in msg
    assert 's1' in msg and len(msg) <= 1900


def test_rationale_is_deterministic():
    v = {'verdict': 'winner', 'choice': 'tier_r3000',
         'eligible': ['sp500', 'tier_r3000'],
         'comparisons': [{'challenger': 'tier_r3000', 'incumbent': 'sp500',
                          'delta': 0.15, 'displaced': True}]}
    r1 = recs.build_rationale(v, window=('2021-07-01', '2026-06-05'))
    r2 = recs.build_rationale(v, window=('2021-07-01', '2026-06-05'))
    assert r1 == r2 and 'tier_r3000' in r1 and '0.15' in r1
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_ladder_recs.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `src/backtest/universe_ladder_recs.py`**

```python
"""SP-7 Phase B — recommendation persistence + Discord posting (python side).

Mirrors the JS plumbing contracts exactly:
- row shape: universe_recommender.js _persist (approved=NULL pending)
- message shape + REQUIRED footer: universe_recommender.js _formatDiscordMessage
- webhook: agent_registry.webhook_urls['universe-recs'] + explicit UA
  (Cloudflare 1010 — see reference_discord_urllib_cloudflare_ua)
"""
from __future__ import annotations

import json
import os
import urllib.request

GRID_COLS = ('sharpe', 'max_dd_pct', 'win_rate', 'trades_n', 'sortino', 'calmar')
USER_AGENT = 'OpenClaw-LadderRecs/1.0 (+botjohn)'


def insert_recommendation(pg, *, strategy_id: str, current_predicate: str,
                          candidate_predicate: str, candidate_set_id: str,
                          backtest_summary: dict, rationale: str) -> int:
    with pg.cursor() as cur:
        cur.execute(
            """INSERT INTO strategy_universe_recommendations
                 (strategy_id, current_predicate, candidate_predicate,
                  candidate_set_id, backtest_summary, rationale, approved,
                  mastermind_cost_usd)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, NULL, NULL)
               RETURNING id""",
            (strategy_id, current_predicate, candidate_predicate,
             candidate_set_id, json.dumps(backtest_summary), rationale))
        rec_id = cur.fetchone()[0]
    pg.commit()
    return int(rec_id)


def build_rationale(verdict: dict, *, window: tuple[str, str]) -> str:
    parts = [f"sp7b tier-ladder {window[0]}..{window[1]};",
             f"eligible={','.join(verdict['eligible']) or 'none'};"]
    for c in verdict.get('comparisons', []):
        mark = '→' if c['displaced'] else '✗'
        parts.append(f"{c['challenger']} vs {c['incumbent']} Δ={c['delta']:+.2f}{mark};")
    parts.append(f"verdict={verdict['verdict']}"
                 + (f" choice={verdict['choice']}" if verdict['choice'] else ''))
    return ' '.join(parts)[:1000]


def _fmt(v):
    return 'n/a' if v is None else str(v)


def format_change_message(strategy_id: str, current: str, choice: str,
                          rationale: str, grid: list[dict], *, rec_id: int) -> str:
    lines = [
        f'**Universe Rec — {strategy_id}**',
        f'Current: `{current}` → Proposed: `{choice}`',
        'Confidence: deterministic (sp7b ladder)',
        f'Rationale: {rationale[:400]}', '', '**Grid:**',
        '| Candidate | Sharpe | MaxDD% | WinRate | Trades | Sortino | Calmar |',
        '|---|---|---|---|---|---|---|',
    ]
    for row in grid:
        lines.append('| `' + row['name'] + '` | '
                     + ' | '.join(_fmt(row.get(c)) for c in GRID_COLS) + ' |')
    lines.append('')
    lines.append('React ✅ to approve · ❌ to reject · ⏸ to defer')
    lines.append(f'_footer: universe-rec:{rec_id}_')
    return '\n'.join(lines)[:1900]


def format_summary_message(non_changes: list[tuple[str, str]]) -> str:
    """Batched no-change/no-signal/universe-independent verdicts — NOT
    adoptable, so NO universe-rec footer (the reaction parser must not fire)."""
    lines = ['**Universe Ladder — no-change verdicts**']
    for sid, verdict in non_changes:
        lines.append(f'- `{sid}`: {verdict}')
    return '\n'.join(lines)[:1900]


def get_webhook(pg) -> str | None:
    direct = os.environ.get('DISCORD_UNIVERSE_RECS_WEBHOOK')
    if direct:
        return direct
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT webhook_urls FROM agent_registry "
                        "WHERE webhook_urls IS NOT NULL")
            for (hooks,) in cur.fetchall():
                if hooks and 'universe-recs' in hooks:
                    return hooks['universe-recs']
    except Exception as e:
        print(f'[ladder-recs] webhook lookup failed: {e}')
    return None


def post_discord(pg, msg: str) -> bool:
    url = get_webhook(pg)
    if not url:
        print('[ladder-recs] no universe-recs webhook — skipping post')
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps({'content': msg[:1900]}).encode(),
            headers={'Content-Type': 'application/json',
                     'User-Agent': USER_AGENT})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f'[ladder-recs] webhook post failed ({e}) — non-fatal')
        return False
```

- [ ] **Step 4: Implement the one-shot supersede script**

```python
#!/usr/bin/env python3
"""One-shot: tag the 58 stale 2026-05-25 legacy Phase-C recommendation rows
as superseded (rationale append — NO deletion, spec §5). Idempotent."""
from __future__ import annotations
import os
import sys

import psycopg2

TAG = ' [superseded-by:sp7b-tier-ladder 2026-06-06]'


def main() -> int:
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""
            UPDATE strategy_universe_recommendations
               SET rationale = COALESCE(rationale, '') || %s
             WHERE recommended_at::date = '2026-05-25'
               AND approved IS NULL AND adopted = FALSE
               AND COALESCE(rationale, '') NOT LIKE %s""",
            (TAG, '%superseded-by:sp7b-tier-ladder%'))
        print(f'[supersede] tagged {cur.rowcount} legacy rows')
    pg.commit()
    pg.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_sp7_ladder_recs.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/backtest/universe_ladder_recs.py scripts/supersede_legacy_universe_recs.py tests/test_sp7_ladder_recs.py
git commit -m "feat(sp7-phase-b): ladder rec persist + Discord post (footer contract) + legacy supersede one-shot"
```

---

### Task 10: Queue driver — seed / drain

**Files:**
- Create: `scripts/run_universe_ladder.py`
- Test: `tests/test_sp7_ladder_driver.py`

Behaviors (spec §4/§5): strategy-major; extremes-first (sp500 priority 0, tier_liquid 1, tier_r1000 2, tier_r3000 3); degenerate skip when extremes' trade_shas match; per-cell budgets 7200s default / 21600s slow-list (`S_tr_03_bocpd_change_point`, `S_pairs_trading_jump_diffusion_intraday` — the only two with runtime evidence; do NOT pre-blacklist others); 3-consecutive-error strategy failure; RAM floor 1800MB before each cell; stuck-`running` cells reset to `queued` at drain start (mid-kill recovery); per-strategy selection + rec persist as soon as 4 cells are terminal; `[ladder] DONE` printed when queue empties (the wrapper greps it).

- [ ] **Step 1: Write the failing tests** (drive the pure/queue logic with a FAKE cell runner — no real backtests)

```python
"""SP-7 Phase B Task 10 — ladder driver queue logic (fake runner)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import run_universe_ladder as drv


def test_cell_priority_extremes_first():
    assert drv.TIER_PRIORITY == {'sp500': 0, 'tier_liquid': 1,
                                 'tier_r1000': 2, 'tier_r3000': 3}


def test_budget_for():
    assert drv.budget_for('S_tr_03_bocpd_change_point') == 21600
    assert drv.budget_for('S_pairs_trading_jump_diffusion_intraday') == 21600
    assert drv.budget_for('anything_else') == 7200


def test_degenerate_detection():
    cells = {'sp500': {'status': 'done', 'trade_sha': 'abc'},
             'tier_liquid': {'status': 'done', 'trade_sha': 'abc'}}
    assert drv.is_degenerate(cells) is True
    cells['tier_liquid']['trade_sha'] = 'xyz'
    assert drv.is_degenerate(cells) is False
    cells['tier_liquid'] = {'status': 'error', 'trade_sha': None}
    assert drv.is_degenerate(cells) is False  # error ≠ identical


def test_consecutive_error_policy():
    assert drv.should_fail_strategy(['error', 'error', 'error']) is True
    assert drv.should_fail_strategy(['error', 'done', 'error']) is False
    assert drv.should_fail_strategy(['error', 'error']) is False


def test_finalize_payload_winner_change():
    W = ('2021-07-01', '2026-06-05')
    cells = {
        'sp500':       {'status': 'done', 'metrics': {'sharpe': 1.0, 'trades_n': 100}, 'w': W},
        'tier_r1000':  {'status': 'done', 'metrics': {'sharpe': 1.2, 'trades_n': 100}, 'w': W},
        'tier_r3000':  {'status': 'timeout', 'metrics': None, 'w': W},
        'tier_liquid': {'status': 'done', 'metrics': {'sharpe': 1.1, 'trades_n': 100}, 'w': W},
    }
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'change' and p['choice'] == 'tier_r1000'
    assert p['summary']['grid'][0]['name'] == 'sp500'
    assert p['summary']['cell_statuses']['tier_r3000'] == 'timeout'


def test_finalize_payload_degenerate():
    W = ('2021-07-01', '2026-06-05')
    cells = {
        'sp500':       {'status': 'done', 'metrics': {'sharpe': 1.0, 'trades_n': 100}, 'w': W},
        'tier_r1000':  {'status': 'skipped_degenerate', 'metrics': None, 'w': W},
        'tier_r3000':  {'status': 'skipped_degenerate', 'metrics': None, 'w': W},
        'tier_liquid': {'status': 'done', 'metrics': {'sharpe': 1.0, 'trades_n': 100}, 'w': W},
    }
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'universe-independent' and p['choice'] == 'sp500'


def test_finalize_payload_no_signal():
    W = ('2021-07-01', '2026-06-05')
    cells = {t: {'status': 'error', 'metrics': None, 'w': W}
             for t in drv.LADDER_TIERS}
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'no_signal' and p['choice'] == 'sp500'


def test_finalize_payload_no_change():
    W = ('2021-07-01', '2026-06-05')
    cells = {
        'sp500':       {'status': 'done', 'metrics': {'sharpe': 1.5, 'trades_n': 100}, 'w': W},
        'tier_r1000':  {'status': 'done', 'metrics': {'sharpe': 1.55, 'trades_n': 100}, 'w': W},
        'tier_r3000':  {'status': 'done', 'metrics': {'sharpe': 1.2, 'trades_n': 100}, 'w': W},
        'tier_liquid': {'status': 'done', 'metrics': {'sharpe': 0.9, 'trades_n': 100}, 'w': W},
    }
    p = drv.finalize_payload(cells, current='sp500')
    assert p['verdict_name'] == 'no_change' and p['choice'] == 'sp500'


def test_metrics_to_grid_row():
    m = {'sharpe': 1.2, 'max_dd_pct': 10.0, 'win_rate': 0.5, 'trades_n': 50,
         'sortino': 1.5, 'calmar': 1.0, 'mean_holding_days': 3.0,
         'mean_universe_size': 900.0, 'trade_sha': 'x', 'mode': 'tier',
         'candidate': 'tier_r1000'}
    row = drv.grid_row('tier_r1000', m)
    assert row['name'] == 'tier_r1000' and row['sharpe'] == 1.2
    assert drv.grid_row('sp500', None)['sharpe'] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_ladder_driver.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement** (~250 lines; the tested helpers EXACTLY as below, the orchestration loop per the docstring)

```python
#!/usr/bin/env python3
"""SP-7 Phase B — ladder queue driver.

  seed  — create/extend a run: insert (strategy × 4 tiers) queued cells.
          --strategy SID limits to one strategy (dashboard recompute);
          --arm touches data/.sp7_ladder_armed; builds the membership
          artifact if absent (delegates to scripts/build_tier_membership.py).
  drain — sequentially run queued cells until the queue is empty or TERM.
          Prints '[ladder] DONE' when no queued cells remain.

Resumability: terminal cell writes are atomic per cell; cells found in
status='running' at drain start are reset to 'queued' (a prior window's
SIGTERM landed mid-cell).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

LADDER_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
TIER_PRIORITY = {'sp500': 0, 'tier_liquid': 1, 'tier_r1000': 2, 'tier_r3000': 3}
SLOW_BUDGETS = {
    'S_tr_03_bocpd_change_point': 21600,            # ~3.5h on 591 names
    'S_pairs_trading_jump_diffusion_intraday': 21600,
}
DEFAULT_BUDGET = 7200
RAM_FLOOR_MB = 1800
SENTINEL = ROOT / 'data' / '.sp7_ladder_armed'
GRID_KEYS = ('sharpe', 'max_dd_pct', 'win_rate', 'trades_n', 'sortino',
             'calmar', 'mean_holding_days', 'mean_universe_size')


def budget_for(strategy_id: str) -> int:
    return SLOW_BUDGETS.get(strategy_id, DEFAULT_BUDGET)


def is_degenerate(extremes: dict) -> bool:
    a, b = extremes.get('sp500'), extremes.get('tier_liquid')
    return bool(a and b and a.get('status') == 'done'
                and b.get('status') == 'done'
                and a.get('trade_sha') and a['trade_sha'] == b.get('trade_sha'))


def should_fail_strategy(recent_statuses: list[str]) -> bool:
    return len(recent_statuses) >= 3 and all(
        s == 'error' for s in recent_statuses[-3:])


def grid_row(tier: str, metrics: dict | None) -> dict:
    row = {'name': tier}
    for k in GRID_KEYS:
        row[k] = None if metrics is None else metrics.get(k)
    return row


def mem_available_mb() -> int:
    for line in open('/proc/meminfo'):
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    return 0


def _pg():
    import psycopg2
    return psycopg2.connect(os.environ['POSTGRES_URI'])


# ── seed ────────────────────────────────────────────────────────────────
def cmd_seed(args) -> int:
    pg = _pg()
    run_id = args.run_id or f'ladder-{date.today().strftime("%Y%m%d")}'
    window_start, window_end = args.start, args.end or date.today().isoformat()
    artifact = (ROOT / 'data' /
                f'universe_tier_membership_{run_id}.parquet')
    if not artifact.exists():
        rc = subprocess.run(
            ['python3', 'scripts/build_tier_membership.py',
             '--run-id', run_id, '--start', window_start,
             '--end', window_end], cwd=str(ROOT)).returncode
        if rc != 0:
            print(f'[ladder-seed] membership build failed rc={rc}')
            return 1
    with pg.cursor() as cur:
        if args.strategy:
            sids = [args.strategy]
        else:
            cur.execute("SELECT id FROM strategy_registry "
                        "WHERE status='approved' ORDER BY id")
            sids = [r[0] for r in cur.fetchall()]
        n = 0
        for sid in sids:
            for tier in LADDER_TIERS:
                cur.execute("""
                    INSERT INTO universe_ladder_runs
                      (run_id, strategy_id, tier, status, window_start,
                       window_end, artifact_path)
                    VALUES (%s, %s, %s, 'queued', %s, %s, %s)
                    ON CONFLICT (run_id, strategy_id, tier) DO NOTHING""",
                    (run_id, sid, tier, window_start, window_end,
                     str(artifact)))
                n += cur.rowcount
    pg.commit()
    print(f'[ladder-seed] run={run_id} strategies={len(sids)} new_cells={n}')
    if not args.strategy:
        _redis_set('sp7:ladder:full_run_id', run_id)
    if args.arm:
        SENTINEL.touch()
        print(f'[ladder-seed] armed {SENTINEL}')
    pg.close()
    return 0


# ── drain ───────────────────────────────────────────────────────────────
def run_cell(cell: dict) -> dict:
    """Spawn universe_grid_cli for one cell; return terminal-state fields."""
    t0 = time.time()
    cmd = ['python3', '-m', 'backtest.universe_grid_cli',
           '--strategy', cell['strategy_id'],
           '--start', str(cell['window_start']),
           '--end', str(cell['window_end']),
           '--membership-artifact', cell['artifact_path'],
           '--tier', cell['tier']]
    try:
        res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                             text=True, timeout=budget_for(cell['strategy_id']))
        dur = round(time.time() - t0, 1)
        if res.returncode == 0:
            metrics = json.loads(res.stdout.strip().splitlines()[-1])
            return {'status': 'done', 'metrics': metrics,
                    'trade_sha': metrics.get('trade_sha'),
                    'duration_s': dur, 'stderr_tail': None}
        return {'status': 'error', 'metrics': None, 'trade_sha': None,
                'duration_s': dur,
                'stderr_tail': (res.stderr or '')[-800:]}
    except subprocess.TimeoutExpired:
        return {'status': 'timeout', 'metrics': None, 'trade_sha': None,
                'duration_s': round(time.time() - t0, 1),
                'stderr_tail': f'timeout after {budget_for(cell["strategy_id"])}s'}
    except Exception as e:
        return {'status': 'error', 'metrics': None, 'trade_sha': None,
                'duration_s': round(time.time() - t0, 1),
                'stderr_tail': str(e)[:800]}


def cmd_drain(args) -> int:
    pg = _pg()
    with pg.cursor() as cur:  # mid-kill recovery
        cur.execute("UPDATE universe_ladder_runs SET status='queued' "
                    "WHERE status='running'")
        if cur.rowcount:
            print(f'[ladder] reset {cur.rowcount} stuck running cells')
    pg.commit()

    while True:
        if mem_available_mb() < RAM_FLOOR_MB:
            print(f'[ladder] RAM below floor ({mem_available_mb()}MB) — wait 300s')
            time.sleep(300)
            continue
        with pg.cursor() as cur:
            cur.execute("""
                SELECT id, run_id, strategy_id, tier, window_start,
                       window_end, artifact_path
                  FROM universe_ladder_runs WHERE status='queued'
                 ORDER BY queued_at,
                          CASE tier WHEN 'sp500' THEN 0
                                    WHEN 'tier_liquid' THEN 1
                                    WHEN 'tier_r1000' THEN 2
                                    ELSE 3 END, id
                 LIMIT 1""")
            row = cur.fetchone()
        if row is None:
            _maybe_record_full_run(pg)
            print('[ladder] DONE')
            break
        cell = dict(zip(('id', 'run_id', 'strategy_id', 'tier',
                         'window_start', 'window_end', 'artifact_path'), row))
        # degenerate short-circuit + error policy BEFORE spending compute
        if _pre_skip(pg, cell):
            continue
        with pg.cursor() as cur:
            cur.execute("UPDATE universe_ladder_runs SET status='running', "
                        "started_at=NOW() WHERE id=%s", (cell['id'],))
        pg.commit()
        print(f"[ladder] cell {cell['strategy_id']}/{cell['tier']} "
              f"budget={budget_for(cell['strategy_id'])}s")
        result = run_cell(cell)
        with pg.cursor() as cur:
            cur.execute("""
                UPDATE universe_ladder_runs
                   SET status=%s, metrics=%s::jsonb, trade_sha=%s,
                       duration_s=%s, stderr_tail=%s, finished_at=NOW()
                 WHERE id=%s""",
                (result['status'],
                 json.dumps(result['metrics']) if result['metrics'] else None,
                 result['trade_sha'], result['duration_s'],
                 result['stderr_tail'], cell['id']))
        pg.commit()
        print(f"[ladder] cell {cell['strategy_id']}/{cell['tier']} "
              f"→ {result['status']} ({result['duration_s']}s)")
        _maybe_finalize_strategy(pg, cell['run_id'], cell['strategy_id'])
    pg.close()
    return 0


def _pre_skip(pg, cell) -> bool:
    """Apply degenerate-skip + 3-error policy; True if cell was skipped."""
    with pg.cursor() as cur:
        cur.execute("""SELECT tier, status, trade_sha
                         FROM universe_ladder_runs
                        WHERE run_id=%s AND strategy_id=%s""",
                    (cell['run_id'], cell['strategy_id']))
        cells = {r[0]: {'status': r[1], 'trade_sha': r[2]}
                 for r in cur.fetchall()}
        if cell['tier'] in ('tier_r1000', 'tier_r3000') and is_degenerate(cells):
            cur.execute("""UPDATE universe_ladder_runs
                              SET status='skipped_degenerate', finished_at=NOW()
                            WHERE id=%s""", (cell['id'],))
            pg.commit()
            print(f"[ladder] {cell['strategy_id']}/{cell['tier']} "
                  "skipped_degenerate (extremes identical)")
            _maybe_finalize_strategy(pg, cell['run_id'], cell['strategy_id'])
            return True
        # 3-consecutive-error policy: trailing terminal statuses by finish time
        cur.execute("""SELECT status FROM universe_ladder_runs
                        WHERE run_id=%s AND strategy_id=%s
                          AND status IN ('done','error','timeout')
                        ORDER BY finished_at""",
                    (cell['run_id'], cell['strategy_id']))
        trailing = [r[0] for r in cur.fetchall()]
        if should_fail_strategy(trailing):
            cur.execute("""UPDATE universe_ladder_runs
                              SET status='error',
                                  stderr_tail='strategy failed: 3 consecutive errors',
                                  finished_at=NOW()
                            WHERE id=%s""", (cell['id'],))
            pg.commit()
            _maybe_finalize_strategy(pg, cell['run_id'], cell['strategy_id'])
            return True
    return False


def finalize_payload(cells: dict, current: str) -> dict:
    """PURE verdict→rec-row mapping (unit-tested; the DB glue below stays
    thin). cells: tier -> {'status', 'metrics', 'w': (start, end)}."""
    from backtest.universe_ladder_selection import select_tier
    from backtest import universe_ladder_recs as recs

    window = next(iter(cells.values()))['w']
    metrics_by_tier = {t: (c['metrics'] if c['status'] == 'done' else None)
                       for t, c in cells.items()}
    degenerate = (
        cells.get('sp500', {}).get('status') == 'done'
        and cells.get('tier_liquid', {}).get('status') == 'done'
        and all(cells.get(t, {}).get('status') == 'skipped_degenerate'
                for t in ('tier_r1000', 'tier_r3000')))
    if degenerate:
        choice, verdict_name = current, 'universe-independent'
        rationale = 'sp7b ladder: extremes trade-identical → universe-independent'
    else:
        verdict = select_tier(metrics_by_tier)
        if verdict['verdict'] == 'no_signal':
            choice, verdict_name = current, 'no_signal'
        else:
            choice = verdict['choice']
            verdict_name = 'no_change' if choice == current else 'change'
        rationale = recs.build_rationale(verdict, window=window)
    summary = {'grid': [grid_row(t, metrics_by_tier.get(t))
                        for t in LADDER_TIERS],
               'window': list(window), 'verdict': verdict_name,
               'cell_statuses': {t: c['status'] for t, c in cells.items()},
               'candidate_set': list(LADDER_TIERS)}
    return {'choice': choice, 'verdict_name': verdict_name,
            'rationale': rationale, 'summary': summary}


def _maybe_finalize_strategy(pg, run_id: str, strategy_id: str) -> None:
    """When all 4 cells are terminal → finalize_payload → persist rec, post."""
    from backtest import universe_ladder_recs as recs

    with pg.cursor() as cur:
        cur.execute("""SELECT tier, status, metrics, window_start, window_end
                         FROM universe_ladder_runs
                        WHERE run_id=%s AND strategy_id=%s""",
                    (run_id, strategy_id))
        rows = cur.fetchall()
        cur.execute("""SELECT 1 FROM strategy_universe_recommendations
                        WHERE strategy_id=%s AND candidate_set_id=%s""",
                    (strategy_id, f'sp7b-1-{run_id}'))
        if cur.fetchone():
            return  # already finalized
    cells = {r[0]: {'status': r[1], 'metrics': r[2],
                    'w': (str(r[3]), str(r[4]))} for r in rows}
    if len(cells) < 4 or any(
            c['status'] in ('queued', 'running') for c in cells.values()):
        return
    current = _current_predicate(strategy_id)
    p = finalize_payload(cells, current)
    rec_id = recs.insert_recommendation(
        pg, strategy_id=strategy_id, current_predicate=current,
        candidate_predicate=p['choice'],
        candidate_set_id=f'sp7b-1-{run_id}',
        backtest_summary=p['summary'], rationale=p['rationale'])
    if p['verdict_name'] == 'change':
        msg = recs.format_change_message(strategy_id, current, p['choice'],
                                         p['rationale'], p['summary']['grid'],
                                         rec_id=rec_id)
        recs.post_discord(pg, msg)
    else:
        _queue_summary_line(pg, strategy_id, p['verdict_name'])
    print(f"[ladder] finalized {strategy_id}: {p['verdict_name']} → rec {rec_id}")


def _current_predicate(strategy_id: str) -> str:
    try:
        manifest = json.loads(
            (ROOT / 'src' / 'strategies' / 'manifest.json').read_text())
        ref = (manifest.get('strategies', {}).get(strategy_id, {})
               .get('metadata', {}).get('universe_filter_ref'))
        return ref.rsplit(':', 1)[-1] if ref else 'sp500'
    except Exception:
        return 'sp500'


def _queue_summary_line(pg, sid: str, verdict: str) -> None:
    """Batch non-change verdicts into one Discord summary at drain end —
    accumulate in redis list, flushed by cmd_drain's DONE path."""
    r = _redis()
    if r:
        r.rpush('sp7:ladder:summary_queue', f'{sid}:{verdict}')


def _maybe_record_full_run(pg) -> None:
    r = _redis()
    if not r:
        return
    # flush non-change summary
    items = []
    while True:
        v = r.lpop('sp7:ladder:summary_queue')
        if v is None:
            break
        sid, verdict = v.split(':', 1)
        items.append((sid, verdict))
    if items:
        from backtest import universe_ladder_recs as recs
        recs.post_discord(pg, recs.format_summary_message(items))
    full_run = r.get('sp7:ladder:full_run_id')
    if full_run:
        with pg.cursor() as cur:
            cur.execute("""SELECT count(*) FROM universe_ladder_runs
                           WHERE run_id=%s AND status IN ('queued','running')""",
                        (full_run,))
            if cur.fetchone()[0] == 0:
                r.set('sp7:ladder:last_full_run', date.today().isoformat())
                r.delete('sp7:ladder:full_run_id')
                print(f'[ladder] full run {full_run} complete — '
                      'sp7:ladder:last_full_run updated')


def _redis():
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL',
                                          'redis://localhost:6379'),
                           socket_connect_timeout=3, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _redis_set(k, v):
    r = _redis()
    if r:
        r.set(k, v)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('seed')
    s.add_argument('--run-id')
    s.add_argument('--strategy')
    s.add_argument('--start', default='2021-07-01')
    s.add_argument('--end')
    s.add_argument('--arm', action='store_true')
    d = sub.add_parser('drain')
    args = ap.parse_args()
    return cmd_seed(args) if args.cmd == 'seed' else cmd_drain(args)


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_sp7_ladder_driver.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_universe_ladder.py tests/test_sp7_ladder_driver.py
git commit -m "feat(sp7-phase-b): ladder queue driver (seed/drain, extremes-first, budgets, resume)"
```

---

### Task 11: Nightly window wrapper + systemd user units

**Files:**
- Create: `scripts/overnight_ladder.sh`
- Create: `docs/sp7-ladder.service`, `docs/sp7-ladder.timer`
- Test: `tests/test_sp7_overnight_ladder_sh.py` (static assertions on the script text — house pattern for untestable bash)

- [ ] **Step 1: Write `scripts/overnight_ladder.sh`** (mirrors `scripts/overnight_backfill.sh` verbatim conventions: sentinel gate, inline .env grep-source, budget-to-13:00-UTC, TERM, rc=124 = benign)

```bash
#!/usr/bin/env bash
# SP-7 Phase B — overnight-window wrapper for the tier-ladder queue.
# Armed by: python3 scripts/run_universe_ladder.py seed --arm
#   (or check_ladder_saturday.py / the :7870 recompute button)
# Runs nightly (timer 01:00 UTC) until the queue drains, then disarms.
set -euo pipefail
cd /root/openclaw
ARMED=data/.sp7_ladder_armed
LOG=logs/sp7_ladder_$(date -u +%F).log
[ -f "$ARMED" ] || { echo "[sp7-ladder] not armed, exiting"; exit 0; }

# Backfill has priority on the box — never share a window with it.
[ -f data/.sp7_backfill_armed ] && {
  echo "[sp7-ladder] backfill armed — yielding tonight" | tee -a "$LOG"; exit 0; }

set -a; . <(grep -E '^(POSTGRES_URI|REDIS_URL)' .env | sed 's/\r$//'); set +a

# Seconds until 13:00 UTC — the window close (clears the EDGAR/premarket band).
now=$(date -u +%s); close=$(date -u -d "13:00" +%s)
[ "$close" -le "$now" ] && close=$(date -u -d "tomorrow 13:00" +%s)
budget=$(( close - now ))
echo "[sp7-ladder] window budget ${budget}s" | tee -a "$LOG"

set +e
timeout --signal=TERM "$budget" nice -n 19 \
    python3 scripts/run_universe_ladder.py drain >> "$LOG" 2>&1
rc=$?
set -e

if [ $rc -eq 0 ] && grep -q "\[ladder\] DONE" "$LOG"; then
  rm -f "$ARMED"
  echo "[sp7-ladder] COMPLETE — disarmed" | tee -a "$LOG"
elif [ $rc -eq 124 ]; then
  echo "[sp7-ladder] window closed (SIGTERM at 13:00 UTC) — resumes next night" | tee -a "$LOG"
else
  echo "[sp7-ladder] driver rc=$rc — investigate $LOG" | tee -a "$LOG"
fi
```

- [ ] **Step 2: Write the units** (install happens in the runbook, NOT here)

`docs/sp7-ladder.service`:
```ini
[Unit]
Description=SP-7 Phase B tier-ladder overnight window (armed via data/.sp7_ladder_armed)

[Service]
Type=oneshot
WorkingDirectory=/root/openclaw
ExecStart=/bin/bash /root/openclaw/scripts/overnight_ladder.sh
```

`docs/sp7-ladder.timer`:
```ini
[Unit]
Description=Nightly 01:00 UTC SP-7 tier-ladder window

[Timer]
# Mon-Fri 01:00 UTC; window closes 13:00 UTC (mirrors sp7-overnight-backfill).
# Saturday EXCLUDED: the Sat 12:00 UTC weekend stack owns the box (OOM lesson).
OnCalendar=Mon-Fri 01:00 UTC
Persistent=false

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Static test**

```python
"""SP-7 Phase B Task 11 — wrapper invariants (static text assertions)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / 'scripts' / 'overnight_ladder.sh').read_text()


def test_sentinel_gate_and_disarm():
    assert '.sp7_ladder_armed' in SH
    assert 'rm -f "$ARMED"' in SH


def test_backfill_priority_guard():
    assert '.sp7_backfill_armed' in SH and 'yielding' in SH


def test_window_discipline():
    assert 'timeout --signal=TERM' in SH and 'nice -n 19' in SH
    assert '13:00' in SH and 'rc -eq 124' in SH.replace('$', '')


def test_done_grep_matches_driver_output():
    assert '\\[ladder\\] DONE' in SH
```

- [ ] **Step 4: Run + shellcheck-by-bash**

Run: `python3 -m pytest tests/test_sp7_overnight_ladder_sh.py -v && bash -n scripts/overnight_ladder.sh`
Expected: 4 PASS; bash syntax OK.

- [ ] **Step 5: Commit**

```bash
git add scripts/overnight_ladder.sh docs/sp7-ladder.service docs/sp7-ladder.timer tests/test_sp7_overnight_ladder_sh.py
git commit -m "feat(sp7-phase-b): nightly ladder window wrapper + systemd user units"
```

---

### Task 12: B3 union-N threshold proposals + adoption hook

**Files:**
- Create: `src/execution/universe_threshold_proposals.py`
- Modify: `src/strategies/lifecycle_universe_adoption.py` (post-commit best-effort hook)
- Test: `tests/test_sp7_threshold_proposals.py`

- [ ] **Step 1: Write the failing tests** (pure math + supersede SQL contract via mocks)

```python
"""SP-7 Phase B Task 12 — √ln(N) union-N proposal math."""
from __future__ import annotations
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.universe_threshold_proposals import breadth_factor, propose_values


def test_factor_identity_at_sp500():
    assert breadth_factor(503, 503) == 1.0


def test_factor_curve():
    f = breadth_factor(5113, 503)
    assert abs(f - math.sqrt(math.log(5113) / math.log(503))) < 1e-12
    assert 1.15 < f < 1.20  # spec's ≈×1.17


def test_factor_guards():
    assert breadth_factor(0, 503) == 1.0      # degenerate → no scaling
    assert breadth_factor(503, 0) == 1.0
    assert breadth_factor(1, 1) == 1.0


def test_propose_values_clamped():
    bases = {'LOW_VOL': 3.0, 'TRANSITIONING': 4.0,
             'HIGH_VOL': 9.5, 'CRISIS': 6.0}
    out = propose_values(bases, factor=1.17)
    assert abs(out['LOW_VOL'] - 3.51) < 0.01
    assert out['HIGH_VOL'] == 10.0  # clamped at DB CHECK ceiling
    assert all(1.0 <= v <= 10.0 for v in out.values())
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_sp7_threshold_proposals.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""SP-7 Phase B B3 — breadth-scaled min_cumulative_sharpe proposals.

Union-N rule (spec §6): on each adoption event, N_union = |∪ adopted-universe
memberships across all registry-approved strategies| on the LATEST snapshot
(un-adopted strategies contribute sp500). factor = √(ln N_union / ln N_sp500).
proposed = current_base × factor per regime, clamped [1.0, 10.0].
Proposals only — NEVER writes regime_sizer_params (the :3000 Apply button does,
through the existing PUT validation).
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def breadth_factor(n_union: int, n_sp500: int) -> float:
    if n_union <= 1 or n_sp500 <= 1:
        return 1.0
    return math.sqrt(math.log(n_union) / math.log(n_sp500))


def propose_values(bases: dict[str, float], *, factor: float) -> dict[str, float]:
    return {r: max(1.0, min(10.0, round(b * factor, 2)))
            for r, b in bases.items()}


def _resolver():
    from src.strategies._db_adapters import PostgresMetadataDB, ParquetCoverage
    from src.strategies.universe_resolver import UniverseResolver

    def manifest_loader():
        return json.loads(
            (ROOT / 'src' / 'strategies' / 'manifest.json').read_text())

    return UniverseResolver(
        db=PostgresMetadataDB(os.environ['POSTGRES_URI']),
        coverage=ParquetCoverage(), manifest_loader=manifest_loader)


def compute_union_n(pg, as_of: date | None = None) -> tuple[int, int]:
    """(N_union across approved strategies' current predicates, N_sp500)."""
    from src.strategies.universe_default import sp500
    from src.strategies.universe_resolver import MockResolver
    from src.strategies._db_adapters import PostgresMetadataDB, ParquetCoverage

    as_of = as_of or date.today()
    with pg.cursor() as cur:
        cur.execute("SELECT id FROM strategy_registry WHERE status='approved'")
        sids = [r[0] for r in cur.fetchall()]
    res = _resolver()
    union: set[str] = set()
    for sid in sids:
        try:
            union.update(res.resolve(sid, as_of))
        except Exception as e:
            print(f'[b3] resolve failed for {sid}: {e} — contributes nothing')
    base = MockResolver(db=PostgresMetadataDB(os.environ['POSTGRES_URI']),
                        coverage=ParquetCoverage(), predicate=sp500)
    n_sp500 = len(base.resolve('_sp500_baseline', as_of))
    return len(union), n_sp500


def compute_and_write(trigger: str) -> int:
    """Supersede pending proposals, write 4 fresh ones. Returns count."""
    import psycopg2
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        n_union, n_sp500 = compute_union_n(pg)
        factor = breadth_factor(n_union, n_sp500)
        with pg.cursor() as cur:
            cur.execute("""SELECT regime_state, min_cumulative_sharpe,
                                  liquidity_param, min_signal_notional_usd
                             FROM regime_sizer_params""")
            rows = {r[0]: {'min_cumulative_sharpe': float(r[1]),
                           'liquidity_param': float(r[2]) if r[2] is not None else None,
                           'min_signal_notional_usd': float(r[3]) if r[3] is not None else None}
                    for r in cur.fetchall()}
            bases = {r: v['min_cumulative_sharpe'] for r, v in rows.items()}
            proposed = propose_values(bases, factor=factor)
            cur.execute("""UPDATE universe_threshold_proposals
                              SET status='superseded', decided_at=NOW(),
                                  decided_by=%s,
                                  decision_reason='auto-superseded by newer proposal'
                            WHERE status='pending'""", (f'sp7b:{trigger}',))
            n = 0
            for regime in REGIMES:
                if regime not in bases:
                    continue
                if abs(proposed[regime] - bases[regime]) < 0.01:
                    continue  # no-op proposal adds noise
                cur.execute("""
                    INSERT INTO universe_threshold_proposals
                      (proposer, regime_state, current_row,
                       proposed_min_cumulative_sharpe, basis, status)
                    VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, 'pending')""",
                    (f'sp7b:{trigger}', regime, json.dumps(rows[regime]),
                     proposed[regime],
                     json.dumps({'n_union': n_union, 'n_sp500': n_sp500,
                                 'factor': round(factor, 4),
                                 'trigger': trigger})))
                n += 1
        pg.commit()
        print(f'[b3] trigger={trigger} N_union={n_union} N_sp500={n_sp500} '
              f'factor={factor:.4f} proposals={n}')
        return n
    finally:
        pg.close()


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'src'))
    sys.exit(0 if compute_and_write(
        trigger=sys.argv[1] if len(sys.argv) > 1 else 'manual') >= 0 else 1)
```

- [ ] **Step 4: Add the post-adopt hook** in `src/strategies/lifecycle_universe_adoption.py` — AFTER the existing commit + `os.rename` two-phase completes successfully (locate the end of `adopt_universe_recommendation`; the hook must be best-effort and NEVER fail adoption):

```python
    # SP-7 Phase B B3: adoption changes the union breadth → refresh proposals.
    # DETACHED, fire-and-forget: BOTH adoption entrypoints run this module
    # inside a 30s execFileSync window (Discord ✅ → bot.js → :7870 → adopt;
    # dashboard button → :7870 → adopt). A synchronous union-resolve over all
    # 67 approved strategies (~67 fresh psycopg2 conns + fetches + coverage
    # load) can cross 30s under load — try/except does NOT protect against
    # the EXTERNAL timeout kill, which would make every adoption look failed
    # to the operator even though it committed. Popen + return immediately.
    try:
        import subprocess as _sp
        _sp.Popen(
            [sys.executable, '-m', 'src.execution.universe_threshold_proposals',
             f'adoption:{rec_id}'],
            cwd=str(Path(__file__).resolve().parents[2]),
            start_new_session=True,
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    except Exception as e:  # pragma: no cover
        print(f'[adopt] B3 refresh spawn failed (non-fatal): {e}')
```

(Verify `sys` and `Path` are already imported in lifecycle_universe_adoption.py — grounding says the module uses both; add imports if not. The module's `__main__` already takes the trigger as argv[1].)

- [ ] **Step 4b: Time the union-resolve on the live box** (informational guard):

Run: `set -a; . <(grep -E '^POSTGRES_URI' .env); set +a; time python3 -m src.execution.universe_threshold_proposals manual-timing-check`
Record the wall time in the task report. (Detached spawn means this no longer gates adoption, but if it exceeds ~120s flag it for the reviewer — the :7870 recompute button seeding path shares some of this machinery.)

- [ ] **Step 5: Run tests + the adoption module's existing tests**

Run: `python3 -m pytest tests/test_sp7_threshold_proposals.py -v && grep -rl "lifecycle_universe_adoption" tests/ | xargs -r python3 -m pytest -v`
Expected: new 5 PASS; existing adoption tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/execution/universe_threshold_proposals.py src/strategies/lifecycle_universe_adoption.py tests/test_sp7_threshold_proposals.py
git commit -m "feat(sp7-phase-b): B3 union-N threshold proposals + post-adopt refresh hook"
```

---

### Task 13: :3000 dashboard — proposal endpoints + Conviction Gates UI

**Files:**
- Modify: `src/channels/api/server.js` (johnbot :3000 — the 10k-line monolith; THREE inline edit sites)
- Test: `tests/test_sp7_b3_endpoints.sh` → use a node syntax check + curl smoke in the runbook instead (this server has no JS test harness; follow house precedent: endpoints verified by curl post-restart)

- [ ] **Step 1: Add GET endpoint** — directly AFTER the existing `app.put('/api/config/regime-sizing/:regime', ...)` handler (≈line 783):

```javascript
// SP-7 Phase B B3 — pending √ln(N) threshold proposals (read-only list)
app.get('/api/universe-threshold-proposals', async (req, res) => {
  try {
    const r = await dbQuery(
      `SELECT id, proposed_at, proposer, regime_state, current_row,
              proposed_min_cumulative_sharpe, basis, status
         FROM universe_threshold_proposals
        WHERE status = 'pending'
        ORDER BY CASE regime_state WHEN 'LOW_VOL' THEN 1
                 WHEN 'TRANSITIONING' THEN 2 WHEN 'HIGH_VOL' THEN 3
                 ELSE 4 END`);
    res.json({ proposals: r.rows });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// SP-7 Phase B B3 — apply ONE proposal through the same validation as the
// direct PUT (value into regime_sizer_params, proposal stamped approved).
app.post('/api/universe-threshold-proposals/:id/apply', async (req, res) => {
  const id = String(req.params.id || '');
  if (!/^\d+$/.test(id)) return res.status(400).json({ error: 'id must be numeric' });
  try {
    const p = await dbQuery(
      `SELECT regime_state, proposed_min_cumulative_sharpe
         FROM universe_threshold_proposals
        WHERE id = $1 AND status = 'pending'`, [id]);
    if (p.rowCount === 0) return res.status(404).json({ error: 'no pending proposal with that id' });
    const regime = p.rows[0].regime_state;
    const v = parseFloat(p.rows[0].proposed_min_cumulative_sharpe);
    if (!isFinite(v) || v < 1.0 || v > 10.0) {
      return res.status(400).json({ error: 'proposed value outside [1.0, 10.0]' });
    }
    const upd = await dbQuery(
      `UPDATE regime_sizer_params SET min_cumulative_sharpe = $2,
              updated_at = NOW()
        WHERE regime_state = $1 RETURNING *`, [regime, v]);
    if (upd.rowCount === 0) return res.status(404).json({ error: `regime ${regime} not found` });
    await dbQuery(
      `UPDATE universe_threshold_proposals
          SET status = 'approved', decided_at = NOW(),
              decided_by = 'operator:dashboard',
              applied_row = $2::jsonb
        WHERE id = $1`, [id, JSON.stringify(upd.rows[0])]);
    res.json({ ok: true, regime, applied: v });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// SP-7 Phase B B3 — reject a pending proposal
app.post('/api/universe-threshold-proposals/:id/reject', async (req, res) => {
  const id = String(req.params.id || '');
  if (!/^\d+$/.test(id)) return res.status(400).json({ error: 'id must be numeric' });
  try {
    const r = await dbQuery(
      `UPDATE universe_threshold_proposals
          SET status = 'rejected', decided_at = NOW(),
              decided_by = 'operator:dashboard'
        WHERE id = $1 AND status = 'pending'`, [id]);
    if (r.rowCount === 0) return res.status(404).json({ error: 'no pending proposal with that id' });
    res.json({ ok: true });
  } catch (err) { res.status(500).json({ error: err.message }); }
});
```

- [ ] **Step 2: Extend the Conviction Gates JS** — inside `_loadSharpeGates()` (≈line 7916), AFTER the existing sliders render, append a proposals fetch + per-regime chip. Locate the end of the function body and add:

```javascript
  // SP-7 Phase B B3: surface pending √ln(N) proposals next to the sliders
  try {
    const pr = await fetch('/api/universe-threshold-proposals').then(r => r.json());
    const props = Array.isArray(pr.proposals) ? pr.proposals : [];
    for (const p of props) {
      const card = host.querySelector(`[data-regime="${p.regime_state}"]`);
      if (!card) continue;
      const chip = document.createElement('div');
      chip.style.cssText = 'margin-top:4px;font-size:11px;opacity:0.85';
      const cur = (p.current_row && p.current_row.min_cumulative_sharpe) || '?';
      const basis = p.basis || {};
      chip.innerHTML =
        `proposal: ${cur} → <b>${p.proposed_min_cumulative_sharpe}</b>` +
        ` <span title="N_union=${basis.n_union} N_sp500=${basis.n_sp500}">(×${basis.factor})</span>` +
        ` <button data-apply="${p.id}">Apply</button>` +
        ` <button data-reject="${p.id}">✗</button>`;
      chip.querySelector('[data-apply]').addEventListener('click', async () => {
        const r = await fetch(`/api/universe-threshold-proposals/${p.id}/apply`, { method: 'POST' });
        chip.textContent = r.ok ? '✓ applied' : '✗ apply failed';
        if (r.ok) _loadSharpeGates();
      });
      chip.querySelector('[data-reject]').addEventListener('click', async () => {
        await fetch(`/api/universe-threshold-proposals/${p.id}/reject`, { method: 'POST' });
        chip.remove();
      });
      card.appendChild(chip);
    }
  } catch (e) { /* proposals are progressive enhancement — never break gates */ }
```

NOTE: confirm the slider card actually carries `data-regime="${r.state}"` (grounding says it does, server.js:≈7925); if the attribute is on a child, adjust the selector.

- [ ] **Step 3: Syntax check**

Run: `node --check src/channels/api/server.js`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add src/channels/api/server.js
git commit -m "feat(sp7-phase-b): B3 proposal endpoints + Conviction Gates proposal chips (:3000)"
```

(Live verification — restart johnbot + curl — happens in the runbook; this server is the live Discord bot host, restarts are operator-gated.)

---

### Task 14: 12th-Saturday sentinel + weekend step-8 re-point

**Files:**
- Create: `scripts/check_ladder_saturday.py`
- Modify: `src/maintenance/weekend_saturday.sh` (step 8 block only)
- Test: `tests/test_sp7_saturday_sentinel.py`

- [ ] **Step 1: Write the failing tests**

```python
"""SP-7 Phase B Task 14 — 12-week sentinel decision logic."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import check_ladder_saturday as cls


def test_due_when_never_run():
    assert cls.is_due(None, today=date(2026, 6, 6)) is True


def test_due_at_84_days():
    assert cls.is_due('2026-03-14', today=date(2026, 6, 6)) is True   # 84d
    assert cls.is_due('2026-03-15', today=date(2026, 6, 6)) is False  # 83d


def test_garbage_value_is_due():
    assert cls.is_due('not-a-date', today=date(2026, 6, 6)) is True
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: Implement**

```python
#!/usr/bin/env python3
"""SP-7 Phase B B4 — 12th-Saturday ladder sentinel.

Runs inside weekend_saturday.sh step 8 (the slot the legacy universe-recs
invocation vacated). If ≥12 weeks (84 days) since the last FULL ladder run
(redis sp7:ladder:last_full_run), seed a full run + arm the nightly window.
Compute happens in the following nightly windows, NEVER on Saturday.

Usage: python3 scripts/check_ladder_saturday.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEEKS_12_DAYS = 84
KEY = 'sp7:ladder:last_full_run'


def is_due(last_iso: str | None, *, today: date) -> bool:
    if not last_iso:
        return True
    try:
        last = date.fromisoformat(last_iso)
    except ValueError:
        return True
    return (today - last).days >= WEEKS_12_DAYS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    last = None
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL',
                                          'redis://localhost:6379'),
                           socket_connect_timeout=3, decode_responses=True)
        last = r.get(KEY)
    except Exception as e:
        print(f'[ladder-saturday] redis unavailable ({e}) — treating as due')
    due = is_due(last, today=date.today())
    print(f'[ladder-saturday] last_full_run={last} due={due}')
    if not due or args.dry_run:
        return 0
    rc = subprocess.run(
        ['python3', 'scripts/run_universe_ladder.py', 'seed', '--arm'],
        cwd=str(ROOT)).returncode
    print(f'[ladder-saturday] seed rc={rc}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Re-point step 8** in `src/maintenance/weekend_saturday.sh`. Replace exactly these two lines:

```bash
step "8/8 universe-recs (gated)"
node src/agent/curators/run_mastermind.js --mode universe-recs 2>&1 | tee -a "$LOG" || step "WARN universe-recs rc=$?"
```

with:

```bash
step "8/8 universe-ladder sentinel (SP-7 Phase B — replaced legacy universe-recs 2026-06-06)"
nice -n 19 python3 scripts/check_ladder_saturday.py 2>&1 | tee -a "$LOG" || step "WARN ladder-sentinel rc=$?"
```

(The legacy mode + its gate stay in the codebase for Phase D removal; `OPENCLAW_UNIVERSE_RECS` is already commented out of `.env`, and `doctor.py:1375` / `universe_recs_health.py:29` self-skip with it unset.)

- [ ] **Step 5: Run tests + bash syntax**

Run: `python3 -m pytest tests/test_sp7_saturday_sentinel.py -v && bash -n src/maintenance/weekend_saturday.sh`
Expected: 3 PASS; syntax OK.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_ladder_saturday.py src/maintenance/weekend_saturday.sh tests/test_sp7_saturday_sentinel.py
git commit -m "feat(sp7-phase-b): 12th-Saturday ladder sentinel + weekend step-8 re-point"
```

---

### Task 15: :7870 control room — ladder summary + per-strategy Recompute

**Files:**
- Modify: `src/channels/dashboard/server.js` (after the existing `POST /api/universe-recs/:id/:action` at ≈line 449)
- Modify: `src/channels/dashboard/public/index.html` (new tile next to the universe-recs section)

- [ ] **Step 1: Add the endpoints** (match THIS server's conventions: `query()` helper, `execFileSync` with `cwd=repoRoot`, module style `src.`-prefixed for python -m, plain script path here):

```javascript
// SP-7 Phase B — ladder run summary (latest run's cells, grouped)
app.get('/api/universe-ladder', async (req, res) => {
  try {
    const latest = await query(
      `SELECT run_id FROM universe_ladder_runs
        ORDER BY queued_at DESC LIMIT 1`);
    if (latest.rows.length === 0) return res.json({ run_id: null, cells: [] });
    const runId = latest.rows[0].run_id;
    const cells = await query(
      `SELECT strategy_id, tier, status, duration_s,
              metrics->>'sharpe' AS sharpe, finished_at
         FROM universe_ladder_runs WHERE run_id = $1
        ORDER BY strategy_id, CASE tier WHEN 'sp500' THEN 0
                 WHEN 'tier_r1000' THEN 1 WHEN 'tier_r3000' THEN 2
                 ELSE 3 END`, [runId]);
    const counts = await query(
      `SELECT status, count(*)::int AS n FROM universe_ladder_runs
        WHERE run_id = $1 GROUP BY status`, [runId]);
    res.json({ run_id: runId, counts: counts.rows, cells: cells.rows });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// SP-7 Phase B — per-strategy recompute (spec §7.2): enqueue cells + arm
app.post('/api/universe-ladder/:strategyId/recompute', async (req, res) => {
  const sid = String(req.params.strategyId || '');
  if (!/^[A-Za-z0-9_]+$/.test(sid)) return res.status(400).json({ error: 'bad strategy id' });
  try {
    const known = await query(
      `SELECT 1 FROM strategy_registry WHERE id = $1 AND status = 'approved'`, [sid]);
    if (known.rows.length === 0) return res.status(404).json({ error: `${sid} is not a registry-approved strategy` });
    const repoRoot = path.resolve(__dirname, '../../..');
    const { execFileSync } = require('node:child_process');
    execFileSync('python3',
      ['scripts/run_universe_ladder.py', 'seed', '--strategy', sid, '--arm'],
      { cwd: repoRoot, stdio: 'pipe', timeout: 120000 });
    res.status(202).json({ ok: true, strategy_id: sid,
      note: 'cells enqueued + window armed; runs in the next nightly window (01:00–13:00 UTC Mon–Fri)' });
  } catch (err) { res.status(500).json({ error: err.message }); }
});
```

NOTE: seed builds the membership artifact on first call (can take minutes) — hence the 120s timeout; if the artifact for today's run-id already exists, seeding is sub-second. Verify `path` is already required at the top of this server (it is — used by the adopt shell-out).

- [ ] **Step 2: Add the tile** in `public/index.html` — copy the universe-recs section's structure (≈line 132-548; `table()`/`pill()`/`fetchJSON()` helpers exist). Add a "Universe Ladder" section that renders `GET /api/universe-ladder` counts + a per-strategy text input with a Recompute button POSTing to `/api/universe-ladder/<sid>/recompute`, displaying the JSON response. Keep it minimal and in the existing visual style.

- [ ] **Step 3: Syntax check**

Run: `node --check src/channels/dashboard/server.js`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add src/channels/dashboard/server.js src/channels/dashboard/public/index.html
git commit -m "feat(sp7-phase-b): :7870 ladder summary + per-strategy recompute button"
```

---

### Task 16: Runbook

**Files:**
- Create: `docs/sp7-phase-b-runbook.md`

- [ ] **Step 1: Write the runbook** — orders ALL activation steps; nothing in Tasks 1–15 changed live behavior by itself:

```markdown
# SP-7 Phase B activation runbook (operator-gated)

Pre-flight: weekend stack NOT running (check `systemctl list-timers`),
`data/.sp7_backfill_armed` absent, branch merged to the live branch.

1. MERGE + MIGRATE
   - merge feat/sp7-phase-b-tier-ladder into the live branch; `git push`
   - `npm run db:migrate`
   - VERIFY (runner has no applied-tracking): both tables exist:
     `python3 -c "import psycopg2,os;c=psycopg2.connect(os.environ['POSTGRES_URI']).cursor();c.execute('SELECT 1 FROM universe_ladder_runs LIMIT 0');c.execute('SELECT 1 FROM universe_threshold_proposals LIMIT 0');print('ok')"`

2. B0 REPAIR (nights or operator-approved off-window; ~1–3h total)
   - dry-run one month first:
     `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 python3 scripts/sp7_b0_repair_metadata.py --months --start 2023-06-01 --end 2023-06-30 --dry-run`
   - full: `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 nice -n 19 python3 scripts/sp7_b0_repair_metadata.py --months --dailies --resume`
   - ACCEPTANCE:
     a. `python3 -m system_checks --check universe_tier_coherence` → PASS
     b. spot SQL: AAPL/MSFT/NVDA/JPM in_r1000 at 2021-07-31 / 2023-06-30 / 2025-06-30 (the probe covers this)
     c. per-month in_sp500 ≈ 500±15; r3000 = min(3000, ranked-pool); any month r3000 < 2800 → investigate BEFORE GO
     d. clamp live-parity: next engine compute logs kept≈591 (unchanged)

3. DASHBOARD RESTART (adoption paths currently 404)
   - `systemctl restart fundjohn-dashboard.service`  (or --user, match the unit)
   - VERIFY: `curl -s localhost:7870/api/universe-recs | head -c 200` → JSON not 404
   - restart johnbot (picks up :3000 B3 endpoints):
     `systemctl --user restart johnbot.service` ; verify
     `curl -s localhost:3000/api/universe-threshold-proposals` → JSON

4. LEGACY CLEANUP
   - `python3 scripts/supersede_legacy_universe_recs.py` → "tagged 58"

5. LADDER UNITS
   - `cp docs/sp7-ladder.service docs/sp7-ladder.timer ~/.config/systemd/user/`
   - `systemctl --user daemon-reload && systemctl --user enable --now sp7-ladder.timer`
   - VERIFY: `systemctl --user list-timers | grep sp7-ladder` → next Mon-Fri 01:00 UTC

6. SINGLE-STRATEGY SMOKE (before trusting the unattended loop)
   - pick a fast fixed-ticker strategy (e.g. S_fomc_presell_spy_long):
     `python3 scripts/run_universe_ladder.py seed --strategy <sid> --arm`
     `nice -n 19 python3 scripts/run_universe_ladder.py drain`  (foreground;
      minutes — extremes run, middles skip degenerate)
   - VERIFY before proceeding: rec row exists with candidate_set_id
     `sp7b-1-<run_id>` and verdict universe-independent/no_change;
     `[ladder] DONE` printed; no stuck running cells
     (single-strategy seeds never set sp7:ladder:full_run_id, so this does
      NOT mark the 12-week cadence as satisfied)

7. FULL SEED + ARM (after B0 acceptance AND smoke ONLY)
   - `python3 scripts/run_universe_ladder.py seed --arm`
     (builds membership artifact ~minutes; seeds 67×4 cells; arms sentinel)
   - sanity: artifact JSON sidecar n_series — sp500≈350–500, tier_liquid≈3300–5100 per month
   - first cells run tonight 01:00 UTC; watch `logs/sp7_ladder_<date>.log`

8. NIGHTLY WATCH (3–10 nights)
   - recs appear in #universe-recs as strategies finalize (changes only;
     no-change verdicts batch at drain end)
   - adopt via ✅ reaction or :7870 buttons; each adoption refreshes
     B3 proposals (visible next to Conviction Gates sliders on :3000)
   - `[ladder] COMPLETE — disarmed` in the log = full run done;
     redis `sp7:ladder:last_full_run` set (12th-Saturday cadence starts)

ABORT: remove data/.sp7_ladder_armed (stops next window; running cell
finishes or dies at 13:00 TERM; cells resume on re-arm). B0 is idempotent
and resumable; its UPDATEs are value-repairs, not deletions.
```

- [ ] **Step 2: Commit**

```bash
git add docs/sp7-phase-b-runbook.md
git commit -m "docs(sp7-phase-b): activation runbook"
```

---

### Task 17: Full regression + lint + final review prep

- [ ] **Step 1: Full pytest sweep** (sequential — 2-core box):

Run: `set -a; . <(grep -E '^(POSTGRES_URI|REDIS_URL)' .env); set +a; nice -n 19 python3 -m pytest tests/ -x -q 2>&1 | tail -20`
Expected: all green except the 3 documented pre-existing `test_intraday_hmm` failures (LRN-20260604-002) — anything else new is a regression to fix before review.

- [ ] **Step 2: Lints**

Run: `python3 scripts/lint_universe_predicates.py && node --check src/channels/api/server.js && node --check src/channels/dashboard/server.js && bash -n scripts/overnight_ladder.sh && bash -n src/maintenance/weekend_saturday.sh`
Expected: all clean.

- [ ] **Step 3: Diff audit** — confirm NO live-behavior change pre-runbook:

Run: `git diff feat/sp6-phase-a-eod-open-execution...HEAD --stat`
Verify: no changes to `src/execution/engine.py`, `regime_blended_sizer*`, collector, or `.env`; `weekend_saturday.sh` step 8 re-point is the ONLY live-path edit (and it degrades to a no-op print when redis says not-due... it seeds at first run — confirm operator wants first-seed via runbook step 6 instead: the sentinel script's seed path only fires when `last_full_run` is unset, which is exactly the bootstrap; document in the review notes).

- [ ] **Step 4: Commit any fixes, then request review** per superpowers:requesting-code-review (whole-branch).

---

## Self-review (done at plan-write time)

- **Spec coverage:** B0→Task 3+4; B1 predicates→1, precompute→5, resolver→6, CLI→7, queue→10, window→11; B2 selection→8, recs/Discord/supersede→9; B3→12+13; B4 sentinel→14, button→15; runbook/acceptance→16; regression→17. Options-variant: deferred by spec (no task — correct). Mint-time: Phase D (no task — correct).
- **Known checks flagged inline:** Task 13 `data-regime` selector check, Task 15 artifact-build latency, Task 4 run_one return-shape check.
- **Type consistency:** `LADDER_TIERS` tuple defined in BOTH `universe_ladder_selection.py` (canonical) and `run_universe_ladder.py`/`build_tier_membership.py` (local copies for import-weight reasons) — implementer may import from the selection module where convenient; keep values identical. `trade_sha` lives in `universe_grid_cli` (driver reads it from CLI output, never recomputes).
- **Numbers:** migrations 131/132 (re-verify free at Task 2); budgets 7200/21600; RAM floor 1800MB; window 2021-07-01→run-start; ΔSharpe 0.10; MIN_TRADES 30; 84 days.
