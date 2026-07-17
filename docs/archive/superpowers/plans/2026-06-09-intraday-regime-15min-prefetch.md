# Intraday Regime 15-min Cadence + Prefetch/Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut intraday redeploy churn (15-min ticks, uniform 3-tick confirmation, no cooldown) and eliminate the stale-price anchor by prefetching prices on the first candidate tick and gating the tick-3 redeploy on fresh data.

**Architecture:** A single default-OFF flag `OPENCLAW_INTRADAY_15MIN_PREFETCH` gates the whole feature. The detector (`run_intraday_market_state.py`) detects a candidate transition on tick-1 and spawns a prices-only refetch (`refetch_prices.py`) tracked by a Redis sentinel (`src/execution/intraday_prefetch.py`). On tick-3 confirmation the redeploy (`redeploy_pipeline.py`) waits on that sentinel (bounded) and aborts on failure/timeout/stale — never trading on bad data.

**Tech Stack:** Python 3 (psycopg2, redis, pandas, pytest), Node (cron-schedule.js, collector.js), Redis, PostgreSQL, Discord webhooks.

**Spec:** `docs/superpowers/specs/2026-06-09-intraday-regime-15min-prefetch-design.md`

**Constraints (READ FIRST):**
- VPS is 2-core — run tasks/tests **sequentially**; never fan out parallel pytest.
- Master parquets are **append-only** (CLAUDE.md) — refetch uses dedup keep-last, never deletes.
- Do **not** `git reset --hard`; do **not** restart johnbot (operator-gated).
- Branch: `feat/intraday-regime-15min-prefetch` (already created, spec committed `ac1e24b`).
- Tests use `fakeredis` if available, else a minimal in-test fake; do NOT touch live Redis/DB in unit tests.

---

## Flag semantics (referenced by every task)

`OPENCLAW_INTRADAY_15MIN_PREFETCH == '1'` → new behavior. Anything else (unset/`0`) → today's exact behavior. Provide one helper, imported everywhere:

```python
# src/execution/intraday_prefetch.py  (top of file — see Task 2)
import os
def prefetch_enabled() -> bool:
    return os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') == '1'
```

---

## Task 1: Flag-gated cadence (cron) + feature-floor

**Files:**
- Modify: `src/engine/cron-schedule.js` (intraday tick `cron.schedule('*/5 9-19 * * 1-5', …)`)
- Modify: `src/ingestion/intraday_features.py` (`_floor_ts`/`ts.floor('5min')`)
- Test: `tests/test_intraday_15min_prefetch.py` (feature-floor portion)

- [ ] **Step 1: Write the failing test** (`tests/test_intraday_15min_prefetch.py`)

```python
import importlib, os
import pandas as pd

def _reload_features():
    import src.ingestion.intraday_features as f
    return importlib.reload(f)

def test_feature_floor_is_15min_when_flag_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    f = _reload_features()
    ts = pd.Timestamp('2026-06-09 14:07:00', tz='UTC')
    assert f._floor_ts(ts) == pd.Timestamp('2026-06-09 14:00:00', tz='UTC')
    ts2 = pd.Timestamp('2026-06-09 14:22:00', tz='UTC')
    assert f._floor_ts(ts2) == pd.Timestamp('2026-06-09 14:15:00', tz='UTC')

def test_feature_floor_is_5min_when_flag_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', raising=False)
    f = _reload_features()
    ts = pd.Timestamp('2026-06-09 14:07:00', tz='UTC')
    assert f._floor_ts(ts) == pd.Timestamp('2026-06-09 14:05:00', tz='UTC')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_15min_prefetch.py -k feature_floor -v`
Expected: FAIL (`_floor_ts` not defined, or returns 5-min flooring under flag).

- [ ] **Step 3: Implement the feature-floor change**

In `src/ingestion/intraday_features.py`, replace the existing inline `ts.floor('5min')` with a helper. Find the current flooring (around the `_floor_ts`/collect path) and introduce:

```python
import os

def _floor_ts(ts):
    """Bucket the feature timestamp. 15-min when the 15min-prefetch flag is
    ON (so 15-min ticks dedup cleanly), else legacy 5-min."""
    freq = '15min' if os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') == '1' else '5min'
    return ts.floor(freq)
```

Replace the call site (was `ts.floor('5min')`) with `_floor_ts(ts)`. Update the module docstring line 1 from "5-min intraday feature collector" → "intraday feature collector (5-min legacy / 15-min when OPENCLAW_INTRADAY_15MIN_PREFETCH=1)".

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_15min_prefetch.py -k feature_floor -v`
Expected: PASS (both).

- [ ] **Step 5: Cadence in cron-schedule.js**

In `src/engine/cron-schedule.js`, replace the hardcoded intraday schedule. Find:

```js
    cron.schedule('*/5 9-19 * * 1-5', () => {
```

Replace with a flag-conditional expression (cron registered once at boot; flipping the flag already needs a restart):

```js
    const _intradaySchedule = process.env.OPENCLAW_INTRADAY_15MIN_PREFETCH === '1'
        ? '*/15 9-19 * * 1-5'   // 15-min cadence (uniform 3-tick confirmation + prefetch)
        : '*/5 9-19 * * 1-5';   // legacy 5-min cadence
    cron.schedule(_intradaySchedule, () => {
```

Update the adjacent comment block: note the 15-min cadence and that confirmation is uniform 3-tick when the flag is on. `node --check src/engine/cron-schedule.js` must pass.

- [ ] **Step 6: Verify JS parses + commit**

Run: `cd /root/openclaw && node --check src/engine/cron-schedule.js && echo OK`
Expected: `OK`

```bash
git add tests/test_intraday_15min_prefetch.py src/ingestion/intraday_features.py src/engine/cron-schedule.js
git commit -m "feat(intraday): flag-gated 15-min cadence + 15-min feature floor"
```

---

## Task 2: Prefetch sentinel + freshness + in-flight lock helper

**Files:**
- Create: `src/execution/intraday_prefetch.py`
- Test: `tests/test_intraday_prefetch_helper.py`

Single source of truth for the Redis sentinel + freshness + in-flight lock. Keys:
- Prefetch sentinel: `intraday:prefetch:{date}` → JSON `{status, target_state, episode, started_at, finished_at, n_tickers, error}` (`status` ∈ `running|done|failed`).
- In-flight lock: `intraday:redeploy:inflight` (value `1`, TTL 900s).

- [ ] **Step 1: Write the failing test** (`tests/test_intraday_prefetch_helper.py`)

```python
import json
import src.execution.intraday_prefetch as p

class FakeRedis:
    def __init__(self): self.kv = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)

def test_sentinel_roundtrip():
    r = FakeRedis()
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL', episode='2026-06-09:HIGH_VOL:t0')
    s = p.read_prefetch(r, '2026-06-09')
    assert s['status'] == 'running' and s['target_state'] == 'HIGH_VOL'
    p.set_prefetch_done(r, '2026-06-09', n_tickers=503)
    s = p.read_prefetch(r, '2026-06-09')
    assert s['status'] == 'done' and s['n_tickers'] == 503

def test_failed_status():
    r = FakeRedis()
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL', episode='e')
    p.set_prefetch_failed(r, '2026-06-09', error='conn loss')
    assert p.read_prefetch(r, '2026-06-09')['status'] == 'failed'

def test_should_prefetch_debounce():
    r = FakeRedis()
    # No sentinel → should prefetch
    assert p.should_prefetch(r, '2026-06-09', episode='e1') is True
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL', episode='e1')
    # Same episode already running → debounce (skip)
    assert p.should_prefetch(r, '2026-06-09', episode='e1') is False
    # Different episode → prefetch again
    assert p.should_prefetch(r, '2026-06-09', episode='e2') is True

def test_inflight_lock():
    r = FakeRedis()
    assert p.acquire_inflight(r) is True     # first acquire wins
    assert p.acquire_inflight(r) is False    # second blocked
    p.release_inflight(r)
    assert p.acquire_inflight(r) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_prefetch_helper.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `src/execution/intraday_prefetch.py`**

```python
"""src/execution/intraday_prefetch.py — coordination for the tick-1 price
prefetch and the tick-3 data-ready gate (OPENCLAW_INTRADAY_15MIN_PREFETCH).

State lives in two Redis keys:
  intraday:prefetch:{date}   — JSON sentinel for the prices-only refetch
  intraday:redeploy:inflight — single-in-flight lock (replaces the old cooldown)
"""
import json
import os

PREFETCH_KEY = 'intraday:prefetch:{date}'
INFLIGHT_KEY = 'intraday:redeploy:inflight'
PREFETCH_TTL_S = 6 * 3600        # sentinel lives the trading day
INFLIGHT_TTL_S = 900             # 15 min backstop


def prefetch_enabled() -> bool:
    return os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') == '1'


def _k(date: str) -> str:
    return PREFETCH_KEY.format(date=date)


def read_prefetch(r, date: str) -> dict | None:
    if r is None:
        return None
    raw = r.get(_k(date))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _write(r, date: str, payload: dict) -> None:
    if r is None:
        return
    r.set(_k(date), json.dumps(payload), ex=PREFETCH_TTL_S)


def set_prefetch_running(r, date: str, *, target_state: str, episode: str,
                         started_at: str | None = None) -> None:
    _write(r, date, {
        'status': 'running', 'target_state': target_state,
        'episode': episode, 'started_at': started_at,
    })


def set_prefetch_done(r, date: str, *, n_tickers: int,
                      finished_at: str | None = None) -> None:
    cur = read_prefetch(r, date) or {}
    cur.update({'status': 'done', 'n_tickers': n_tickers,
                'finished_at': finished_at})
    _write(r, date, cur)


def set_prefetch_failed(r, date: str, *, error: str,
                        finished_at: str | None = None) -> None:
    cur = read_prefetch(r, date) or {}
    cur.update({'status': 'failed', 'error': str(error)[:300],
                'finished_at': finished_at})
    _write(r, date, cur)


def should_prefetch(r, date: str, *, episode: str) -> bool:
    """Debounce: prefetch only if there is no sentinel for THIS episode that
    is already running or done. A different episode (new candidate) re-fires."""
    s = read_prefetch(r, date)
    if not s:
        return True
    if s.get('episode') != episode:
        return True
    return s.get('status') not in ('running', 'done')


def acquire_inflight(r) -> bool:
    """True if we took the lock; False if a redeploy is already in flight."""
    if r is None:
        return True   # no redis → can't coordinate; let the spawn proceed
    return bool(r.set(INFLIGHT_KEY, '1', nx=True, ex=INFLIGHT_TTL_S))


def release_inflight(r) -> None:
    if r is not None:
        try:
            r.delete(INFLIGHT_KEY)
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_prefetch_helper.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/execution/intraday_prefetch.py tests/test_intraday_prefetch_helper.py
git commit -m "feat(intraday): prefetch sentinel + freshness + in-flight lock helper"
```

---

## Task 3: Prices-only refetch entrypoint (`scripts/refetch_prices.py`)

**Files:**
- Create: `scripts/refetch_prices.py`
- Test: `tests/test_refetch_prices.py`

Contract: set sentinel `running` → run the collector's **price-fill only** for the union universe (delegates to the JS collector via `node scripts/run_collector_once.js --prices-only`; **if that flag does not exist, add a minimal `--prices-only` branch to `run_collector_once.js` that runs only the price-fill stage and exits**) → verify exit code 0 AND a freshness check on `prices.parquet`/`data_coverage` → set sentinel `done` (with n_tickers) or `failed`. Exit 0 on success, 1 on failure. Never deletes rows (collector append-dedup keep-last is the only writer).

- [ ] **Step 1: Write the failing test** (`tests/test_refetch_prices.py`)

```python
import types
import scripts.refetch_prices as rp

class FakeRedis:
    def __init__(self): self.kv = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)

def test_refetch_success_writes_done(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rp, '_redis', lambda: r)
    monkeypatch.setattr(rp, '_run_price_fill', lambda date: 0)          # collector rc=0
    monkeypatch.setattr(rp, '_freshness_ok', lambda date: (True, 503))  # fresh, 503 tickers
    rc = rp.run('2026-06-09')
    assert rc == 0
    import src.execution.intraday_prefetch as p
    s = p.read_prefetch(r, '2026-06-09')
    assert s['status'] == 'done' and s['n_tickers'] == 503

def test_refetch_collector_failure_writes_failed(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rp, '_redis', lambda: r)
    monkeypatch.setattr(rp, '_run_price_fill', lambda date: 1)          # collector rc!=0
    monkeypatch.setattr(rp, '_freshness_ok', lambda date: (False, 0))
    rc = rp.run('2026-06-09')
    assert rc == 1
    import src.execution.intraday_prefetch as p
    assert p.read_prefetch(r, '2026-06-09')['status'] == 'failed'

def test_refetch_stale_after_success_writes_failed(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rp, '_redis', lambda: r)
    monkeypatch.setattr(rp, '_run_price_fill', lambda date: 0)          # rc=0 but...
    monkeypatch.setattr(rp, '_freshness_ok', lambda date: (False, 0))   # ...data stale/partial
    rc = rp.run('2026-06-09')
    assert rc == 1
    import src.execution.intraday_prefetch as p
    assert p.read_prefetch(r, '2026-06-09')['status'] == 'failed'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_refetch_prices.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `scripts/refetch_prices.py`**

```python
#!/usr/bin/env python3
"""scripts/refetch_prices.py — prices-only intraday refetch for the regime
prefetch (OPENCLAW_INTRADAY_15MIN_PREFETCH). Sets the prefetch sentinel,
delegates the actual fetch to the JS collector's price-fill stage, then
verifies freshness. Never deletes master rows (collector is append-dedup).

Exit 0 = fresh prices written + sentinel 'done'. Exit 1 = sentinel 'failed'.
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.execution import intraday_prefetch as p   # noqa: E402

FRESHNESS_TABLE_DAYS = 1   # union prices must cover within today


def _redis():
    try:
        import redis
        url = os.environ.get('REDIS_URL')
        if not url:
            return None
        return redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
    except Exception:
        return None


def _run_price_fill(date: str) -> int:
    """Invoke the JS collector's prices-only stage. Returns its exit code."""
    cmd = ['node', str(ROOT / 'scripts' / 'run_collector_once.js'), '--prices-only']
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), timeout=20 * 60,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            print(proc.stdout.decode()[-2000:])
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124


def _freshness_ok(date: str) -> tuple[bool, int]:
    """True + covered-ticker-count if data_coverage shows union prices updated
    to within FRESHNESS_TABLE_DAYS of `date`."""
    try:
        import psycopg2
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            return (False, 0)
        cutoff = (dt.date.fromisoformat(date) - dt.timedelta(days=FRESHNESS_TABLE_DAYS)).isoformat()
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM data_coverage WHERE data_type='prices' AND date_to >= %s",
            (cutoff,))
        n = int(cur.fetchone()[0])
        cur.close(); conn.close()
        return (n > 0, n)
    except Exception as e:
        print(f'[refetch] freshness check error: {e}')
        return (False, 0)


def run(date: str) -> int:
    r = _redis()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    # episode is owned by the caller (detector); here we just mark running if
    # nothing newer exists, so a synchronous-fallback call also records state.
    if p.read_prefetch(r, date) is None:
        p.set_prefetch_running(r, date, target_state='(refetch)',
                               episode=f'{date}:refetch', started_at=now)
    rc = _run_price_fill(date)
    fresh, n = _freshness_ok(date)
    fin = dt.datetime.now(dt.timezone.utc).isoformat()
    if rc == 0 and fresh:
        p.set_prefetch_done(r, date, n_tickers=n, finished_at=fin)
        print(f'[refetch] done n_tickers={n}')
        return 0
    p.set_prefetch_failed(r, date, error=f'rc={rc} fresh={fresh}', finished_at=fin)
    print(f'[refetch] FAILED rc={rc} fresh={fresh}')
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=dt.date.today().isoformat())
    args = ap.parse_args(argv)
    return run(args.date)


if __name__ == '__main__':
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_refetch_prices.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire `--prices-only` into the JS collector (only if missing)**

Inspect `scripts/run_collector_once.js`. If it does not accept `--prices-only`, add a branch that runs ONLY the collector's price-fill stage (the function `collector.js` uses to fill `prices.parquet` for the union universe) and exits with its status — skipping options/fundamentals/macro/sentiment. Keep existing default behavior unchanged. Verify: `node --check scripts/run_collector_once.js && echo OK`.

- [ ] **Step 6: Commit**

```bash
git add scripts/refetch_prices.py tests/test_refetch_prices.py scripts/run_collector_once.js
git commit -m "feat(intraday): prices-only refetch entrypoint + sentinel writes"
```

---

## Task 3.5: Intraday-snapshot price fetch (all-asset) + repoint refetch + tighten freshness

**Why:** Re-running the daily collect intraday writes 0 rows for today (Alpaca daily bars finalize post-close), so it can't deliver today's price. Per operator decision (spec Addendum 2026-06-09), the prefetch must fetch today's INTRADAY snapshot for ALL asset classes so signals/brackets use the live price.

**Files:**
- Modify: `src/pipeline/collector.js` (add `runIntradaySnapshotPrices`)
- Modify: `src/pipeline/run_collector_once.js` (add `--intraday-snapshot` flag)
- Modify: `scripts/refetch_prices.py` (`_run_price_fill` → `--intraday-snapshot`; `_freshness_ok` → require today)
- Test: `tests/test_intraday_snapshot_parse.py`, extend `tests/test_refetch_prices.py`

- [ ] **Step 1: Failing test for the snapshot→row parser** (`tests/test_intraday_snapshot_parse.py`)

Add a pure exported parser to collector.js is hard to unit-test from Python; instead implement the parser as a small pure JS function and test it with a node assertion script. Create `tests/test_intraday_snapshot_parse.py` that shells out:

```python
import subprocess, pathlib
ROOT = pathlib.Path('/root/openclaw')

def test_snapshot_dailybar_to_row_via_node():
    # Exercise the pure parser exported from collector.js
    script = r'''
      const { _snapshotToPriceRow } = require('./src/pipeline/collector.js');
      const snap = { dailyBar: { o:743.63, h:746.9, l:722.59, c:733.96, v:63273993 } };
      const row = _snapshotToPriceRow('SPY', snap, '2026-06-09');
      const ok = row && row.ticker==='SPY' && row.date==='2026-06-09'
                 && row.close===733.96 && row.open===743.63 && row.high===746.9
                 && row.low===722.59 && row.volume===63273993;
      const none = _snapshotToPriceRow('NODAILY', { dailyBar: null }, '2026-06-09');
      if (!ok) { console.error('row mismatch', JSON.stringify(row)); process.exit(1); }
      if (none !== null) { console.error('expected null for missing dailyBar'); process.exit(1); }
      console.log('OK');
    '''
    r = subprocess.run(['node','-e',script], cwd=str(ROOT), capture_output=True, text=True)
    assert 'OK' in r.stdout, r.stdout + r.stderr
```

- [ ] **Step 2: Run → FAIL** (`_snapshotToPriceRow` not exported). `cd /root/openclaw && python3 -m pytest tests/test_intraday_snapshot_parse.py -v`

- [ ] **Step 3: Implement `runIntradaySnapshotPrices` + parser in `collector.js`**

Add a pure, exported helper and the fetch function. The parser maps one Alpaca snapshot's `dailyBar` to a price row (returns null if no dailyBar):

```js
// Pure: snapshot.dailyBar (today's partial OHLCV) → prices.parquet row, or null.
function _snapshotToPriceRow(ticker, snap, dateStr) {
  const db = snap && snap.dailyBar;
  if (!db || db.c == null) return null;
  return { ticker, date: dateStr, open: db.o, high: db.h, low: db.l, close: db.c, volume: db.v };
}
```

`runIntradaySnapshotPrices(tickers)`:
- Resolve the union universe like `runHistoricalPrices` (reuse the same envelope/tickers source).
- **Equity + ETF:** batch `alpaca data multi-snapshots --symbols <chunk>` (~150/chunk); for each symbol map `_snapshotToPriceRow(sym, snap, todayET)`; collect non-null rows.
- **Crypto:** mirror `fillPricesAlpacaCrypto`'s symbol mapping (BTC-USD→BTC/USD) using Alpaca crypto latest/snapshot bars; map today's bar to a row.
- **Indices/forex:** mirror `runMarketPricesNonEquity`/`fillPricesFmpHistorical` using FMP real-time quote for `^GSPC/^VIX/…`/`EURUSD`; if unavailable for a class, log and skip that class (non-fatal).
- Write all rows via the SAME flush/dedup path `runHistoricalPrices` uses (append-dedup keep-last on `(ticker,date)`, then `store.flushPrices()`), and update `data_coverage` so `date_to=today` for written tickers.
- Return a count of rows written. Log a one-line summary. No Discord post.
- Export both `runIntradaySnapshotPrices` and `_snapshotToPriceRow`.

Today's ET date: derive the trading date in ET (reuse the collector's existing ET-date helper if present; else `new Date()` → America/New_York `YYYY-MM-DD`).

- [ ] **Step 4: Run parser test → PASS.** `cd /root/openclaw && python3 -m pytest tests/test_intraday_snapshot_parse.py -v`

- [ ] **Step 5: Add `--intraday-snapshot` flag to `run_collector_once.js`**

Mirror the `--prices-only` early-return branch, but call `collector.runIntradaySnapshotPrices()` (NOT `runEodRefresh`). `process.exit(0)` on success, `1` on error. No Discord. Default behavior unchanged. Verify `node --check src/pipeline/run_collector_once.js && echo OK`.

- [ ] **Step 6: Repoint `refetch_prices.py` + tighten freshness**

- `_run_price_fill`: change the node arg `--prices-only` → `--intraday-snapshot`.
- `_freshness_ok`: change the cutoff so it requires TODAY's coverage — `cutoff = date` (i.e., `WHERE data_type='prices' AND date_to >= %s` with `%s = date`), and update the module comment (the snapshot fetch now writes today's row, so the date−1 lag rationale no longer applies). Keep the `(bool, int)` return shape.
- Update/extend `tests/test_refetch_prices.py`: the existing monkeypatched tests still pass (they patch `_freshness_ok`). Add `test_freshness_requires_today` that monkeypatches `psycopg2`? — simpler: assert the SQL cutoff equals `date` by refactoring the cutoff into a tiny pure helper `_freshness_cutoff(date)` returning `date` and test `rp._freshness_cutoff('2026-06-09') == '2026-06-09'`.

- [ ] **Step 7: Run the refetch suite → PASS.** `cd /root/openclaw && python3 -m pytest tests/test_refetch_prices.py tests/test_intraday_snapshot_parse.py -v`

- [ ] **Step 8: Commit**

```bash
git add src/pipeline/collector.js src/pipeline/run_collector_once.js scripts/refetch_prices.py tests/test_intraday_snapshot_parse.py tests/test_refetch_prices.py
git commit -m "feat(intraday): all-asset intraday-snapshot price fetch + require-today freshness"
```

**Note:** A live smoke (does `runIntradaySnapshotPrices` actually write today's rows + advance data_coverage?) is deferred to the operator-gated post-implementation step — unit tests here cover the pure parser + freshness contract; the live CLI calls are not exercised in unit tests.

---

## Task 4: Uniform 3-tick confirmation (flag-gated)

**Files:**
- Modify: `scripts/run_intraday_market_state.py:528-533` (`_tier_for_transition`)
- Test: `tests/test_intraday_15min_prefetch.py` (confirmation portion)

- [ ] **Step 1: Write the failing test** (append to `tests/test_intraday_15min_prefetch.py`)

```python
import importlib

def _reload_detector(monkeypatch, flag):
    if flag: monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    else: monkeypatch.delenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', raising=False)
    import importlib.util, sys
    from pathlib import Path
    path = Path('/root/openclaw/scripts/run_intraday_market_state.py')
    spec = importlib.util.spec_from_file_location('rims', path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def test_uniform_3tick_when_flag_on(monkeypatch):
    m = _reload_detector(monkeypatch, flag=True)
    # Upward LOW_VOL->HIGH_VOL would be 2 ticks under tiers; uniform => 3
    assert m._tier_for_transition('LOW_VOL', 'HIGH_VOL') == (3, 0.70)
    assert m._tier_for_transition('TRANSITIONING', 'CRISIS') == (3, 0.70)
    assert m._tier_for_transition('CRISIS', 'LOW_VOL') == (3, 0.70)

def test_tiered_when_flag_off(monkeypatch):
    m = _reload_detector(monkeypatch, flag=False)
    assert m._tier_for_transition('LOW_VOL', 'HIGH_VOL') == (2, 0.80)
    assert m._tier_for_transition('TRANSITIONING', 'CRISIS') == (1, 0.90)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_15min_prefetch.py -k tick_when -v`
Expected: FAIL (uniform case returns tiered values).

- [ ] **Step 3: Implement the uniform tier**

In `scripts/run_intraday_market_state.py`, modify `_tier_for_transition` (lines 528-533):

```python
def _tier_for_transition(prior_state: str | None, new_state: str) -> tuple[int, float]:
    """Return (required_ticks, required_confidence) for transition into new_state.
    With OPENCLAW_INTRADAY_15MIN_PREFETCH=1, ALL transitions require a uniform
    3-tick (45-min) confirmation at the 0.70 floor — the 15-min cadence already
    filters noise, so we drop the faster upward tiers. Flag OFF = legacy tiers."""
    import os
    if os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') == '1':
        return (3, CONFIDENCE_FLOOR)
    if _is_upward(prior_state, new_state):
        return HYSTERESIS_TIERS.get(new_state, _DOWNWARD_TIER)
    return _DOWNWARD_TIER
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_15min_prefetch.py -k tick_when -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add scripts/run_intraday_market_state.py tests/test_intraday_15min_prefetch.py
git commit -m "feat(intraday): uniform 3-tick confirmation when 15min flag on"
```

---

## Task 5: Drop redeploy cooldown + in-flight lock (detector)

**Files:**
- Modify: `scripts/run_intraday_market_state.py:797-857` (cooldown read + spawn block)
- Test: `tests/test_intraday_cooldown_inflight.py`

Behavior when flag ON: do NOT read `redeploy:cooldown:{date}` (still read `liquidate:cooldown:{date}`); guard the spawn with `acquire_inflight`. When flag OFF: unchanged (both cooldown keys honored).

- [ ] **Step 1: Write the failing test** (`tests/test_intraday_cooldown_inflight.py`)

```python
import importlib.util
from pathlib import Path

def _load(monkeypatch, flag):
    if flag: monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    else: monkeypatch.delenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', raising=False)
    spec = importlib.util.spec_from_file_location(
        'rims', Path('/root/openclaw/scripts/run_intraday_market_state.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def test_redeploy_cooldown_ignored_when_flag_on(monkeypatch):
    m = _load(monkeypatch, flag=True)
    # only liquidate cooldown should block; redeploy cooldown must NOT
    assert m._cooldown_active({'redeploy:cooldown:2026-06-09': '1'}, '2026-06-09') is False
    assert m._cooldown_active({'liquidate:cooldown:2026-06-09': '1'}, '2026-06-09') is True

def test_both_cooldowns_block_when_flag_off(monkeypatch):
    m = _load(monkeypatch, flag=False)
    assert m._cooldown_active({'redeploy:cooldown:2026-06-09': '1'}, '2026-06-09') is True
    assert m._cooldown_active({'liquidate:cooldown:2026-06-09': '1'}, '2026-06-09') is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_cooldown_inflight.py -v`
Expected: FAIL (`_cooldown_active` not defined).

- [ ] **Step 3: Extract + flag-gate the cooldown check, add in-flight lock**

In `scripts/run_intraday_market_state.py`, add a pure helper near the other gate helpers:

```python
def _cooldown_active(kv: dict, date_str: str) -> bool:
    """Which cooldown keys block a confirmed-transition redeploy.
    Flag ON: only a manual liquidate cooldown blocks (the 60-min redeploy
    cooldown is dropped — 45-min 3-tick persistence is the throttle).
    Flag OFF: legacy — both redeploy and liquidate cooldowns block."""
    import os
    keys = [f'liquidate:cooldown:{date_str}']
    if os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') != '1':
        keys.append(f'redeploy:cooldown:{date_str}')
    return any(kv.get(k) for k in keys)
```

Then replace the inline cooldown loop (lines ~809-817) so it builds `kv` from Redis for the relevant keys and calls `_cooldown_active(kv, date_str)`. In the `else:` (fire) branch, wrap the spawn with the in-flight lock:

```python
            from src.execution.intraday_prefetch import acquire_inflight, release_inflight, prefetch_enabled
            if prefetch_enabled() and not acquire_inflight(rcli):
                logger.info('redeploy skipped — another redeploy in flight')
                transition_tag = f'INTRADAY_HMM_{prior_state}_{state_name}_INFLIGHT'
            else:
                _sync_regime_to_consumers(...)           # unchanged call
                spawn_kind = _spawn_redeploy(...)         # unchanged call
                fired_liquidation = True
                # ... existing logging + Discord post ...
```

(The detached redeploy releases the lock on completion — Task 7 — and the 900s TTL is the backstop. Do NOT release it here in the parent.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_cooldown_inflight.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add scripts/run_intraday_market_state.py tests/test_intraday_cooldown_inflight.py
git commit -m "feat(intraday): drop redeploy cooldown + add in-flight lock (flag-gated)"
```

---

## Task 6: Tick-1 candidate prefetch trigger + Discord (detector)

**Files:**
- Modify: `scripts/run_intraday_market_state.py` (`run_one_tick`, after `streak`/`fired` computed ~line 785)
- Test: `tests/test_intraday_candidate_trigger.py`

A **candidate** = `state != settled` AND `streak == 1` AND market open AND `confidence >= CONFIDENCE_FLOOR`. On a candidate (flag ON), set sentinel `running` + spawn `refetch_prices.py` detached (debounced by episode) + post a `(1/3)` Discord line.

- [ ] **Step 1: Write the failing test** (`tests/test_intraday_candidate_trigger.py`)

```python
import importlib.util
from pathlib import Path

def _load(monkeypatch):
    monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    spec = importlib.util.spec_from_file_location(
        'rims', Path('/root/openclaw/scripts/run_intraday_market_state.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def test_is_candidate_true_on_first_new_tick(monkeypatch):
    m = _load(monkeypatch)
    assert m._is_candidate_transition(
        settled='LOW_VOL', state='HIGH_VOL', streak=1,
        confidence=0.95, market_open=True) is True

def test_is_candidate_false_when_streak_gt1_or_lowconf_or_closed_or_same(monkeypatch):
    m = _load(monkeypatch)
    assert m._is_candidate_transition('LOW_VOL', 'HIGH_VOL', 2, 0.95, True) is False   # streak>1
    assert m._is_candidate_transition('LOW_VOL', 'HIGH_VOL', 1, 0.50, True) is False   # low conf
    assert m._is_candidate_transition('LOW_VOL', 'HIGH_VOL', 1, 0.95, False) is False  # closed
    assert m._is_candidate_transition('HIGH_VOL', 'HIGH_VOL', 1, 0.95, True) is False  # same
    assert m._is_candidate_transition(None, 'HIGH_VOL', 1, 0.95, True) is False        # no settled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_candidate_trigger.py -v`
Expected: FAIL (`_is_candidate_transition` not defined).

- [ ] **Step 3: Implement candidate predicate + trigger**

Add the predicate near `_confirmed_transition`:

```python
def _is_candidate_transition(settled, state, streak, confidence, market_open) -> bool:
    """First-tick signal of a (not-yet-confirmed) transition — the trigger to
    warm a prices-only refetch so data is fresh by the 3rd-tick confirmation."""
    return (
        market_open
        and settled is not None
        and state != settled
        and streak == 1
        and confidence >= CONFIDENCE_FLOOR
    )
```

Add a detached spawner mirroring `_spawn_redeploy`:

```python
def _spawn_refetch_prices(date_str: str) -> str:
    """Spawn scripts/refetch_prices.py DETACHED (fire-and-forget)."""
    log_path = ROOT / 'logs' / f'refetch_prices_{date_str}.log'
    cmd = [sys.executable, str(ROOT / 'scripts' / 'refetch_prices.py'), '--date', date_str]
    try:
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except Exception:
        fd = subprocess.DEVNULL
    try:
        subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         stdout=fd, stderr=fd, start_new_session=True, close_fds=True)
    except Exception as e:
        logger.error('refetch spawn failed: %s', e)
        return 'spawn_error'
    finally:
        if isinstance(fd, int) and fd != subprocess.DEVNULL:
            try: os.close(fd)
            except Exception: pass
    return 'spawned'
```

In `run_one_tick`, after `streak`/`fired`/`prior_state` are computed (~line 785) and BEFORE the fire block, add (flag-gated):

```python
    from src.execution import intraday_prefetch as _pf
    if _pf.prefetch_enabled() and not force_dry_run:
        settled = _find_settled_regime(history)
        date_str = features['ts_utc'].strftime('%Y-%m-%d')
        if _is_candidate_transition(settled, state_name, streak, confidence, market_open):
            episode = f"{date_str}:{state_name}:{features['ts_utc'].isoformat()}"
            rcli = _redis()
            if _pf.should_prefetch(rcli, date_str, episode=episode):
                _pf.set_prefetch_running(rcli, date_str, target_state=state_name,
                                         episode=episode,
                                         started_at=features['ts_utc'].isoformat())
                _spawn_refetch_prices(date_str)
                _post_to_discord('intraday-regime',
                    f':arrows_counterclockwise: candidate {settled} → {state_name} '
                    f'(tick 1/3, conf={confidence:.2f}) — prefetching prices')
```

(`market_open` is the existing `_is_option_market_open(...)` result in `run_one_tick`; reuse that variable.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_candidate_trigger.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/run_intraday_market_state.py tests/test_intraday_candidate_trigger.py
git commit -m "feat(intraday): tick-1 candidate prefetch trigger + Discord (flag-gated)"
```

---

## Task 7: Tick-3 data-ready gate + cooldown removal + lock release (redeploy)

**Files:**
- Modify: `scripts/redeploy_pipeline.py` (`main`, lines 268-326; add gate before `_spawn_orchestrator`)
- Test: `tests/test_redeploy_gate.py`

Behavior (flag ON): before running steps, poll the prefetch sentinel up to `GATE_TIMEOUT_S = 1200`. `done`+fresh → proceed. `running` → poll. `failed`/timeout/stale → abort (`action=aborted`, Discord, exit 0, run NO steps). No sentinel → run a synchronous `refetch_prices.run(date)` then re-check. Always `release_inflight` in a `finally`. Drop the `redeploy:cooldown` read + set; keep the `redeploy:fired` sentinel.

- [ ] **Step 1: Write the failing test** (`tests/test_redeploy_gate.py`)

```python
import scripts.redeploy_pipeline as rd

class FakeRedis:
    def __init__(self, kv=None): self.kv = kv or {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)
    def ttl(self, k): return 60

def test_gate_proceeds_when_done_and_fresh(monkeypatch):
    import src.execution.intraday_prefetch as p
    r = FakeRedis()
    p.set_prefetch_done(r, '2026-06-09', n_tickers=503)
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    assert rd._data_ready_gate('2026-06-09') == 'proceed'

def test_gate_aborts_on_failed(monkeypatch):
    import src.execution.intraday_prefetch as p
    r = FakeRedis()
    p.set_prefetch_failed(r, '2026-06-09', error='conn loss')
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: False)
    assert rd._data_ready_gate('2026-06-09') == 'abort'

def test_gate_no_sentinel_runs_sync_refetch(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(rd, '_redis', lambda: r)
    called = {'n': 0}
    def fake_sync(date):
        called['n'] += 1
        import src.execution.intraday_prefetch as p
        p.set_prefetch_done(r, date, n_tickers=503); return 0
    monkeypatch.setattr(rd, '_sync_refetch', fake_sync)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    assert rd._data_ready_gate('2026-06-09') == 'proceed'
    assert called['n'] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_redeploy_gate.py -v`
Expected: FAIL (`_data_ready_gate` not defined).

- [ ] **Step 3: Implement the gate**

Add to `scripts/redeploy_pipeline.py` (top-level), gating on `prefetch_enabled()`:

```python
import time as _time
from src.execution import intraday_prefetch as _pf

GATE_TIMEOUT_S = 1200   # 20 min
GATE_POLL_S = 30


def _freshness_ok(date: str) -> bool:
    from scripts.refetch_prices import _freshness_ok as _f
    ok, _ = _f(date)
    return ok


def _sync_refetch(date: str) -> int:
    from scripts.refetch_prices import run as _run
    return _run(date)


def _data_ready_gate(date: str) -> str:
    """Return 'proceed' or 'abort'. Bounded wait on the tick-1 prefetch;
    synchronous refetch if no sentinel; abort on failure/timeout/stale."""
    r = _redis()
    s = _pf.read_prefetch(r, date)
    if s is None:
        _sync_refetch(date)
        s = _pf.read_prefetch(r, date)
    waited = 0
    while True:
        s = _pf.read_prefetch(r, date) or s
        status = (s or {}).get('status')
        if status == 'done' and _freshness_ok(date):
            return 'proceed'
        if status == 'failed':
            return 'abort'
        if waited >= GATE_TIMEOUT_S:
            return 'abort'
        _time.sleep(GATE_POLL_S)
        waited += GATE_POLL_S
```

In `main`, gate ON path: remove the `redeploy:cooldown` read (lines ~275-282) and its `r.set(cooldown_key,...)` (line ~321) **when `prefetch_enabled()`** (keep `redeploy:fired` sentinel logic intact). After the RTH gate passes and before `_spawn_orchestrator`, insert:

```python
    if _pf.prefetch_enabled():
        try:
            verdict = _data_ready_gate(run_date)
            if verdict == 'abort':
                _post_webhook('intraday-regime',
                    f'⛔ redeploy aborted ({reason}) — price ingestion failed/timeout; '
                    f'regime row updated, no signals/orders submitted')
                _print_action({'action': 'aborted', 'reason': 'ingestion_not_ready'})
                return 0
        finally:
            pass   # lock released after the orchestrator finishes, below
```

Wrap the `_spawn_orchestrator` + sentinel-set so the in-flight lock is released in a `finally` (gate ON):

```python
    try:
        rc = _spawn_orchestrator(reason, run_date, dry_run=args.dry_run)
        # set redeploy:fired sentinel (keep); DO NOT set redeploy:cooldown when flag on
    finally:
        if _pf.prefetch_enabled():
            _pf.release_inflight(_redis())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_redeploy_gate.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Verify nothing else regressed + commit**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_15min_prefetch.py tests/test_intraday_prefetch_helper.py tests/test_refetch_prices.py tests/test_intraday_cooldown_inflight.py tests/test_intraday_candidate_trigger.py tests/test_redeploy_gate.py -v`
Expected: ALL PASS.

```bash
git add scripts/redeploy_pipeline.py tests/test_redeploy_gate.py
git commit -m "feat(intraday): tick-3 data-ready gate + drop redeploy cooldown + lock release"
```

---

## Task 8: Integration smoke + regression + spec-coverage review

**Files:**
- Create: `tests/test_intraday_lifecycle_smoke.py`

- [ ] **Step 1: Write a dry-run lifecycle smoke test**

```python
"""End-to-end (mocked) tick1→tick3 lifecycle: candidate fires prefetch,
sentinel goes done, gate proceeds. No live orders, no live network."""
import scripts.redeploy_pipeline as rd
import src.execution.intraday_prefetch as p

class FakeRedis:
    def __init__(self): self.kv = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)
    def ttl(self, k): return 60

def test_lifecycle_prefetch_then_gate_proceeds(monkeypatch):
    r = FakeRedis()
    # tick-1: detector marks running
    p.set_prefetch_running(r, '2026-06-09', target_state='HIGH_VOL',
                           episode='2026-06-09:HIGH_VOL:t0', started_at='t0')
    # prefetch completes
    p.set_prefetch_done(r, '2026-06-09', n_tickers=503)
    # tick-3 gate
    monkeypatch.setattr(rd, '_redis', lambda: r)
    monkeypatch.setattr(rd, '_freshness_ok', lambda date: True)
    assert rd._data_ready_gate('2026-06-09') == 'proceed'
```

- [ ] **Step 2: Run the smoke + full new-suite + intraday regression**

Run: `cd /root/openclaw && python3 -m pytest tests/test_intraday_lifecycle_smoke.py tests/test_intraday_15min_prefetch.py tests/test_intraday_prefetch_helper.py tests/test_refetch_prices.py tests/test_intraday_cooldown_inflight.py tests/test_intraday_candidate_trigger.py tests/test_redeploy_gate.py tests/test_intraday_hmm*.py -v`
Expected: new suites PASS; pre-existing `test_intraday_hmm` failures (3 known per LRN-20260604-002) unchanged — note any NEW failures.

- [ ] **Step 3: Flag-OFF safety check** — confirm legacy paths intact

Run: `cd /root/openclaw && OPENCLAW_INTRADAY_15MIN_PREFETCH= python3 -m pytest tests/test_intraday_15min_prefetch.py -k "5min or tiered or off" -v`
Expected: PASS (flag-OFF = legacy 5-min + tiered).

- [ ] **Step 4: Spec-coverage self-review**

Re-read the spec's "Locked decisions" + "Edge cases". Confirm each maps to a task. Note any gap.

- [ ] **Step 5: Commit**

```bash
git add tests/test_intraday_lifecycle_smoke.py
git commit -m "test(intraday): tick1->tick3 lifecycle smoke + spec-coverage check"
```

---

## Post-implementation (operator-gated — NOT part of the subagent run)

1. Dry-run review: trigger `redeploy_pipeline.py --dry-run --reason TEST --date <today>` with a mocked/real sentinel; confirm gate proceed/abort logging.
2. Live smoke of `refetch_prices.py --date <today>` during RTH: writes fresh today rows + sentinel `done`; confirm `prices.parquet` row count did NOT shrink.
3. Flip `OPENCLAW_INTRADAY_15MIN_PREFETCH=1` in prod `.env` and restart johnbot (`systemctl --user` — operator-approved).
4. Watch the first 15-min ticks + first candidate→confirm lifecycle in `#intraday-regime`.
