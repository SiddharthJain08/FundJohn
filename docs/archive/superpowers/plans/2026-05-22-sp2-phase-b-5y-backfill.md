# SP-2 Phase B: 5-Year Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL — use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Backfill `prices.parquet` to 5y × ~3000 most-liquid US equities and write monthly `ticker_metadata_snapshots` rows back to 2021-05-22, so Phase C's universe-recs Mastermind has point-in-time correct historical metadata to evaluate predicates against.

**Architecture:** Idempotent driver `scripts/backfill_universe_5y.py` runs a stage → validate → promote loop per `(target, chunk_key)`. Redis checkpoints + Postgres `backfill_audit` table guarantee resumability and forensic auditability. `pyarrow.parquet.write_to_dataset(..., existing_data_behavior='delete_matching')` is the **only** documented exception to the master-parquet append-only invariant — restricted to PROMOTE step on zero-existing partitions, with v2+ recovery gated by `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1` and `data_quarantine` supersede semantics. Quarantine filter wired into every parquet consumer so bad rows that slip past validation become structurally invisible without `DELETE`.

**Tech Stack:** Python 3.13 (driver, validators, builders, doctor, system_checks); Node.js (collector.js quarantine subprocess shim, dashboard tiles); PostgreSQL (migration 115); Redis (chunk checkpoints); pytest for unit tests; nohup-driven background invocation (no new systemd timer).

**Spec:** `docs/superpowers/specs/2026-05-22-sp2-phase-b-5y-backfill-design.md`

**Branch:** `feat/sp2-phase-b-5y-backfill` (off main, after `feat/sp2-phase-a-universe-machinery` / PR #8 merges)

**Acceptance:**
- `ticker_metadata_snapshots` contains monthly rows back to 2021-06-01 for ~3000 tickers (~180k rows ± 10%).
- `prices.parquet` extended from ~500 to ~3000 tickers × 5y.
- `data_quarantine` filter wired into all five backtest engines + resolver + collector parquet read path.
- Doctor + system_checks expose backfill progress and history depth.
- Operator runbook (`docs/sp2-backfill-runbook.md`) covers preflight, kickoff, monitoring, quarantine recovery, full rollback.

---

## ⚠️ Codebase conventions (verified 2026-05-22)

Same substitutions as Phase A apply. Repeated here for the implementer's reference:

| In the plan | Use instead | Source |
|---|---|---|
| `DATABASE_URL` | `POSTGRES_URI` | `src/maintenance/doctor.py` `REQUIRED_ENV` |
| separate test DB | `POSTGRES_URI` + `source_tag='test_<uuid>'` cleanup-after pattern | No `conftest.py`; no test DB |
| `psycopg` (v3) | `psycopg2` | only `psycopg2-binary` installed |
| `psql -f migration.sql` | `node -e "require('./src/database/postgres').migrate().then(...)"` | `psql` not installed |
| `freezegun.freeze_time` | `monkeypatch.setattr('<module>.date', _FrozenDate)` | freezegun not installed |
| `@check(tag='x', name='y')` | `@check(name='y', tags=['x'])` | `src/system_checks/registry.py:23` |
| System check files | `src/system_checks/checks/<name>.py` (NOT `check_<name>.py`) | Phase A confirmed convention |
| System check return | `(Status, str)` tuple | `src/system_checks/runner.py` |
| Test imports for `src/strategies/*` | `from strategies.X import Y` with `sys.path.insert(0, ROOT/'src')` OR `from src.strategies.X import Y` | match nearby existing tests |

**Additional Phase B-specific conventions:**

| In the plan | Use instead | Source |
|---|---|---|
| `from src.pipeline.rate_limiter import get_rate_limiter` | exists, asyncio-based — use `async with limiter.limited("fmp")` | `src/pipeline/rate_limiter.py:189` |
| Redis client construction | `from src.database.redis import get_redis` (existing shared client) | match patterns in `src/agent/services/cycle-cache.js` and `src/pipeline/run_intraday_market_state.py` |
| Discord webhook for #backfill-log | `src/channels/discord/notify.js` `notify(channel, msg)` style (existing); call from Python via subprocess (`node src/channels/discord/notify_cli.js`) **OR** direct HTTPS POST to `DISCORD_BACKFILL_LOG_WEBHOOK` | match `src/channels/discord/` patterns — verify which style exists when implementing |
| Master parquet path | `data/master/<file>.parquet` | confirmed by `ls data/master/` before any write |
| `_append_parquet` semantics | per-file helper in `src/pipeline/backfillers/alpaca_options.py:191` — deduplicates on `(date, contract_symbol)` | Phase B uses `pyarrow.parquet.write_to_dataset(existing_data_behavior='delete_matching')` instead since the partition granularity is `(year, symbol)` and zero-existing precondition is checked explicitly |

If any substitution looks ambiguous in context, re-verify against the named source file before implementing.

---

## Task 0: Branch + workspace setup

**Files:** none (git scaffolding)

- [ ] **Step 1: Confirm Phase A is merged**

```bash
cd /root/openclaw
git fetch origin
git log origin/main --oneline | grep -i "sp-2 phase a" | head -3
gh pr view 8 --json state -q .state    # expect MERGED
```

- [ ] **Step 2: Create feature branch off main**

```bash
git checkout main && git pull
git checkout -b feat/sp2-phase-b-5y-backfill origin/main
```

- [ ] **Step 3: Verify clean tree**

Run: `git status` → expect "nothing to commit, working tree clean".

- [ ] **Step 4: Verify Python/Node tooling versions and existing dependencies**

```bash
python3 --version    # 3.13.x
node --version       # 22.x
python3 -c "import pyarrow, pandas, psycopg2; print(pyarrow.__version__, pandas.__version__, psycopg2.__version__)"
python3 -c "from src.pipeline.rate_limiter import get_rate_limiter; print(get_rate_limiter())"
```

- [ ] **Step 5: Verify Phase A artifacts present on main**

```bash
test -f src/strategies/universe_resolver.py
test -f src/strategies/universe_default.py
test -f src/pipeline/ticker_metadata_writer.py
psql_check() { node -e "require('./src/database/postgres').migrate().then(()=>{}).catch(e=>{console.error(e);process.exit(1)})"; }
psql_check
```

---

## Task 1: One-shot — build the backfill universe artifact

**Files:**
- Create: `scripts/build_backfill_universe.py`
- Create: `data/.backfill_universe_v1.txt` (commit)

The frozen list of ~3000 tickers Phase B will backfill. Pinned at start so the universe doesn't drift mid-run.

- [ ] **Step 1: Write the builder script**

Skeleton:
```python
#!/usr/bin/env python3
"""SP-2 Phase B: one-shot backfill-universe builder.

Output: data/.backfill_universe_v1.txt — newline-delimited tickers.
Source: alpaca_tradable_universe filtered to active+tradable, ranked by
        ADV (USD) computed from prices.parquet last 60 trading days.
"""
import argparse, os, sys
from pathlib import Path
import pandas as pd, pyarrow.parquet as pq, psycopg2

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / '.backfill_universe_v1.txt'
MASTER_PRICES = ROOT / 'data' / 'master' / 'prices.parquet'

def main(top_n: int) -> int:
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""
          SELECT symbol FROM alpaca_tradable_universe
          WHERE status='active' AND tradable=TRUE
          ORDER BY symbol
        """)
        active = [r[0] for r in cur.fetchall()]

    df = pq.read_table(str(MASTER_PRICES),
                       columns=['symbol','date','adj_close','volume']).to_pandas()
    cutoff = df['date'].max() - pd.Timedelta(days=90)
    recent = df[df['date'] >= cutoff].copy()
    recent['dollar_vol'] = recent['adj_close'] * recent['volume']
    adv = recent.groupby('symbol')['dollar_vol'].mean().sort_values(ascending=False)

    ranked = [t for t in adv.index if t in set(active)][:top_n]
    OUT.write_text('\n'.join(ranked) + '\n')
    print(f'Wrote {len(ranked)} tickers to {OUT}')
    return 0

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--top-n', type=int, default=3000)
    sys.exit(main(ap.parse_args().top_n))
```

- [ ] **Step 2: Run it (locally on VPS or staging clone)**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 scripts/build_backfill_universe.py --top-n 3000
wc -l data/.backfill_universe_v1.txt   # expect 3000
head data/.backfill_universe_v1.txt    # spot: AAPL, MSFT, NVDA, etc.
```

- [ ] **Step 3: Commit artifact + script**

```bash
git add scripts/build_backfill_universe.py data/.backfill_universe_v1.txt
git commit -m "feat(sp2-b): backfill universe builder + frozen top-3000 list (v1)"
```

**Note:** This file is committed deliberately as a frozen artifact. Future Phase B v2 cycles would write `v2.txt` rather than mutate v1.

---

## Task 2: One-shot — historical SP500 membership CSV

**Files:**
- Create: `scripts/probe_sp500_historical_membership.py`
- Create: `data/sp500_historical_membership_v1.csv` (commit)

Used by `build_month_snapshot` to populate `in_sp500` for historical months. Without this file, `in_sp500` defaults to today's membership (documented bias from Phase A).

- [ ] **Step 1: Write the probe script**

```python
#!/usr/bin/env python3
"""One-shot: writes a historical SP500 membership CSV.

Source: Wikipedia "List of S&P 500 companies" + "Selected changes to the list"
        section. Operator-runnable; output is committed once and never
        re-run as a cron job.

Schema: ticker,added_on,removed_on  (one row per (ticker, contiguous-membership-span)).
"""
import argparse, sys
from pathlib import Path
import pandas as pd, requests

OUT = Path(__file__).resolve().parents[1] / 'data' / 'sp500_historical_membership_v1.csv'
URL = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'

def main(dry: bool) -> int:
    tables = pd.read_html(URL)
    current = tables[0][['Symbol', 'Date added']].rename(
        columns={'Symbol':'ticker', 'Date added':'added_on'})
    current['removed_on'] = None
    changes = tables[1].copy()
    # Build (ticker, removed_on) pairs from changes — see Wikipedia table layout
    # Parsing details depend on current table shape; verify col names at runtime.
    # ...
    out = pd.concat([current, ...], ignore_index=True)
    if dry:
        print(out.head()); print(f'Total rows: {len(out)}'); return 0
    out.to_csv(OUT, index=False)
    print(f'Wrote {len(out)} membership-span rows to {OUT}')
    return 0

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    sys.exit(main(ap.parse_args().dry_run))
```

- [ ] **Step 2: Dry-run, then commit**

```bash
python3 scripts/probe_sp500_historical_membership.py --dry-run
python3 scripts/probe_sp500_historical_membership.py
head -5 data/sp500_historical_membership_v1.csv
git add scripts/probe_sp500_historical_membership.py data/sp500_historical_membership_v1.csv
git commit -m "feat(sp2-b): historical SP500 membership CSV (v1, Wikipedia-sourced)"
```

- [ ] **Step 3: Document the source** in the CSV header (manual edit before commit):

```csv
# Source: en.wikipedia.org/wiki/List_of_S%26P_500_companies (scraped YYYY-MM-DD)
# Schema: ticker,added_on,removed_on  (removed_on=NULL means currently a member)
ticker,added_on,removed_on
```

---

## Task 3: Migration 115 — `backfill_audit`

**Files:**
- Create: `src/database/migrations/115_backfill_audit.sql`

- [ ] **Step 1: Write migration**

```sql
-- Append-only durable audit log of every backfill chunk attempt.
-- Redis is for ops orchestration; this is for forensics + audit.

CREATE TABLE IF NOT EXISTS backfill_audit (
  id            BIGSERIAL PRIMARY KEY,
  target        TEXT NOT NULL,
  chunk_key     TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL,
  ended_at      TIMESTAMPTZ,
  status        TEXT NOT NULL,
  rows_written  INTEGER,
  source_tag    TEXT NOT NULL,
  sha256        TEXT,
  error_text    TEXT,
  CONSTRAINT backfill_audit_chunk_unique UNIQUE (target, chunk_key, source_tag, started_at)
);
CREATE INDEX IF NOT EXISTS idx_backfill_audit_status ON backfill_audit(target, status);
CREATE INDEX IF NOT EXISTS idx_backfill_audit_recent ON backfill_audit(started_at DESC);
```

- [ ] **Step 2: Apply locally**

```bash
node -e "require('./src/database/postgres').migrate().then(()=>process.exit(0)).catch(e=>{console.error(e);process.exit(1)})"
psql_check() { python3 -c "import os, psycopg2; c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor(); cur.execute('SELECT count(*) FROM backfill_audit'); print(cur.fetchone()); c.close()"; }
export $(grep -E "^POSTGRES_URI=" .env | head -1)
psql_check
```

- [ ] **Step 3: Commit**

```bash
git add src/database/migrations/115_backfill_audit.sql
git commit -m "feat(sp2-b): migration 115 backfill_audit (forensic log)"
```

---

## Task 4: Quarantine filter module

**Files:**
- Create: `src/pipeline/quarantine_filter.py`
- Create: `tests/test_quarantine_filter.py`

- [ ] **Step 1: Implement `filter_quarantined`**

```python
"""Read-time filter for master-parquet consumers.

The append-only invariant means bad rows cannot be DELETEd. This module
loads data_quarantine, caches it, and lets consumers drop affected
(symbol, date) pairs at read time.
"""
from __future__ import annotations
import os, time, threading
from typing import Iterable, Tuple
import psycopg2, pandas as pd

_CACHE: dict[str, tuple[float, set[Tuple[str, str]]]] = {}
_LOCK = threading.Lock()
_TTL = 300  # 5 minutes

def _load(master_table: str) -> set[Tuple[str, str]]:
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""
            SELECT symbol, affected_date::TEXT
            FROM data_quarantine
            WHERE master_table = %s AND superseded_at IS NULL
        """, (master_table,))
        return {(s, d) for s, d in cur.fetchall()}

def _cached(master_table: str) -> set[Tuple[str, str]]:
    with _LOCK:
        entry = _CACHE.get(master_table)
        if entry and time.time() - entry[0] < _TTL:
            return entry[1]
        rows = _load(master_table)
        _CACHE[master_table] = (time.time(), rows)
        return rows

def filter_quarantined(df: pd.DataFrame, master_table: str) -> pd.DataFrame:
    """Drop rows whose (symbol, date) appears unsuperseded in data_quarantine."""
    bad = _cached(master_table)
    if not bad:
        return df
    if df.empty or 'symbol' not in df.columns or 'date' not in df.columns:
        return df
    key = list(zip(df['symbol'].astype(str), df['date'].astype(str)))
    mask = [k not in bad for k in key]
    return df.loc[mask].reset_index(drop=True)

def invalidate_cache(master_table: str | None = None) -> None:
    with _LOCK:
        if master_table is None:
            _CACHE.clear()
        else:
            _CACHE.pop(master_table, None)
```

- [ ] **Step 2: Unit tests**

```python
# tests/test_quarantine_filter.py
import pytest, pandas as pd, uuid, os, psycopg2
from src.pipeline.quarantine_filter import filter_quarantined, invalidate_cache

@pytest.fixture
def pg():
    c = psycopg2.connect(os.environ['POSTGRES_URI']); yield c; c.close()

def test_drops_marked_rows(pg):
    invalidate_cache()
    tag = f'test_{uuid.uuid4().hex[:8]}'
    with pg.cursor() as cur:
        cur.execute("""INSERT INTO data_quarantine
            (master_table, symbol, affected_date, source_tag, reason, flagged_by)
            VALUES ('prices.parquet','TEST','2024-01-15',%s,'unit-test','auto:test')""", (tag,))
        pg.commit()
    try:
        df = pd.DataFrame({'symbol':['TEST','TEST','OK'], 'date':['2024-01-15','2024-01-16','2024-01-15']})
        out = filter_quarantined(df, 'prices.parquet')
        assert len(out) == 2
        assert ('TEST','2024-01-15') not in zip(out['symbol'], out['date'])
    finally:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM data_quarantine WHERE source_tag = %s", (tag,))
            pg.commit()
        invalidate_cache()
```

Add tests for: empty quarantine, superseded row (`superseded_at` set), cache invalidation, missing columns gracefully passes through.

- [ ] **Step 3: Run + commit**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -m pytest tests/test_quarantine_filter.py -v
git add src/pipeline/quarantine_filter.py tests/test_quarantine_filter.py
git commit -m "feat(sp2-b): quarantine filter module + tests"
```

---

## Task 5: Wire quarantine filter into all consumers

**Files (MODIFY):**
- `src/strategies/universe_resolver.py` (`coverage_floor` path)
- `src/backtest/unified_backtest.py`
- `src/backtest/regime_blended_backtest.py`
- `src/backtest/quick_backtest.py`
- `src/backtest/intraday_regime_backtest.py`
- `src/backtest/regime_performance_analyzer.py`
- `src/pipeline/collector.js` (subprocess shim — see step 4)

- [ ] **Step 1: Add filter to each `_load_prices` Python site**

For each of the 5 backtest engines and the resolver, find the parquet read path and wrap:
```python
from src.pipeline.quarantine_filter import filter_quarantined
...
df = pq.read_table(...).to_pandas()
df = filter_quarantined(df, 'prices.parquet')
return df
```

- [ ] **Step 2: For `universe_resolver.coverage_floor`**

After loading the per-ticker bar count, exclude any tickers whose `(symbol, date)` quarantine entries reduce their bar count below `MIN_BARS_FOR_INCLUSION`.

- [ ] **Step 3: Test parametrized integration across all 5 backtests**

```python
# tests/test_quarantine_filter_integration.py
import pytest, importlib
ENGINES = ['unified_backtest','regime_blended_backtest','quick_backtest',
           'intraday_regime_backtest','regime_performance_analyzer']

@pytest.mark.parametrize('engine', ENGINES)
def test_each_engine_imports_filter(engine):
    mod = importlib.import_module(f'src.backtest.{engine}')
    src = open(mod.__file__).read()
    assert 'filter_quarantined' in src, f'{engine} missing quarantine filter import'
```

- [ ] **Step 4: collector.js subprocess shim**

Node's collector reads parquet via Python subprocess elsewhere; add a tiny CLI mode to `quarantine_filter.py` so collector can call it:
```python
# Append to src/pipeline/quarantine_filter.py
if __name__ == '__main__':
    import argparse, json, sys
    ap = argparse.ArgumentParser()
    ap.add_argument('--master-table', required=True)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    rows = sorted(_cached(args.master_table))
    if args.json: print(json.dumps([list(r) for r in rows]))
    else:
        for s, d in rows: print(f'{s}\t{d}')
```

Then in `src/pipeline/collector.js` add a startup hook:
```js
const { execFileSync } = require('node:child_process');
let QUARANTINE = new Set();
try {
  const raw = execFileSync('python3', ['src/pipeline/quarantine_filter.py',
    '--master-table', 'prices.parquet', '--json'], { encoding: 'utf8' });
  QUARANTINE = new Set(JSON.parse(raw).map(([s,d]) => `${s}|${d}`));
  console.log(`[collector] quarantine filter loaded ${QUARANTINE.size} rows`);
} catch (e) { console.warn('[collector] quarantine load failed (continuing):', e.message); }
// Use QUARANTINE.has(`${sym}|${date}`) in parquet-row consumers.
```

- [ ] **Step 5: Run + commit**

```bash
python3 -m pytest tests/test_quarantine_filter_integration.py -v
node -c src/pipeline/collector.js   # syntax check
git add src/strategies/universe_resolver.py src/backtest/*.py src/pipeline/collector.js src/pipeline/quarantine_filter.py tests/test_quarantine_filter_integration.py
git commit -m "feat(sp2-b): wire quarantine filter into resolver+backtests+collector"
```

---

## Task 6: Backfill driver scaffolding

**Files:**
- Create: `scripts/backfill_universe_5y.py` (scaffolding only — per-target logic in Tasks 7-9)
- Create: `src/pipeline/backfillers/__init__.py` (if missing — verify)

- [ ] **Step 1: Argparse + Redis ckpt + audit row helpers**

```python
#!/usr/bin/env python3
"""SP-2 Phase B: 5y backfill driver.

USAGE:
  scripts/backfill_universe_5y.py --target prices|metadata|options
                                  [--resume] [--dry-run]
                                  [--tickers AAPL,MSFT] [--years 2021,2022]
                                  [--source-tag backfill_5y_v1]
                                  [--supersede-quarantine]
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, sys, time, uuid
from datetime import date, datetime
from pathlib import Path
import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

STAGING = ROOT / 'data' / '.staging'
CHECKPOINTS = ROOT / 'data' / '.checkpoints' / 'backfill_5y'
UNIVERSE_FILE = ROOT / 'data' / '.backfill_universe_v1.txt'

def _ensure_dirs():
    STAGING.mkdir(parents=True, exist_ok=True)
    CHECKPOINTS.mkdir(parents=True, exist_ok=True)

def _redis():
    from database.redis import get_redis        # match Phase A import style
    return get_redis()

def _audit_start(pg, target: str, chunk_key: str, source_tag: str) -> int:
    started_at = datetime.utcnow()
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO backfill_audit (target, chunk_key, started_at, status, source_tag)
            VALUES (%s, %s, %s, 'in_progress', %s) RETURNING id
        """, (target, chunk_key, started_at, source_tag))
        pg.commit()
        return cur.fetchone()[0]

def _audit_finish(pg, audit_id: int, status: str, rows: int = 0, sha: str = None, err: str = None):
    with pg.cursor() as cur:
        cur.execute("""UPDATE backfill_audit SET ended_at=NOW(), status=%s,
                       rows_written=%s, sha256=%s, error_text=%s WHERE id=%s""",
                    (status, rows, sha, err, audit_id))
        pg.commit()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', required=True, choices=['prices','metadata','options'])
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--tickers', default=None)
    ap.add_argument('--years', default=None)
    ap.add_argument('--source-tag', default='backfill_5y_v1')
    ap.add_argument('--supersede-quarantine', action='store_true')
    args = ap.parse_args()
    _ensure_dirs()
    if args.source_tag != 'backfill_5y_v1' and not os.environ.get('OPENCLAW_BACKFILL_ALLOW_OVERWRITE'):
        sys.exit('REFUSED: non-v1 source_tag requires OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1')
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    {
        'prices':   _run_prices,
        'metadata': _run_metadata,
        'options':  _run_options,
    }[args.target](args, pg)

# Stubs for Tasks 7/8/9 to fill in:
def _run_prices(args, pg):   raise NotImplementedError('Task 7')
def _run_metadata(args, pg): raise NotImplementedError('Task 8')
def _run_options(args, pg):  raise NotImplementedError('Task 9')

if __name__ == '__main__': main()
```

- [ ] **Step 2: Driver-only smoke test**

```python
# tests/test_backfill_driver_scaffolding.py
import subprocess
def test_driver_refuses_unknown_target():
    r = subprocess.run(['python3','scripts/backfill_universe_5y.py','--target','garbage'],
                       capture_output=True, text=True)
    assert r.returncode != 0
def test_driver_refuses_v2_without_gate(monkeypatch):
    monkeypatch.delenv('OPENCLAW_BACKFILL_ALLOW_OVERWRITE', raising=False)
    r = subprocess.run(['python3','scripts/backfill_universe_5y.py','--target','prices',
                        '--source-tag','backfill_5y_v2','--dry-run'], capture_output=True, text=True)
    assert r.returncode != 0
    assert 'REFUSED' in r.stderr or 'REFUSED' in r.stdout
```

- [ ] **Step 3: Commit**

```bash
python3 -m pytest tests/test_backfill_driver_scaffolding.py -v
git add scripts/backfill_universe_5y.py tests/test_backfill_driver_scaffolding.py
git commit -m "feat(sp2-b): backfill driver scaffolding (argparse, audit, gate)"
```

---

## Task 7: `--target prices` implementation

**Files:**
- Modify: `scripts/backfill_universe_5y.py` (`_run_prices`)
- Create: `src/pipeline/backfillers/universe_prices.py`
- Create: `tests/test_backfill_universe_prices.py`

- [ ] **Step 1: `universe_prices.py` — pure fetch + validate module**

```python
"""Pure module: fetch + validate price chunks for backfill driver."""
import json, subprocess, hashlib
from datetime import date
from pathlib import Path
import pandas as pd

ALPACA_BIN = '/root/go/bin/alpaca'

SCHEMA = ['symbol','date','open','high','low','close','volume','adj_close']

def fetch_ticker_year(symbol: str, year: int) -> pd.DataFrame:
    start, end = f'{year}-01-01', f'{year}-12-31'
    raw = subprocess.run([ALPACA_BIN,'data','stocks','bars',
                          '--symbol', symbol,
                          '--timeframe','1Day',
                          '--start', start, '--end', end],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    obj = json.loads(raw)
    bars = obj.get('bars', {}).get(symbol, [])
    if not bars: return pd.DataFrame(columns=SCHEMA)
    df = pd.DataFrame(bars)
    df['symbol'] = symbol
    df['date'] = pd.to_datetime(df['t']).dt.date.astype(str)
    df['adj_close'] = df['c']     # Alpaca daily bars are split-adjusted at request time
    out = df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})[SCHEMA]
    return out

def validate(df: pd.DataFrame, symbol: str, year: int) -> tuple[bool, str | None]:
    if list(df.columns) != SCHEMA: return False, 'schema_mismatch'
    if df[['symbol','date','close']].isna().any().any(): return False, 'null_pk_or_close'
    if df['date'].min() < f'{year}-01-01' or df['date'].max() > f'{year}-12-31':
        return False, 'date_out_of_range'
    # Listed mid-year tickers can have fewer bars; full-year minimum 200, partial ≥ 30
    if len(df) < 30: return False, f'row_count_too_low ({len(df)})'
    return True, None

def sha256(df: pd.DataFrame) -> str:
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()
```

- [ ] **Step 2: `_run_prices` in driver**

```python
def _run_prices(args, pg):
    import pyarrow.parquet as pq, pyarrow as pa
    from pipeline.backfillers.universe_prices import fetch_ticker_year, validate, sha256, SCHEMA
    from pipeline.rate_limiter import get_rate_limiter
    from pipeline.quarantine_filter import invalidate_cache
    limiter = get_rate_limiter()
    redis = _redis()
    tickers = (args.tickers.split(',') if args.tickers
               else UNIVERSE_FILE.read_text().strip().split('\n'))
    years = [int(y) for y in (args.years.split(',') if args.years
                              else range(2021, date.today().year + 1))]
    master = ROOT / 'data' / 'master' / 'prices.parquet'
    for symbol in tickers:
        for year in years:
            chunk_key = f'{symbol}:{year}'
            redis_key = f'backfill:5y:prices:{chunk_key}'
            status = (redis.get(redis_key) or b'').decode()
            if args.resume and status == 'promoted':
                continue
            if status == 'quarantined' and not args.supersede_quarantine:
                continue
            audit_id = _audit_start(pg, 'prices', chunk_key, args.source_tag)
            redis.set(redis_key, 'in_progress', ex=86400)
            try:
                asyncio.run(limiter.acquire('alpaca'))
                df = fetch_ticker_year(symbol, year)
                ok, err = validate(df, symbol, year)
                if not ok:
                    redis.set(redis_key, 'quarantined', ex=86400 * 30)
                    _quarantine_chunk(pg, 'prices.parquet', symbol, year, args.source_tag, err)
                    _audit_finish(pg, audit_id, 'quarantined', 0, None, err)
                    _notify_discord(f'❌ {chunk_key}: {err}')
                    continue
                if args.dry_run:
                    print(f'[dry-run] {chunk_key}: {len(df)} rows valid'); 
                    _audit_finish(pg, audit_id, 'validated', len(df), sha256(df)); 
                    continue
                # PROMOTE: zero-existing precondition + delete_matching atomic write
                existing = _existing_dates_for(master, symbol, year)
                df_new = df[~df['date'].isin(existing)]
                if df_new.empty:
                    redis.set(redis_key, 'promoted', ex=86400)
                    _audit_finish(pg, audit_id, 'promoted', 0); continue
                if not df_new.equals(df) and args.source_tag == 'backfill_5y_v1':
                    err = f'overlap with existing rows ({len(df)-len(df_new)})'
                    redis.set(redis_key, 'quarantined', ex=86400 * 30)
                    _audit_finish(pg, audit_id, 'quarantined', 0, None, err)
                    continue
                df_new['year'] = year
                df_new['source_tag'] = args.source_tag
                table = pa.Table.from_pandas(df_new)
                pq.write_to_dataset(table, root_path=str(master),
                                    partition_cols=['year','symbol'],
                                    existing_data_behavior='delete_matching')
                redis.set(redis_key, 'promoted', ex=86400)
                _audit_finish(pg, audit_id, 'promoted', len(df_new), sha256(df_new))
                invalidate_cache('prices.parquet')
            except Exception as e:
                redis.set(redis_key, 'failed', ex=86400)
                _audit_finish(pg, audit_id, 'failed', 0, None, str(e)[:500])
                _notify_discord(f'⚠️ {chunk_key} failed: {e}')

def _existing_dates_for(master, symbol, year):
    import pyarrow.parquet as pq
    try:
        t = pq.read_table(str(master), columns=['date'],
                          filters=[('symbol','=',symbol),('year','=',year)]).to_pandas()
        return set(t['date'].astype(str))
    except Exception: return set()

def _quarantine_chunk(pg, master_table, symbol, year, source_tag, reason):
    from datetime import date as D
    start_d = D(year, 1, 1); end_d = D(year, 12, 31)
    with pg.cursor() as cur:
        cur.execute("""INSERT INTO data_quarantine
            (master_table, symbol, affected_date, source_tag, reason, flagged_by)
            SELECT %s, %s, dd::date, %s, %s, 'auto:backfill-validator'
            FROM generate_series(%s::date, %s::date, '1 day') dd
            ON CONFLICT DO NOTHING""",
            (master_table, symbol, source_tag, reason, start_d, end_d))
        pg.commit()

def _notify_discord(msg: str):
    import os, requests
    hook = os.environ.get('DISCORD_BACKFILL_LOG_WEBHOOK')
    if not hook: return
    try: requests.post(hook, json={'content': msg}, timeout=5)
    except Exception: pass
```

- [ ] **Step 3: Tests**

```python
# tests/test_backfill_universe_prices.py
import pytest, pandas as pd
from src.pipeline.backfillers.universe_prices import validate, SCHEMA

def _df(n):
    return pd.DataFrame({c: list(range(n)) for c in SCHEMA})

def test_validate_rejects_schema_mismatch():
    df = pd.DataFrame({'symbol':['A'], 'date':['2024-01-01'], 'close':[1.0]})
    ok, err = validate(df, 'A', 2024); assert not ok and 'schema' in err

def test_validate_rejects_low_rowcount():
    df = pd.DataFrame({c: list(range(10)) for c in SCHEMA})
    df['symbol'] = 'A'; df['date'] = [f'2024-01-{1+i:02d}' for i in range(10)]; df['close'] = list(range(10))
    ok, err = validate(df, 'A', 2024); assert not ok

def test_validate_accepts_minimal_valid():
    rows = [{'symbol':'A','date':f'2024-{1+(i//28):02d}-{1+(i%28):02d}','open':1,'high':1,'low':1,'close':1,'volume':1,'adj_close':1} for i in range(60)]
    df = pd.DataFrame(rows); ok, err = validate(df, 'A', 2024); assert ok, err
```

Plus integration test (`monkeypatch fetch_ticker_year` to return a fixture DataFrame, run `_run_prices` against `--dry-run` and `--tickers TEST`).

- [ ] **Step 4: Smoke run + commit**

```bash
export $(grep -E "^POSTGRES_URI=|^DISCORD_BACKFILL_LOG_WEBHOOK=|^ALPACA_API_KEY=|^ALPACA_SECRET_KEY=" .env)
python3 -m pytest tests/test_backfill_universe_prices.py -v
python3 scripts/backfill_universe_5y.py --target prices --tickers AAPL --years 2024 --dry-run
git add scripts/backfill_universe_5y.py src/pipeline/backfillers/universe_prices.py tests/test_backfill_universe_prices.py
git commit -m "feat(sp2-b): --target prices fetch+validate+promote loop"
```

---

## Task 8: `--target metadata` implementation

**Files:**
- Modify: `scripts/backfill_universe_5y.py` (`_run_metadata`)
- Create: `src/pipeline/backfillers/universe_metadata.py`
- Modify: `src/pipeline/ticker_metadata_writer.py` (delegate to `build_month_snapshot`)
- Create: `tests/test_universe_metadata_builder.py`

- [ ] **Step 1: `universe_metadata.py` — month snapshot builder**

```python
"""Composite source builder for ticker_metadata_snapshots.

build_month_snapshot(snapshot_date, universe) -> pd.DataFrame
  Used by both Phase B backfill driver and Phase A live writer for DRY.
"""
from __future__ import annotations
import os, subprocess, json, csv
from datetime import date
from pathlib import Path
import pandas as pd, psycopg2, pyarrow.parquet as pq, requests

ROOT = Path(__file__).resolve().parents[3]
MASTER_PRICES = ROOT / 'data' / 'master' / 'prices.parquet'
SP500_CSV     = ROOT / 'data' / 'sp500_historical_membership_v1.csv'
ALPACA_BIN    = '/root/go/bin/alpaca'

def _sp500_membership_on(d: date) -> set[str]:
    if not SP500_CSV.exists(): return set()
    rows = pd.read_csv(SP500_CSV)
    iso = d.isoformat()
    return set(rows[(rows['added_on'] <= iso) & (rows['removed_on'].isna() | (rows['removed_on'] > iso))]['ticker'])

def _market_cap_for(symbol: str, on: date) -> float | None:
    url = f'https://financialmodelingprep.com/api/v3/historical-market-capitalization/{symbol}'
    try:
        r = requests.get(url, params={'from': on.isoformat(), 'to': on.isoformat(),
                                       'apikey': os.environ['FMP_API_KEY']}, timeout=10)
        if r.status_code != 200: return None
        rows = r.json()
        if rows: return float(rows[0].get('marketCap', 0)) or None
    except Exception: return None
    return None

def _adv_usd_20d(symbol: str, on: date) -> float | None:
    try:
        t = pq.read_table(str(MASTER_PRICES), columns=['symbol','date','adj_close','volume'],
                          filters=[('symbol','=', symbol)]).to_pandas()
        t = t[(t['date'].astype(str) <= on.isoformat())].sort_values('date').tail(20)
        if len(t) < 5: return None
        return float((t['adj_close'] * t['volume']).mean())
    except Exception: return None

def _alpaca_status_for(symbol: str, on: date, pg) -> dict:
    """Reads alpaca_tradable_universe — best-available approximation for historical days."""
    with pg.cursor() as cur:
        cur.execute("""SELECT asset_class, exchange, status, tradable, shortable,
                               fractionable, easy_to_borrow, first_seen_at, last_seen_at
                       FROM alpaca_tradable_universe WHERE symbol = %s""", (symbol,))
        r = cur.fetchone()
    if not r: return {}
    keys = ['asset_class','exchange','status','tradable','shortable','fractionable',
            'easy_to_borrow','first_seen_at','last_seen_at']
    d = dict(zip(keys, r))
    if d.get('first_seen_at') and d['first_seen_at'].date() > on: return {}
    if d.get('last_seen_at') and d['last_seen_at'].date() < on: d['status'] = 'inactive'
    return d

def build_month_snapshot(snapshot_date: date, universe: list[str], pg) -> pd.DataFrame:
    rows = []
    sp500 = _sp500_membership_on(snapshot_date)
    # Build with market_cap first so r1000/r3000 ranking is correct
    for sym in universe:
        a = _alpaca_status_for(sym, snapshot_date, pg)
        if not a: continue
        mc = _market_cap_for(sym, snapshot_date)
        adv = _adv_usd_20d(sym, snapshot_date)
        rows.append({
            'snapshot_date': snapshot_date,
            'symbol': sym,
            'asset_class': a.get('asset_class') or 'us_equity',
            'exchange': a.get('exchange'),
            'status': a.get('status') or 'active',
            'tradable': bool(a.get('tradable')),
            'shortable': bool(a.get('shortable')),
            'fractionable': bool(a.get('fractionable')),
            'easy_to_borrow': bool(a.get('easy_to_borrow')),
            'market_cap': mc,
            'adv_usd_20d': adv,
            'sector': None,         # documented proxy: filled by daily writer from cache
            'industry': None,
            'options_eligible': False,  # set by daily writer's chain-probe cache
            'in_sp500': sym in sp500,
        })
    df = pd.DataFrame(rows)
    if df.empty: return df
    # in_r1000 / in_r3000 = top-N by market_cap WHERE tradable+active
    elig = df[df['tradable'] & (df['status']=='active') & df['market_cap'].notna()].sort_values('market_cap', ascending=False)
    r1000 = set(elig['symbol'].head(1000)); r3000 = set(elig['symbol'].head(3000))
    df['in_r1000'] = df['symbol'].isin(r1000)
    df['in_r3000'] = df['symbol'].isin(r3000)
    df['listed_date'] = None       # already in master snapshots for live rows
    df['delisted_date'] = None
    return df
```

- [ ] **Step 2: `_run_metadata` in driver**

```python
def _run_metadata(args, pg):
    from pipeline.backfillers.universe_metadata import build_month_snapshot
    from datetime import date, timedelta
    universe = (args.tickers.split(',') if args.tickers
                else UNIVERSE_FILE.read_text().strip().split('\n'))
    months = _enumerate_month_ends(args.years)
    redis = _redis()
    for m in months:
        chunk_key = f'{m.isoformat()}:metadata'
        redis_key = f'backfill:5y:metadata:{chunk_key}'
        status = (redis.get(redis_key) or b'').decode()
        if args.resume and status == 'promoted': continue
        audit_id = _audit_start(pg, 'metadata', chunk_key, args.source_tag)
        redis.set(redis_key, 'in_progress', ex=86400)
        try:
            df = build_month_snapshot(m, universe, pg)
            ok, err = _validate_metadata(df, m)
            if not ok:
                redis.set(redis_key, 'quarantined', ex=86400 * 30)
                _audit_finish(pg, audit_id, 'quarantined', 0, None, err); continue
            if args.dry_run:
                _audit_finish(pg, audit_id, 'validated', len(df))
                print(f'[dry-run] {chunk_key}: {len(df)} rows valid'); continue
            with pg.cursor() as cur:
                rows = [(r.snapshot_date, r.symbol, r.asset_class, r.exchange, r.status,
                         r.tradable, r.shortable, r.fractionable, r.easy_to_borrow,
                         r.market_cap, r.adv_usd_20d, r.sector, r.industry,
                         r.options_eligible, r.in_sp500, r.in_r1000, r.in_r3000,
                         r.listed_date, r.delisted_date, args.source_tag)
                        for r in df.itertuples()]
                cur.executemany("""INSERT INTO ticker_metadata_snapshots
                  (snapshot_date, symbol, asset_class, exchange, status, tradable, shortable,
                   fractionable, easy_to_borrow, market_cap, adv_usd_20d, sector, industry,
                   options_eligible, in_sp500, in_r1000, in_r3000, listed_date, delisted_date, source_tag)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (snapshot_date, symbol) DO NOTHING""", rows)
                pg.commit()
            redis.set(redis_key, 'promoted', ex=86400)
            _audit_finish(pg, audit_id, 'promoted', len(df))
        except Exception as e:
            redis.set(redis_key, 'failed', ex=86400)
            _audit_finish(pg, audit_id, 'failed', 0, None, str(e)[:500])

def _enumerate_month_ends(years_csv: str | None):
    from calendar import monthrange
    from datetime import date as D
    yrs = [int(y) for y in (years_csv.split(',') if years_csv else range(2021, D.today().year + 1))]
    out = []
    for y in yrs:
        for m in range(1, 13):
            d = D(y, m, monthrange(y, m)[1])
            if d <= D.today(): out.append(d)
    return out

def _validate_metadata(df, snapshot_date) -> tuple[bool, str | None]:
    if df.empty: return False, 'empty'
    if len(df) < 1500: return False, f'row_count_too_low ({len(df)})'
    if df['symbol'].duplicated().any(): return False, 'duplicate_symbol'
    top10_caps = df.nlargest(10, 'market_cap')['market_cap']
    if top10_caps.max() < 1e11: return False, 'top10_market_cap_implausible'
    return True, None
```

- [ ] **Step 3: DRY refactor — `ticker_metadata_writer.py` calls `build_month_snapshot(today, universe, pg)`**

Replace the inline row construction (Phase A) with a thin wrapper:
```python
from src.pipeline.backfillers.universe_metadata import build_month_snapshot
df = build_month_snapshot(date.today(), universe, pg)
# Add today-only fields (sector, industry, options_eligible) from existing caches
# then upsert with source_tag='live_daily'.
```

Existing Phase A tests for `ticker_metadata_writer.py` must continue to pass.

- [ ] **Step 4: Tests**

```python
# tests/test_universe_metadata_builder.py
import pytest, os, psycopg2
from datetime import date
from src.pipeline.backfillers.universe_metadata import build_month_snapshot

@pytest.fixture
def pg(): c = psycopg2.connect(os.environ['POSTGRES_URI']); yield c; c.close()

def test_builds_for_small_universe(pg):
    df = build_month_snapshot(date(2024,8,31), ['AAPL','MSFT'], pg)
    assert len(df) <= 2
    if len(df) > 0:
        assert {'snapshot_date','symbol','market_cap','in_sp500','in_r1000','in_r3000'} <= set(df.columns)

def test_row_count_validator_threshold():
    from scripts.backfill_universe_5y import _validate_metadata
    import pandas as pd
    df = pd.DataFrame({'symbol':[f'T{i}' for i in range(1499)], 'market_cap':[1e12]*1499})
    ok, err = _validate_metadata(df, date(2024,8,31))
    assert not ok and 'row_count_too_low' in err

def test_writer_uses_builder():
    """Regression: Phase A's ticker_metadata_writer.py must delegate to build_month_snapshot."""
    src = open('src/pipeline/ticker_metadata_writer.py').read()
    assert 'build_month_snapshot' in src
```

- [ ] **Step 5: Smoke + commit**

```bash
python3 -m pytest tests/test_universe_metadata_builder.py tests/test_ticker_metadata_writer.py -v
python3 scripts/backfill_universe_5y.py --target metadata --years 2024 --tickers AAPL,MSFT --dry-run
git add scripts/backfill_universe_5y.py src/pipeline/backfillers/universe_metadata.py src/pipeline/ticker_metadata_writer.py tests/test_universe_metadata_builder.py
git commit -m "feat(sp2-b): --target metadata month-snapshot builder + writer DRY refactor"
```

---

## Task 9: `--target options` — refactor cutover-gap script

**Files:**
- Modify: `scripts/backfill_universe_5y.py` (`_run_options`)
- Modify: `scripts/backfill_options_eod_cutover_gap.py` (now delegates to driver; keep as thin wrapper for backward compat)
- Create: `tests/test_backfill_options_target.py`

- [ ] **Step 1: Port the cutover-gap logic**

`_run_options(args, pg)` re-uses the contract enumeration and `_append_parquet` from `src/pipeline/backfillers/alpaca_options.py`. Chunk key: `(date, ticker)`. Validation reuses the existing checks.

- [ ] **Step 2: Wrapper script preserved**

`scripts/backfill_options_eod_cutover_gap.py` continues to exist; its `main()` builds the same arg vector and calls into `_run_options` from the driver. This preserves operator muscle-memory.

- [ ] **Step 3: Tests**

Parametrize the existing `scripts/backfill_options_eod_cutover_gap.py` test (if any) to also pass when invoked through the driver. Add audit-row + Redis-checkpoint assertions.

- [ ] **Step 4: Commit**

```bash
python3 -m pytest tests/test_backfill_options_target.py -v
git add scripts/backfill_universe_5y.py scripts/backfill_options_eod_cutover_gap.py tests/test_backfill_options_target.py
git commit -m "feat(sp2-b): --target options driver mode (refactor cutover-gap wrapper)"
```

---

## Task 10: Doctor + system_checks

**Files:**
- Modify: `src/maintenance/doctor.py`
- Create: `src/system_checks/checks/backfill_progress.py`
- Create: `src/system_checks/checks/ticker_metadata_history_depth.py`
- Modify: `src/system_checks/checks/__init__.py`
- Create: `tests/test_doctor_backfill.py`

- [ ] **Step 1: Doctor checks (both `slow=True`)**

```python
# Append to src/maintenance/doctor.py
@_check('backfill_progress', slow=True)
def _check_backfill_progress():
    import psycopg2, os
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""SELECT status, count(*) FROM backfill_audit
                       WHERE started_at > NOW() - INTERVAL '7 days' GROUP BY status""")
        stats = dict(cur.fetchall())
    q = stats.get('quarantined', 0); ip = stats.get('in_progress', 0)
    if q > 100: return Result.FAIL, f'{q} chunks quarantined in 7d (data poisoning risk)'
    if q > 0:   return Result.WARN, f'{q} chunks quarantined in 7d (review)'
    return Result.PASS, f'{stats.get("promoted",0)} promoted, {ip} in-progress'

@_check('backfill_universe_coverage', slow=True)
def _check_backfill_universe_coverage():
    import psycopg2, os
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""SELECT date_trunc('month',snapshot_date)::date AS m, count(*)
                       FROM ticker_metadata_snapshots
                       WHERE snapshot_date >= '2021-05-01'
                       GROUP BY m ORDER BY m""")
        rows = cur.fetchall()
    if not rows: return Result.WARN, 'no historical snapshots yet (Phase B not run)'
    low = [(m,c) for m,c in rows if c < 2500]
    if len(low) > 6: return Result.WARN, f'{len(low)} months have <2500 rows'
    return Result.PASS, f'{len(rows)} months, min={min(c for _,c in rows)}'
```

- [ ] **Step 2: system_checks files (matching Phase A convention)**

```python
# src/system_checks/checks/backfill_progress.py
from src.system_checks.registry import check
from src.system_checks.status import Status

@check(name='backfill_progress', tags=['storage','strategies'], requires=['db','redis'])
def run() -> tuple[Status, str]:
    import os, psycopg2
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""SELECT count(*) FROM backfill_audit
                       WHERE status='in_progress' AND started_at < NOW() - INTERVAL '24 hours'""")
        stuck = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM backfill_audit WHERE status='quarantined'")
        q = cur.fetchone()[0]
    if stuck > 0:  return Status.FAIL, f'{stuck} chunks stuck >24h in_progress'
    if q > 0:      return Status.WARN, f'{q} chunks quarantined (manual review pending)'
    return Status.PASS, 'all backfill chunks healthy'
```

```python
# src/system_checks/checks/ticker_metadata_history_depth.py
from src.system_checks.registry import check
from src.system_checks.status import Status
from datetime import date

@check(name='ticker_metadata_history_depth', tags=['strategies'], requires=['db'])
def run() -> tuple[Status, str]:
    import os, psycopg2
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("SELECT min(snapshot_date) FROM ticker_metadata_snapshots")
        mn = cur.fetchone()[0]
    if mn is None: return Status.WARN, 'no snapshots yet'
    threshold = date(2021, 6, 1)
    if mn > threshold: return Status.WARN, f'history depth {mn.isoformat()} (target ≤ 2021-06-01)'
    return Status.PASS, f'history depth {mn.isoformat()}'
```

- [ ] **Step 3: Register imports**

```python
# Append to src/system_checks/checks/__init__.py
from . import backfill_progress, ticker_metadata_history_depth   # noqa: F401
```

- [ ] **Step 4: Tests**

```python
# tests/test_doctor_backfill.py
import pytest, os, psycopg2
from src.maintenance.doctor import _check_backfill_progress, _check_backfill_universe_coverage, Result

def test_progress_passes_on_empty_audit(monkeypatch):
    # ensure clean state for the test window
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("DELETE FROM backfill_audit WHERE chunk_key LIKE 'TEST:%'"); pg.commit()
    status, msg = _check_backfill_progress()
    assert status in (Result.PASS, Result.WARN), msg

def test_history_depth_warns_when_empty(monkeypatch):
    # No precondition — just exercise that the function runs without crash.
    status, msg = _check_backfill_universe_coverage()
    assert status in (Result.PASS, Result.WARN)
```

- [ ] **Step 5: Run + commit**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -m pytest tests/test_doctor_backfill.py -v
python3 -m src.system_checks --tag storage --check backfill_progress
python3 -m src.system_checks --tag strategies --check ticker_metadata_history_depth
git add src/maintenance/doctor.py src/system_checks/checks/__init__.py src/system_checks/checks/backfill_progress.py src/system_checks/checks/ticker_metadata_history_depth.py tests/test_doctor_backfill.py
git commit -m "feat(sp2-b): doctor + system_checks for backfill progress and history depth"
```

---

## Task 11: Dashboard tiles

**Files:**
- Modify: `src/channels/dashboard/server.js` (operator :7870)
- Modify: `src/channels/dashboard/public/index.html`
- Modify: `src/channels/api/server.js` (user :3000)
- Modify: `src/channels/api/routes_pipelines.js`

- [ ] **Step 1: Operator tile — `/api/backfill-progress`**

Query Redis for `backfill:5y:*` keys + Postgres for `backfill_audit` aggregates. Returns:
```json
{"prices": {"in_progress": 12, "validated": 0, "promoted": 14988, "quarantined": 0, "failed": 0},
 "metadata": {...}, "options": {...}}
```

UI: stacked horizontal bar per target with click-through to per-chunk audit detail.

- [ ] **Step 2: User panel — `/api/pipelines/backfill-history`**

```sql
SELECT date_trunc('month', snapshot_date)::date AS m, COUNT(*) AS n
FROM ticker_metadata_snapshots
GROUP BY m ORDER BY m
```

UI: timeline sparkline in the Data Health section of the Pipeline Diagnostics tab.

- [ ] **Step 3: Test + commit**

```bash
node -c src/channels/dashboard/server.js
node -c src/channels/api/server.js
node -c src/channels/api/routes_pipelines.js
curl -s http://127.0.0.1:7870/api/backfill-progress | python3 -m json.tool
curl -s http://127.0.0.1:3000/api/pipelines/backfill-history | python3 -m json.tool
git add src/channels/dashboard/server.js src/channels/dashboard/public/index.html src/channels/api/server.js src/channels/api/routes_pipelines.js
git commit -m "feat(sp2-b): operator + user dashboard tiles for backfill state"
```

---

## Task 12: End-to-end smoke test

**Files:**
- Create: `tests/test_sp2_phase_b_smoke.py`

- [ ] **Step 1: Write smoke covering 5 scenarios**

```python
import pytest, subprocess, json, os, psycopg2, uuid

def _pg(): return psycopg2.connect(os.environ['POSTGRES_URI'])

def test_doctor_runs():
    r = subprocess.run(['python3','-m','src.maintenance.doctor','--required-only','--json'],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode in (0, 2), r.stdout + r.stderr

def test_driver_dry_run_prices():
    r = subprocess.run(['python3','scripts/backfill_universe_5y.py','--target','prices',
                        '--tickers','AAPL','--years','2024','--dry-run'],
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ})
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'dry-run' in r.stdout.lower() or 'validated' in r.stdout.lower()

def test_quarantine_filter_active():
    pg = _pg(); tag = f'smoke_{uuid.uuid4().hex[:6]}'
    with pg.cursor() as cur:
        cur.execute("""INSERT INTO data_quarantine
            (master_table, symbol, affected_date, source_tag, reason, flagged_by)
            VALUES ('prices.parquet','TESTSMOKE','2024-01-15',%s,'smoke','auto:smoke')""", (tag,))
        pg.commit()
    try:
        import pandas as pd
        from src.pipeline.quarantine_filter import filter_quarantined, invalidate_cache
        invalidate_cache('prices.parquet')
        df = pd.DataFrame({'symbol':['TESTSMOKE','OK'], 'date':['2024-01-15','2024-01-15']})
        out = filter_quarantined(df, 'prices.parquet')
        assert len(out) == 1 and out.iloc[0]['symbol'] == 'OK'
    finally:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM data_quarantine WHERE source_tag=%s", (tag,)); pg.commit()

def test_system_checks_storage_tag():
    r = subprocess.run(['python3','-m','src.system_checks','--tag','storage','--json'],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode in (0, 1), r.stdout

def test_system_checks_strategies_tag():
    r = subprocess.run(['python3','-m','src.system_checks','--tag','strategies','--json'],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode in (0, 1), r.stdout
```

- [ ] **Step 2: Run + commit**

```bash
export $(grep -E "^POSTGRES_URI=" .env | head -1)
python3 -m pytest tests/test_sp2_phase_b_smoke.py -v
git add tests/test_sp2_phase_b_smoke.py
git commit -m "test(sp2-b): end-to-end smoke (doctor, dry-run, quarantine, system_checks)"
```

---

## Task 13: Docs + memory + runbook + `.env.example`

**Files:**
- Create: `docs/sp2-backfill-runbook.md`
- Modify: `.env.example`
- Modify: `CLAUDE.md` (Recent Changes entry)
- Modify: `ARCHITECTURE.md` (extend "Per-Strategy Universe Resolution" section)
- Update memory:
  - `/root/.claude/projects/-root/memory/feedback_never_delete_master_data.md` (add Phase B documented exception)
  - `/root/.claude/projects/-root/memory/project_sp2_phase_b_backfill.md` (NEW)
  - `/root/.claude/projects/-root/memory/MEMORY.md` (index)

- [ ] **Step 1: Runbook (`docs/sp2-backfill-runbook.md`)**

Sections:
1. Preflight checklist (link to spec §6.3)
2. Kickoff commands (per-target)
3. Monitoring (Discord, dashboard, doctor)
4. Quarantine handling (when, how, recovery)
5. Rollback ladder (link to spec §6.2)

- [ ] **Step 2: `.env.example` additions**

```
# SP-2 Phase B — 5y backfill
OPENCLAW_BACKFILL_5Y_ACTIVE=0
OPENCLAW_BACKFILL_ALLOW_OVERWRITE=0
BACKFILL_FMP_CONCURRENCY=4
BACKFILL_FMP_INTERVAL_MS=200
BACKFILL_ALPACA_CONCURRENCY=12
BACKFILL_ALPACA_INTERVAL_MS=80
DISCORD_BACKFILL_LOG_WEBHOOK=
```

- [ ] **Step 3: CLAUDE.md entry**

Add above the existing SP-2 Phase A entry:
```markdown
- **2026-XX-XX: SP-2 Phase B — 5y backfill shipped** ... (template after deploy)
```

- [ ] **Step 4: ARCHITECTURE.md**

Extend the SP-2 section with:
- Historical-snapshot back-depth (2021-05+).
- Quarantine recovery flow.
- The single documented append-only exception (`delete_matching` in PROMOTE on zero-existing partitions).

- [ ] **Step 5: Memory updates**

Modify existing:
```markdown
# /root/.claude/projects/-root/memory/feedback_never_delete_master_data.md
... existing content ...

**SP-2 Phase B documented exception (2026-XX-XX):**
`pyarrow.parquet.write_to_dataset(existing_data_behavior='delete_matching')`
is the ONLY permitted relaxation, and ONLY when invoked from
`scripts/backfill_universe_5y.py` PROMOTE step on a partition that has
zero existing rows for the target ticker. v2+ recoveries additionally
require `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1` + a paired `data_quarantine`
supersede row. Every promote attempt is recorded in `backfill_audit`.
```

Create:
```markdown
# /root/.claude/projects/-root/memory/project_sp2_phase_b_backfill.md
---
name: project-sp2-phase-b-backfill
description: "SP-2 Phase B shipped. 5y × ~3000-ticker backfill via stage→validate→promote with Redis ckpts + backfill_audit. data_quarantine filter wired into all 5 backtest engines + resolver + collector."
metadata: {node_type: memory, type: project}
---

...
```

Update MEMORY.md index.

- [ ] **Step 6: Commit**

```bash
git add docs/sp2-backfill-runbook.md .env.example CLAUDE.md ARCHITECTURE.md
git add /root/.claude/projects/-root/memory/feedback_never_delete_master_data.md
git add /root/.claude/projects/-root/memory/project_sp2_phase_b_backfill.md
git add /root/.claude/projects/-root/memory/MEMORY.md
git commit -m "docs(sp2-b): runbook + .env.example + CLAUDE.md + ARCHITECTURE.md + memory"
```

---

## Task 14: PR + soak

- [ ] **Step 1: Final full test sweep**

```bash
python3 -m pytest tests/ --ignore=tests/integration_test.py -x --tb=short 2>&1 | tail -50
node test/graph-smoke.js
python3 -m src.system_checks
```

Expect: all SP-2 tests green; same 11 pre-existing failures as Phase A (regression-tracked).

- [ ] **Step 2: Push + open PR**

```bash
git push -u origin feat/sp2-phase-b-5y-backfill
gh pr create --base main --head feat/sp2-phase-b-5y-backfill \
  --title "SP-2 Phase B: 5y backfill (stage→validate→promote + quarantine)" \
  --body "$(cat <<'EOF'
## Summary
- New driver scripts/backfill_universe_5y.py — stage→validate→promote loop for prices/metadata/options targets. Resumable (Redis chunk ckpts + backfill_audit forensic log). Throttled via existing rate_limiter.
- One-shot artifacts: data/.backfill_universe_v1.txt (frozen top-3000 by ADV); data/sp500_historical_membership_v1.csv (Wikipedia-sourced).
- data_quarantine filter wired into universe_resolver.coverage_floor + all 5 backtest engines + collector.js — provides the ONLY recovery path for master-parquet poisoning (read-time filter; no DELETE).
- The single documented exception to the append-only invariant is delete_matching in PROMOTE step on zero-existing partitions; v2+ recovery requires OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 + paired quarantine supersede row.
- Migration 115 (backfill_audit). New doctor checks (backfill_progress, backfill_universe_coverage; both slow=True). New system_checks (backfill_progress on storage tag, ticker_metadata_history_depth on strategies tag).
- Dashboards: operator /api/backfill-progress tile + user Data Health backfill-history sparkline.
- ticker_metadata_writer.py DRY-refactored to call shared build_month_snapshot.

Spec: docs/superpowers/specs/2026-05-22-sp2-phase-b-5y-backfill-design.md
Plan: docs/superpowers/plans/2026-05-22-sp2-phase-b-5y-backfill.md

## Test plan
- [ ] pytest tests/ green (SP-2 + regression suite); pre-existing 11 unrelated failures unchanged
- [ ] node smokes green
- [ ] Doctor exit 0 with new checks
- [ ] system_checks --tag storage / --tag strategies PASS
- [ ] Soak A (code-only, 3d): doctor green, live cycle latency unchanged
- [ ] Soak B (backfill run, 14d): ≤ 50 quarantines total; spot-audit < 1 mismatch in 20 samples; ticker_metadata_history_depth PASS at ≥ 5y

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Operator pre-deploy checklist**

Run §6.3 of the spec. Confirm disk + FMP headroom. Create `#backfill-log` Discord. Paste webhook in `.env`.

- [ ] **Step 4: After merge — Soak A then kickoff**

```bash
# On VPS, after merge
ssh vps
cd /root/openclaw && git checkout main && git pull
node -e "require('./src/database/postgres').migrate().then(()=>process.exit(0)).catch(e=>{console.error(e);process.exit(1)})"
systemctl restart johnbot.service fundjohn-dashboard.service
python3 -m src.maintenance.doctor --required-only

# Soak A — 3 days no backfill, then:
export OPENCLAW_BACKFILL_5Y_ACTIVE=1
nohup python3 scripts/backfill_universe_5y.py --target prices --resume \
  > /var/log/backfill_prices.log 2>&1 &
# ... after prices completes ...
nohup python3 scripts/backfill_universe_5y.py --target metadata --resume \
  > /var/log/backfill_metadata.log 2>&1 &
```

- [ ] **Step 5: Soak B monitoring (14 days)**

Daily:
- Doctor `_check_backfill_progress` PASS.
- `#backfill-log` digest in Discord reviewed.
- Quarantine count audited.
- Random spot-audit (20 samples) at the end.

If any criterion fails 2 days running → rollback ladder Level 3 (`OPENCLAW_PARQUET_FILTER_BACKFILL_ROWS=1`).

---

## Out of Scope for Phase B

- Full 5y options-EOD backfill (SP-3 alongside crypto/commodity).
- Tick-level data.
- Fundamentals history beyond `financials.parquet`.
- Re-fetching existing prices with delta semantics (would breach append-only).
- Resampled-bar generation (`prices_30m.parquet`).
- Wikipedia historical SP500 membership scraper as a cron job.
- Mastermind `mode=universe-recs` (Phase C).
- PaperHunter / StrategyCoder predicate emission (Phase D).

---

## Spec coverage cross-check

| Spec §  | Topic | Task(s) |
|---|---|---|
| 1.1 | Anchored facts | Task 0 verification |
| 1.2 | Decisions locked | Tasks 1-9 implementations |
| 2.1 | Stage→Validate→Promote | Tasks 6-9 |
| 2.2.1 | --target prices | Task 7 |
| 2.2.2 | --target metadata | Task 8 |
| 2.2.3 | --target options | Task 9 |
| 2.3 | Quarantine + recovery | Tasks 4-5 |
| 2.4 | Doctor + system_checks | Task 10 |
| 2.5 | Dashboard tiles | Task 11 |
| 3.1 | New files | Tasks 1-12 |
| 3.2 | Modified files | Tasks 5, 8, 10, 11 |
| 3.3 | .env changes | Task 13 |
| 3.4 | Migration 115 | Task 3 |
| 3.5 | Memory + docs | Task 13 |
| 4.1 | Initial backfill flow | Task 14 |
| 4.2 | Day-N steady state | Task 8 step 3 (DRY refactor) |
| 4.3 | Resume / partial-failure | Task 6 + Task 10 |
| 5 | Phase ordering | Task sequencing |
| 6.1 | Failure-mode matrix | Tasks 7-9 (per-target error paths) |
| 6.2 | Rollback ladder | Task 13 (runbook + env gates) |
| 6.3 | Pre-deploy checklist | Task 14 |
| 7.1 | Unit tests | each task |
| 7.2 | Integration smoke | Task 12 |
| 7.3 | Soak A/B | Task 14 |
