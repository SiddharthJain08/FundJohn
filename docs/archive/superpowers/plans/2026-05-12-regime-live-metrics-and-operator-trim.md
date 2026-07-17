# Regime Live Metrics & Operator Trim/Expand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace backfill-based regime validation with live per-strategy×regime PnL metrics plus an operator UI to trim/expand `eligible_regimes` from observed live performance.

**Architecture:** Nightly rollup job aggregates `execution_signals × signal_pnl` by `(strategy_id, regime_state, window)` into a new `strategy_regime_live_pnl_rollup` table. A safe-write manifest editor surfaces those metrics through a new Dashboard tab where operators toggle eligibility per cell; every toggle is audited and applied by overwriting `manifest.json` atomically (the gate already re-reads on every call). Two new doctor checks keep rollup freshness and manifest-vs-HEAD drift visible.

**Tech Stack:** Python 3 (psycopg2, pandas, pytest), Node/Express (johnbot api server), PostgreSQL, vanilla JS dashboard, systemd timers.

---

## Overarching Roadmap (context for this plan)

Position determination is evolving in three steps:

| Phase | Method | Validation |
|---|---|---|
| **0 — pre-regime** (shipped) | `trade_agent_llm` discretion | LLM judgment + manual review |
| **1 — regime-blended scalars** (shipping, this plan supports) | `regime_blended_sizer` static scalars, manifest-driven eligibility | DRY-RUN parity → **operator-driven trim/expand from live metrics** ← this plan |
| **2 — learned sizer** (future) | Scalars/eligibility derived from rolling live-perf distribution | Monte Carlo + drift monitoring against literature priors |

**This plan is the bridge between Phase 1 and Phase 2.** The rollup table it produces is also the training input for the eventual learned sizer — so the schema needs enough fidelity (per-trade granularity not just summary stats) for future ML work, even though Phase 1 only reads aggregate columns. Tasks 1-3 are forward-compatible with Phase 2 by design.

**Out of scope:**
- Literature-prior comparison (Phase 2)
- Drift detection against priors (Phase 2)
- Monte Carlo simulation harness (Phase 2)
- Automatic eligibility flipping (always operator-gated in Phase 1)

---

## File Structure

**New files:**
- `src/database/migrations/074_regime_live_pnl_rollup.sql` — rollup table + indexes
- `src/database/migrations/075_regime_eligibility_audit.sql` — audit table
- `src/metrics/__init__.py` — package marker
- `src/metrics/regime_live_pnl.py` — rollup compute + CLI
- `src/strategies/eligibility_manager.py` — safe manifest writer + audit + CLI
- `systemd/regime-live-pnl-rollup.service`, `systemd/regime-live-pnl-rollup.timer`
- `tests/test_regime_live_pnl.py`
- `tests/test_eligibility_manager.py`
- `tests/test_doctor_regime_live_metrics.py`
- `docs/runbooks/regime-eligibility-operator-runbook.md`

**Modified files:**
- `src/maintenance/doctor.py` — add `check_regime_live_rollup_freshness` and `check_manifest_eligibility_drift`
- `src/channels/api/server.js` — three new endpoints + dashboard tab

**Untouched (intentionally):**
- `src/strategies/regime_gate.py` — already re-reads manifest on every call; no hot-reload needed
- `src/strategies/manifest.json` — schema unchanged; writer just rewrites the file
- `execution_signals` / `signal_pnl` tables — already carry regime_state; no schema change

---

## Phase A — Live PnL Rollup Infrastructure

### Task 1: Migration 074 — `strategy_regime_live_pnl_rollup` table

**Files:**
- Create: `src/database/migrations/074_regime_live_pnl_rollup.sql`
- Test: `tests/test_regime_live_pnl.py` (smoke test that migration applies cleanly)

- [ ] **Step 1: Write migration SQL**

```sql
-- 074_regime_live_pnl_rollup.sql
-- Per-strategy per-regime live PnL aggregates, computed nightly from
-- execution_signals × signal_pnl. Replaces backfill-based regime validation
-- with rolling live evidence. Append-only — new run_at row each night.
-- Phase 1 reads aggregate columns; Phase 2 (learned sizer) reads per-trade
-- detail joined back to signal_pnl by (strategy_id, regime_state, window_days).

CREATE TABLE IF NOT EXISTS strategy_regime_live_pnl_rollup (
    run_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    strategy_id     TEXT         NOT NULL,
    regime_state    TEXT         NOT NULL,   -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    window_days     INTEGER      NOT NULL,   -- 30 | 90 | 0 (all-time)
    trade_count     INTEGER      NOT NULL,
    win_count       INTEGER      NOT NULL,
    total_pnl_pct   NUMERIC      NOT NULL,
    avg_pnl_pct     NUMERIC,
    stdev_pnl_pct   NUMERIC,
    sharpe_proxy    NUMERIC,                 -- avg/stdev * sqrt(252/avg_hold_days); informational
    max_dd_proxy    NUMERIC,                 -- worst single trade pnl_pct (negative)
    avg_hold_days   NUMERIC,
    last_signal_at  TIMESTAMPTZ,             -- newest signal_date in window
    PRIMARY KEY (run_at, strategy_id, regime_state, window_days)
);

CREATE INDEX IF NOT EXISTS idx_srlpr_latest
    ON strategy_regime_live_pnl_rollup (strategy_id, regime_state, window_days, run_at DESC);

CREATE INDEX IF NOT EXISTS idx_srlpr_run_at
    ON strategy_regime_live_pnl_rollup (run_at DESC);
```

- [ ] **Step 2: Apply migration**

Run:
```bash
docker exec -i openclaw-postgres psql -U openclaw -d openclaw \
    < /root/openclaw/src/database/migrations/074_regime_live_pnl_rollup.sql
```
Expected: `CREATE TABLE` + two `CREATE INDEX` lines, no errors.

- [ ] **Step 3: Smoke-verify with empty SELECT**

Run:
```bash
docker exec openclaw-postgres psql -U openclaw -d openclaw \
    -c "SELECT COUNT(*) FROM strategy_regime_live_pnl_rollup;"
```
Expected: `0`.

- [ ] **Step 4: Commit**

```bash
git add src/database/migrations/074_regime_live_pnl_rollup.sql
git commit -m "feat(db): migration 074 - strategy_regime_live_pnl_rollup table"
```

---

### Task 2: Rollup compute module

**Files:**
- Create: `src/metrics/__init__.py` (empty)
- Create: `src/metrics/regime_live_pnl.py`
- Test: `tests/test_regime_live_pnl.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_regime_live_pnl.py
"""Tests for regime_live_pnl rollup computation.

Run: pytest tests/test_regime_live_pnl.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from metrics import regime_live_pnl as rlp  # noqa: E402


def _trade(strategy='s1', regime='LOW_VOL', pnl=1.5, closed_days_ago=5,
           held=3):
    closed = date.today() - timedelta(days=closed_days_ago)
    signal = closed - timedelta(days=held)
    return {
        'strategy_id': strategy,
        'regime_state': regime,
        'signal_date': signal,
        'closed_at': closed,
        'realized_pnl_pct': pnl,
        'days_held': held,
    }


def test_rollup_empty_input_returns_empty_frame():
    df = pd.DataFrame(columns=['strategy_id', 'regime_state', 'signal_date',
                                'closed_at', 'realized_pnl_pct', 'days_held'])
    out = rlp.compute_rollup(df, windows=[30, 90, 0])
    assert out.empty


def test_rollup_groups_by_strategy_regime_window():
    rows = [
        _trade('momentum_a', 'LOW_VOL', pnl=2.0, closed_days_ago=10),
        _trade('momentum_a', 'LOW_VOL', pnl=-1.0, closed_days_ago=15),
        _trade('momentum_a', 'HIGH_VOL', pnl=0.5, closed_days_ago=20),
        _trade('mean_rev', 'LOW_VOL', pnl=3.0, closed_days_ago=5),
    ]
    df = pd.DataFrame(rows)
    out = rlp.compute_rollup(df, windows=[30, 0])
    # 4 groups (3 strategy×regime pairs) × 2 windows = 6 rows expected
    assert len(out) == 6
    a_low_30 = out[(out['strategy_id'] == 'momentum_a')
                   & (out['regime_state'] == 'LOW_VOL')
                   & (out['window_days'] == 30)].iloc[0]
    assert a_low_30['trade_count'] == 2
    assert a_low_30['win_count'] == 1
    assert pytest.approx(a_low_30['total_pnl_pct']) == 1.0
    assert pytest.approx(a_low_30['avg_pnl_pct']) == 0.5


def test_rollup_window_excludes_trades_outside_horizon():
    rows = [
        _trade('s1', 'LOW_VOL', pnl=10.0, closed_days_ago=5),
        _trade('s1', 'LOW_VOL', pnl=-20.0, closed_days_ago=60),  # outside 30d
    ]
    df = pd.DataFrame(rows)
    out = rlp.compute_rollup(df, windows=[30, 90])
    w30 = out[(out['strategy_id'] == 's1') & (out['window_days'] == 30)].iloc[0]
    w90 = out[(out['strategy_id'] == 's1') & (out['window_days'] == 90)].iloc[0]
    assert w30['trade_count'] == 1
    assert w90['trade_count'] == 2


def test_rollup_window_zero_is_all_time():
    rows = [_trade('s1', 'LOW_VOL', pnl=1.0, closed_days_ago=400)]
    df = pd.DataFrame(rows)
    out = rlp.compute_rollup(df, windows=[30, 0])
    assert (out[out['window_days'] == 30]['trade_count'] == 0).all() \
        or out[out['window_days'] == 30].empty
    w0 = out[out['window_days'] == 0]
    assert len(w0) == 1
    assert w0.iloc[0]['trade_count'] == 1


def test_rollup_skips_open_trades_realized_pnl_is_null(monkeypatch):
    rows = [_trade('s1', 'LOW_VOL', pnl=2.0, closed_days_ago=5)]
    df_with_open = pd.DataFrame(rows + [{
        'strategy_id': 's1',
        'regime_state': 'LOW_VOL',
        'signal_date': date.today() - timedelta(days=3),
        'closed_at': None,
        'realized_pnl_pct': None,
        'days_held': None,
    }])
    out = rlp.compute_rollup(df_with_open, windows=[30])
    assert out.iloc[0]['trade_count'] == 1


def test_persist_rollup_writes_rows(monkeypatch):
    df = pd.DataFrame([{
        'strategy_id': 's1', 'regime_state': 'LOW_VOL', 'window_days': 30,
        'trade_count': 3, 'win_count': 2, 'total_pnl_pct': 1.5,
        'avg_pnl_pct': 0.5, 'stdev_pnl_pct': 1.0, 'sharpe_proxy': 0.5,
        'max_dd_proxy': -1.0, 'avg_hold_days': 4.0,
        'last_signal_at': datetime.now(timezone.utc),
    }])
    inserts: list = []

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def executemany(self, sql, rows): inserts.extend(rows)
        def execute(self, *a, **k): pass

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return FakeCursor()
        def commit(self): pass

    monkeypatch.setattr(rlp, '_connect', lambda uri: FakeConn())
    rlp.persist_rollup(df, uri='ignored', run_at=datetime.now(timezone.utc))
    assert len(inserts) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && pytest tests/test_regime_live_pnl.py -v`
Expected: ImportError / ModuleNotFoundError on `metrics.regime_live_pnl`.

- [ ] **Step 3: Implement the module**

```python
# src/metrics/regime_live_pnl.py
"""Nightly rollup of per-strategy×regime live PnL from signal_pnl.

Joins signal_pnl (realized rows only) with execution_signals to attach the
regime_state observed at signal time, then aggregates by
(strategy_id, regime_state, window_days). One row per group per nightly run.

Window 0 = all time. Other windows are inclusive of the last N days by
closed_at. This is the data source for:
  - Dashboard "Regime Eligibility" tab (Phase 1)
  - Future learned-sizer training input (Phase 2)

Run as CLI:
    python -m metrics.regime_live_pnl --windows 30 90 0
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_WINDOWS = (30, 90, 0)
TRADING_DAYS_PER_YEAR = 252


def _db_uri() -> str:
    return (
        os.environ.get('DATABASE_URL')
        or os.environ.get('POSTGRES_URI')
        or 'postgresql://openclaw:password@localhost:5432/openclaw'
    )


def _connect(uri: str):
    import psycopg2
    return psycopg2.connect(uri)


def load_closed_trades(uri: str) -> pd.DataFrame:
    """All closed signal_pnl rows joined with execution_signals.regime_state."""
    sql = """
        SELECT
            es.strategy_id,
            es.regime_state,
            es.signal_date,
            sp.closed_at,
            sp.realized_pnl_pct::float AS realized_pnl_pct,
            COALESCE(sp.days_held, 0) AS days_held
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at IS NOT NULL
           AND es.regime_state IS NOT NULL
    """
    with _connect(uri) as conn:
        return pd.read_sql(sql, conn)


def compute_rollup(df: pd.DataFrame, windows=DEFAULT_WINDOWS,
                    today: date | None = None) -> pd.DataFrame:
    """Aggregate trades by (strategy_id, regime_state, window_days).

    A window of 0 means "all time".
    """
    if df.empty:
        return pd.DataFrame()
    today = today or date.today()
    df = df.copy()
    # ensure date dtype
    df['closed_at'] = pd.to_datetime(df['closed_at']).dt.date

    rows: list[dict] = []
    for window in windows:
        if window == 0:
            sub = df
        else:
            cutoff = today - timedelta(days=window)
            sub = df[df['closed_at'] >= cutoff]
        if sub.empty:
            continue
        grouped = sub.groupby(['strategy_id', 'regime_state'], dropna=False)
        for (strategy_id, regime_state), g in grouped:
            pnls = g['realized_pnl_pct'].astype(float)
            avg = float(pnls.mean())
            std = float(pnls.std(ddof=0)) if len(pnls) > 1 else 0.0
            avg_hold = float(g['days_held'].mean()) if len(g) else 0.0
            sharpe_proxy: float | None
            if std > 0 and avg_hold > 0:
                periods_per_year = TRADING_DAYS_PER_YEAR / max(avg_hold, 1.0)
                sharpe_proxy = (avg / std) * math.sqrt(periods_per_year)
            else:
                sharpe_proxy = None
            last_signal = pd.to_datetime(g['signal_date']).max()
            rows.append({
                'strategy_id':    strategy_id,
                'regime_state':   regime_state,
                'window_days':    window,
                'trade_count':    int(len(g)),
                'win_count':      int((pnls > 0).sum()),
                'total_pnl_pct':  float(pnls.sum()),
                'avg_pnl_pct':    avg,
                'stdev_pnl_pct':  std,
                'sharpe_proxy':   sharpe_proxy,
                'max_dd_proxy':   float(pnls.min()),
                'avg_hold_days':  avg_hold,
                'last_signal_at': last_signal.tz_localize('UTC') if last_signal.tzinfo is None else last_signal,
            })
    return pd.DataFrame(rows)


def persist_rollup(df: pd.DataFrame, uri: str,
                    run_at: datetime | None = None) -> int:
    """Insert rollup rows. Returns number of rows inserted."""
    if df.empty:
        return 0
    run_at = run_at or datetime.now(timezone.utc)
    sql = """
        INSERT INTO strategy_regime_live_pnl_rollup (
            run_at, strategy_id, regime_state, window_days,
            trade_count, win_count, total_pnl_pct, avg_pnl_pct,
            stdev_pnl_pct, sharpe_proxy, max_dd_proxy, avg_hold_days,
            last_signal_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    rows = [(
        run_at,
        r['strategy_id'], r['regime_state'], int(r['window_days']),
        int(r['trade_count']), int(r['win_count']),
        float(r['total_pnl_pct']), float(r['avg_pnl_pct']),
        float(r['stdev_pnl_pct']),
        None if r['sharpe_proxy'] is None or (isinstance(r['sharpe_proxy'], float) and math.isnan(r['sharpe_proxy'])) else float(r['sharpe_proxy']),
        float(r['max_dd_proxy']), float(r['avg_hold_days']),
        r['last_signal_at'],
    ) for _, r in df.iterrows()]
    with _connect(uri) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()
    return len(rows)


def run(uri: str | None = None,
        windows=DEFAULT_WINDOWS,
        today: date | None = None) -> dict:
    uri = uri or _db_uri()
    trades = load_closed_trades(uri)
    rollup = compute_rollup(trades, windows=windows, today=today)
    inserted = persist_rollup(rollup, uri=uri)
    return {
        'closed_trades_loaded': int(len(trades)),
        'rollup_rows':          int(len(rollup)),
        'inserted':             inserted,
        'windows':              list(windows),
    }


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--windows', nargs='+', type=int, default=list(DEFAULT_WINDOWS))
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    uri = _db_uri()
    trades = load_closed_trades(uri)
    rollup = compute_rollup(trades, windows=args.windows)
    print(rollup.to_string(index=False))
    if args.dry_run:
        print(f'\n[dry-run] would insert {len(rollup)} rows')
        return 0
    inserted = persist_rollup(rollup, uri=uri)
    print(f'\n[rollup] inserted {inserted} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

Also create empty `src/metrics/__init__.py`:

```python
"""Live metrics computation (rollups, drift detection)."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/openclaw && pytest tests/test_regime_live_pnl.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/metrics/__init__.py src/metrics/regime_live_pnl.py tests/test_regime_live_pnl.py
git commit -m "feat(metrics): nightly regime live PnL rollup module"
```

---

### Task 3: Systemd timer for nightly rollup

**Files:**
- Create: `systemd/regime-live-pnl-rollup.service`
- Create: `systemd/regime-live-pnl-rollup.timer`

- [ ] **Step 1: Write service unit**

```ini
# systemd/regime-live-pnl-rollup.service
[Unit]
Description=OpenClaw regime live PnL nightly rollup
After=postgresql.service docker.service

[Service]
Type=oneshot
WorkingDirectory=/root/openclaw
ExecStart=/usr/bin/python3 -m metrics.regime_live_pnl
Environment="PYTHONPATH=/root/openclaw/src"
Environment="DATABASE_URL=postgresql://openclaw:password@localhost:5432/openclaw"
User=root
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 2: Write timer unit**

```ini
# systemd/regime-live-pnl-rollup.timer
[Unit]
Description=Run regime live PnL rollup nightly at 02:30 ET

[Timer]
# 06:30 UTC ≈ 02:30 ET — after market close + signal_pnl mark-to-close jobs
OnCalendar=*-*-* 06:30:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Install and enable**

Run:
```bash
sudo cp /root/openclaw/systemd/regime-live-pnl-rollup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now regime-live-pnl-rollup.timer
systemctl list-timers regime-live-pnl-rollup.timer
```
Expected: timer listed with `NEXT` set to next 06:30 UTC.

- [ ] **Step 4: Smoke run once manually**

Run:
```bash
sudo systemctl start regime-live-pnl-rollup.service
sudo journalctl -u regime-live-pnl-rollup.service --since "2 minutes ago" --no-pager
```
Expected: log line `[rollup] inserted N rows` with N > 0 (since we have 4503 closed trades).

- [ ] **Step 5: Verify rows landed**

Run:
```bash
docker exec openclaw-postgres psql -U openclaw -d openclaw \
    -c "SELECT regime_state, window_days, COUNT(*) FROM strategy_regime_live_pnl_rollup GROUP BY 1,2 ORDER BY 1,2;"
```
Expected: rows for each populated (regime_state × window) combo.

- [ ] **Step 6: Commit**

```bash
git add systemd/regime-live-pnl-rollup.service systemd/regime-live-pnl-rollup.timer
git commit -m "feat(systemd): nightly timer for regime live PnL rollup"
```

---

### Task 4: Doctor check — rollup freshness

**Files:**
- Modify: `src/maintenance/doctor.py` (add new check function + register)
- Test: `tests/test_doctor_regime_live_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor_regime_live_metrics.py
"""Tests for doctor.check_regime_live_rollup_freshness.

Severity model:
  fresh (<= ROLLUP_STALE_HOURS old)              → PASS
  stale (> ROLLUP_STALE_HOURS, <= 72h)          → WARN
  very stale (> 72h) or table empty             → FAIL
  db error                                       → WARN
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor as doc  # noqa: E402


def _stub_latest_run(monkeypatch, run_at):
    monkeypatch.setattr(doc, '_latest_rollup_run_at', lambda uri: run_at)


def test_fresh_rollup_returns_pass(monkeypatch):
    _stub_latest_run(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=2))
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.PASS


def test_stale_rollup_returns_warn(monkeypatch):
    _stub_latest_run(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=30))
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.WARN
    assert 'stale' in r['detail'].lower()


def test_very_stale_rollup_returns_fail(monkeypatch):
    _stub_latest_run(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=80))
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.FAIL


def test_empty_rollup_returns_fail(monkeypatch):
    _stub_latest_run(monkeypatch, None)
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.FAIL
    assert 'empty' in r['detail'].lower() or 'no rollup' in r['detail'].lower()


def test_db_error_returns_warn(monkeypatch):
    def raise_(uri): raise RuntimeError('db down')
    monkeypatch.setattr(doc, '_latest_rollup_run_at', raise_)
    r = doc.check_regime_live_rollup_freshness()
    assert r['severity'] == doc.WARN
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && pytest tests/test_doctor_regime_live_metrics.py -v`
Expected: AttributeError on `check_regime_live_rollup_freshness`.

- [ ] **Step 3: Add the check to doctor.py**

In `src/maintenance/doctor.py`, near the other regime checks, add:

```python
ROLLUP_STALE_HOURS = 26   # nightly job at 06:30 UTC + 20m randomized + ~2h headroom
ROLLUP_VERY_STALE_HOURS = 72


def _latest_rollup_run_at(uri: str):
    import psycopg2
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(run_at) FROM strategy_regime_live_pnl_rollup
            """)
            row = cur.fetchone()
    return row[0] if row else None


def check_regime_live_rollup_freshness():
    """Validate that the nightly regime live PnL rollup ran recently.

    PASS:  rollup within last 26h
    WARN:  26h - 72h (one missed run; operator should investigate)
    FAIL:  > 72h or table empty (rollup pipeline broken)
    WARN:  db error reaching rollup table
    """
    from datetime import datetime, timezone
    uri = _db_uri()
    try:
        latest = _latest_rollup_run_at(uri)
    except Exception as e:
        return {
            'name':     'regime_live_rollup_freshness',
            'severity': WARN,
            'detail':   f'rollup query failed: {e!s}',
        }
    if latest is None:
        return {
            'name':     'regime_live_rollup_freshness',
            'severity': FAIL,
            'detail':   'rollup table empty — has the timer ever run?',
        }
    age = datetime.now(timezone.utc) - latest
    age_hours = age.total_seconds() / 3600.0
    if age_hours <= ROLLUP_STALE_HOURS:
        sev = PASS
        msg = f'fresh ({age_hours:.1f}h old)'
    elif age_hours <= ROLLUP_VERY_STALE_HOURS:
        sev = WARN
        msg = f'stale ({age_hours:.1f}h old; nightly may have failed)'
    else:
        sev = FAIL
        msg = f'very stale ({age_hours:.1f}h old; rollup pipeline broken)'
    return {
        'name':     'regime_live_rollup_freshness',
        'severity': sev,
        'detail':   msg,
    }
```

Then register it in the main checks list (next to `check_regime_blended_gate_b`). Confirm `_db_uri`, `PASS/WARN/FAIL` symbols are already at module scope — if not, reference them via the existing pattern in the file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/openclaw && pytest tests/test_doctor_regime_live_metrics.py -v`
Expected: 5 passed.

- [ ] **Step 5: Smoke-run doctor**

Run:
```bash
cd /root/openclaw && python -m maintenance.doctor 2>&1 | grep -A1 regime_live_rollup_freshness
```
Expected: `PASS  regime_live_rollup_freshness   fresh (<24h old)` after Task 3 timer ran.

- [ ] **Step 6: Commit**

```bash
git add src/maintenance/doctor.py tests/test_doctor_regime_live_metrics.py
git commit -m "feat(doctor): regime_live_rollup_freshness preflight check"
```

---

## Phase B — Manifest Writer & Audit

### Task 5: Migration 075 — eligibility audit table

**Files:**
- Create: `src/database/migrations/075_regime_eligibility_audit.sql`

- [ ] **Step 1: Write migration SQL**

```sql
-- 075_regime_eligibility_audit.sql
-- Append-only history of operator-initiated changes to manifest
-- eligible_regimes. Every dashboard toggle and CLI mutation lands here.
-- This is the audit trail; manifest.json is the live state.

CREATE TABLE IF NOT EXISTS regime_eligibility_changes (
    id              SERIAL       PRIMARY KEY,
    changed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor           TEXT         NOT NULL,    -- 'operator:<name>' | 'cli' | 'dashboard'
    strategy_id     TEXT         NOT NULL,
    before_regimes  TEXT[],
    after_regimes   TEXT[],
    reason          TEXT,
    source          TEXT                       -- e.g. 'live_sharpe_proxy=-0.4 over 90d'
);

CREATE INDEX IF NOT EXISTS idx_regime_eligibility_strategy_time
    ON regime_eligibility_changes (strategy_id, changed_at DESC);
```

- [ ] **Step 2: Apply migration**

Run:
```bash
docker exec -i openclaw-postgres psql -U openclaw -d openclaw \
    < /root/openclaw/src/database/migrations/075_regime_eligibility_audit.sql
```
Expected: `CREATE TABLE` + `CREATE INDEX`.

- [ ] **Step 3: Commit**

```bash
git add src/database/migrations/075_regime_eligibility_audit.sql
git commit -m "feat(db): migration 075 - regime_eligibility_changes audit"
```

---

### Task 6: Eligibility manager module

**Files:**
- Create: `src/strategies/eligibility_manager.py`
- Test: `tests/test_eligibility_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_eligibility_manager.py
"""Tests for eligibility_manager — safe manifest edits with audit.

Run: pytest tests/test_eligibility_manager.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import eligibility_manager as em  # noqa: E402


@pytest.fixture
def manifest_path(tmp_path):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({
        'strategies': {
            'momentum_a': {'eligible_regimes': ['LOW_VOL', 'TRANSITIONING']},
            'mean_rev':   {'eligible_regimes': ['HIGH_VOL']},
            'no_field':   {},
        }
    }, indent=2))
    return p


def test_set_eligibility_updates_manifest(manifest_path, monkeypatch):
    audits: list = []
    monkeypatch.setattr(em, '_insert_audit', lambda **kw: audits.append(kw))
    em.set_eligibility(
        strategy_id='momentum_a',
        new_regimes=['LOW_VOL'],
        actor='operator:test',
        reason='live sharpe regression in TRANSITIONING',
        source='live_30d_sharpe=-0.5',
        manifest_path=manifest_path,
    )
    data = json.loads(manifest_path.read_text())
    assert data['strategies']['momentum_a']['eligible_regimes'] == ['LOW_VOL']
    assert len(audits) == 1
    assert audits[0]['before_regimes'] == ['LOW_VOL', 'TRANSITIONING']
    assert audits[0]['after_regimes'] == ['LOW_VOL']


def test_set_eligibility_rejects_invalid_regime(manifest_path):
    with pytest.raises(ValueError, match='invalid regime'):
        em.set_eligibility(
            strategy_id='momentum_a',
            new_regimes=['LOW_VOL', 'BOGUS'],
            actor='operator:test',
            reason='typo test',
            source='',
            manifest_path=manifest_path,
        )


def test_set_eligibility_rejects_unknown_strategy(manifest_path):
    with pytest.raises(KeyError):
        em.set_eligibility(
            strategy_id='does_not_exist',
            new_regimes=['LOW_VOL'],
            actor='operator:test',
            reason='', source='',
            manifest_path=manifest_path,
        )


def test_set_eligibility_rejects_empty_list(manifest_path):
    with pytest.raises(ValueError, match='at least one'):
        em.set_eligibility(
            strategy_id='momentum_a',
            new_regimes=[],
            actor='operator:test',
            reason='', source='',
            manifest_path=manifest_path,
        )


def test_set_eligibility_writes_atomically(manifest_path, monkeypatch):
    # If audit insert raises, manifest must not be left half-written.
    monkeypatch.setattr(em, '_insert_audit',
                        lambda **kw: (_ for _ in ()).throw(RuntimeError('db down')))
    with pytest.raises(RuntimeError):
        em.set_eligibility(
            strategy_id='momentum_a',
            new_regimes=['LOW_VOL'],
            actor='operator:test',
            reason='', source='',
            manifest_path=manifest_path,
        )
    # Manifest must be unchanged after rollback.
    data = json.loads(manifest_path.read_text())
    assert data['strategies']['momentum_a']['eligible_regimes'] == ['LOW_VOL', 'TRANSITIONING']


def test_list_strategies_returns_current_eligibility(manifest_path):
    out = em.list_strategies(manifest_path=manifest_path)
    by_id = {r['strategy_id']: r for r in out}
    assert by_id['momentum_a']['eligible_regimes'] == ['LOW_VOL', 'TRANSITIONING']
    assert by_id['no_field']['eligible_regimes'] is None  # backward-compat marker


def test_dedupe_and_sort_regimes(manifest_path, monkeypatch):
    monkeypatch.setattr(em, '_insert_audit', lambda **kw: None)
    em.set_eligibility(
        strategy_id='momentum_a',
        new_regimes=['HIGH_VOL', 'LOW_VOL', 'LOW_VOL', 'TRANSITIONING'],
        actor='operator:test', reason='', source='',
        manifest_path=manifest_path,
    )
    data = json.loads(manifest_path.read_text())
    assert data['strategies']['momentum_a']['eligible_regimes'] == \
        ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']  # canonical order, deduped


def test_recent_audit_returns_rows(monkeypatch):
    monkeypatch.setattr(em, '_query_audit', lambda limit: [
        {'changed_at': datetime.now(timezone.utc), 'strategy_id': 'momentum_a',
         'actor': 'cli', 'before_regimes': ['LOW_VOL', 'TRANSITIONING'],
         'after_regimes': ['LOW_VOL'], 'reason': 'test', 'source': ''},
    ])
    out = em.recent_audit(limit=10)
    assert len(out) == 1
    assert out[0]['strategy_id'] == 'momentum_a'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && pytest tests/test_eligibility_manager.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the module**

```python
# src/strategies/eligibility_manager.py
"""Safe writer for manifest.json eligible_regimes + audit trail.

All edits go through set_eligibility(), which:
  1. validates inputs (canonical regimes, non-empty, known strategy)
  2. writes audit row to regime_eligibility_changes first
  3. atomically rewrites manifest.json (tmp + rename)
  4. on any failure, leaves manifest untouched

The gate (`src/strategies/regime_gate.py`) re-reads manifest.json on every
`is_eligible()` call, so changes apply on the next strategy invocation
without a service restart.

CLI:
    python -m strategies.eligibility_manager --list
    python -m strategies.eligibility_manager --set momentum_a LOW_VOL TRANSITIONING \\
        --actor "operator:sid" --reason "trim HIGH_VOL after 90d drawdown" \\
        --source "live_90d_sharpe=-0.6"
    python -m strategies.eligibility_manager --audit --limit 20
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_MANIFEST = Path(__file__).resolve().parent / 'manifest.json'


def _db_uri() -> str:
    return (
        os.environ.get('DATABASE_URL')
        or os.environ.get('POSTGRES_URI')
        or 'postgresql://openclaw:password@localhost:5432/openclaw'
    )


def _connect(uri: str):
    import psycopg2
    return psycopg2.connect(uri)


def _insert_audit(*, actor: str, strategy_id: str,
                   before_regimes, after_regimes,
                   reason: str, source: str) -> None:
    with _connect(_db_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO regime_eligibility_changes
                  (actor, strategy_id, before_regimes, after_regimes, reason, source)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (actor, strategy_id, before_regimes, after_regimes, reason, source))
        conn.commit()


def _query_audit(limit: int) -> list[dict]:
    with _connect(_db_uri()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT changed_at, actor, strategy_id,
                       before_regimes, after_regimes, reason, source
                  FROM regime_eligibility_changes
                 ORDER BY changed_at DESC
                 LIMIT %s
            """, (limit,))
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _canonicalize(regimes: list[str]) -> list[str]:
    """Dedupe and sort to canonical regime order. Validates entries."""
    seen = set()
    for r in regimes:
        if r not in CANONICAL_REGIMES:
            raise ValueError(f'invalid regime {r!r}; must be one of {CANONICAL_REGIMES}')
        seen.add(r)
    return [r for r in CANONICAL_REGIMES if r in seen]


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON via tmp + rename so the file is never half-written."""
    body = json.dumps(data, indent=2) + '\n'
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix='.manifest.',
                                    suffix='.tmp')
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(body)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def set_eligibility(*, strategy_id: str, new_regimes: list[str],
                     actor: str, reason: str, source: str,
                     manifest_path: Path | None = None) -> dict:
    """Update one strategy's eligible_regimes. Audits then writes.

    Raises:
        KeyError: strategy_id not in manifest
        ValueError: invalid or empty regime list
        RuntimeError: audit insert failed (manifest unchanged)
    """
    manifest_path = manifest_path or DEFAULT_MANIFEST
    canonical = _canonicalize(new_regimes)
    if not canonical:
        raise ValueError('eligible_regimes must contain at least one valid regime')

    data = json.loads(manifest_path.read_text())
    strategies = data.setdefault('strategies', {})
    if strategy_id not in strategies:
        raise KeyError(strategy_id)
    record = strategies[strategy_id]
    before = record.get('eligible_regimes')

    # 1) audit row first — if this fails, the manifest must not change.
    _insert_audit(actor=actor, strategy_id=strategy_id,
                  before_regimes=before, after_regimes=canonical,
                  reason=reason, source=source)

    # 2) only mutate after audit landed.
    record['eligible_regimes'] = canonical
    _atomic_write(manifest_path, data)

    return {
        'strategy_id':    strategy_id,
        'before_regimes': before,
        'after_regimes':  canonical,
        'audited_at':     datetime.now(timezone.utc).isoformat(),
    }


def list_strategies(manifest_path: Path | None = None) -> list[dict]:
    manifest_path = manifest_path or DEFAULT_MANIFEST
    data = json.loads(manifest_path.read_text())
    strategies = data.get('strategies', {}) or {}
    out = []
    for sid, record in strategies.items():
        out.append({
            'strategy_id':      sid,
            'eligible_regimes': record.get('eligible_regimes'),  # None = no field
        })
    return out


def recent_audit(limit: int = 20) -> list[dict]:
    return _query_audit(limit)


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    sub = p.add_mutually_exclusive_group(required=True)
    sub.add_argument('--list', action='store_true')
    sub.add_argument('--set', nargs='+', metavar='STRATEGY REGIME [REGIME ...]')
    sub.add_argument('--audit', action='store_true')
    p.add_argument('--actor', default='cli')
    p.add_argument('--reason', default='')
    p.add_argument('--source', default='')
    p.add_argument('--limit', type=int, default=20)
    args = p.parse_args()

    if args.list:
        for row in list_strategies():
            print(f"{row['strategy_id']}: {row['eligible_regimes']}")
        return 0
    if args.audit:
        for row in recent_audit(limit=args.limit):
            print(f"{row['changed_at']} {row['actor']:>16}  "
                  f"{row['strategy_id']}: {row['before_regimes']} -> {row['after_regimes']}  "
                  f"({row.get('reason') or ''})")
        return 0
    if args.set:
        if len(args.set) < 2:
            print('--set requires STRATEGY REGIME [REGIME ...]', file=sys.stderr)
            return 2
        strategy, *regimes = args.set
        result = set_eligibility(
            strategy_id=strategy, new_regimes=regimes,
            actor=args.actor, reason=args.reason, source=args.source,
        )
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/openclaw && pytest tests/test_eligibility_manager.py -v`
Expected: 7 passed.

- [ ] **Step 5: Smoke-test CLI list**

Run:
```bash
cd /root/openclaw && python -m strategies.eligibility_manager --list | head -10
```
Expected: human-readable strategy: regimes mapping.

- [ ] **Step 6: Commit**

```bash
git add src/strategies/eligibility_manager.py tests/test_eligibility_manager.py
git commit -m "feat(strategies): eligibility_manager — safe manifest edits + audit"
```

---

### Task 7: Doctor check — manifest vs git HEAD drift

**Files:**
- Modify: `src/maintenance/doctor.py`
- Test: `tests/test_doctor_regime_live_metrics.py` (extend with 4 new tests)

- [ ] **Step 1: Write the failing test (append to existing test file)**

```python
# Append to tests/test_doctor_regime_live_metrics.py

import subprocess
from unittest.mock import patch


def test_manifest_in_sync_with_head_returns_pass(monkeypatch):
    """git diff exits 0 with empty output → no drift."""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.PASS


def test_manifest_drift_returns_warn(monkeypatch):
    """git diff shows eligible_regimes changes → WARN."""
    diff_out = '''
+        "eligible_regimes": ["TRANSITIONING"],
-        "eligible_regimes": ["LOW_VOL", "TRANSITIONING"],
'''.strip()
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout=diff_out, stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
    assert 'eligible_regimes' in r['detail']


def test_manifest_drift_in_live_returns_fail(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REGIME_BLENDED_LIVE', '1')
    diff_out = '+        "eligible_regimes": ["TRANSITIONING"],'
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout=diff_out, stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.FAIL


def test_manifest_drift_git_unavailable_returns_warn(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError('git not found')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    r = doc.check_manifest_eligibility_drift()
    assert r['severity'] == doc.WARN
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && pytest tests/test_doctor_regime_live_metrics.py::test_manifest_in_sync_with_head_returns_pass -v`
Expected: AttributeError.

- [ ] **Step 3: Add the check to doctor.py**

```python
import os
import subprocess

REPO_ROOT_DEFAULT = Path('/root/openclaw')


def check_manifest_eligibility_drift():
    """Detect uncommitted changes to manifest.json eligible_regimes vs git HEAD.

    Operator-initiated trim/expand mutates manifest.json directly. The gate
    sees changes immediately, but if the change is never committed,
    redeploys / new clones will silently revert. This check makes that
    visible every cycle until resolved.

    PASS:  no diff or diff contains no eligible_regimes lines
    WARN:  eligible_regimes lines differ from HEAD (DRY-RUN mode)
    FAIL:  eligible_regimes lines differ AND LIVE flag is set
    WARN:  git unavailable or repo not a git checkout
    """
    repo_root = Path(os.environ.get('OPENCLAW_REPO_ROOT', str(REPO_ROOT_DEFAULT)))
    live = os.environ.get('OPENCLAW_REGIME_BLENDED_LIVE') == '1'
    try:
        proc = subprocess.run(
            ['git', 'diff', '--unified=0', 'HEAD', '--', 'src/strategies/manifest.json'],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {
            'name':     'manifest_eligibility_drift',
            'severity': WARN,
            'detail':   f'git unavailable: {e!s}',
        }
    diff_lines = [ln for ln in proc.stdout.splitlines()
                  if ln.startswith(('+', '-'))
                  and 'eligible_regimes' in ln
                  and not ln.startswith(('+++', '---'))]
    if not diff_lines:
        return {
            'name':     'manifest_eligibility_drift',
            'severity': PASS,
            'detail':   'manifest eligible_regimes match HEAD',
        }
    summary = f'{len(diff_lines)} eligible_regimes line(s) differ from HEAD'
    if live:
        return {
            'name':     'manifest_eligibility_drift',
            'severity': FAIL,
            'detail':   f'{summary} (LIVE mode — commit or revert before next cycle)',
        }
    return {
        'name':     'manifest_eligibility_drift',
        'severity': WARN,
        'detail':   f'{summary} (DRY-RUN — commit when satisfied)',
    }
```

Register in the main check list. Add `check_manifest_eligibility_drift` to the set evaluated by `--required-only` (LIVE-mode FAIL must block preflight).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/openclaw && pytest tests/test_doctor_regime_live_metrics.py -v`
Expected: 9 passed (5 from Task 4 + 4 new).

- [ ] **Step 5: Smoke-run doctor**

Run:
```bash
cd /root/openclaw && python -m maintenance.doctor 2>&1 | grep -A1 manifest_eligibility_drift
```
Expected: result line whose severity matches current working-tree state (the existing uncommitted `eligible_regimes=['TRANSITIONING']` for 5 momentum strategies should now surface as WARN).

- [ ] **Step 6: Commit**

```bash
git add src/maintenance/doctor.py tests/test_doctor_regime_live_metrics.py
git commit -m "feat(doctor): manifest_eligibility_drift check vs git HEAD"
```

---

## Phase C — Dashboard Surface

### Task 8: API endpoints in johnbot api server

**Files:**
- Modify: `src/channels/api/server.js`

- [ ] **Step 1: Add three endpoints**

In `src/channels/api/server.js`, near the existing API route registrations, add:

```javascript
// === Regime Eligibility (Phase 1 operator trim/expand) ===
const { spawn } = require('child_process');

function runPython(args, body) {
  return new Promise((resolve, reject) => {
    const env = {
      ...process.env,
      PYTHONPATH: '/root/openclaw/src',
    };
    const p = spawn('/usr/bin/python3', args, {
      cwd: '/root/openclaw',
      env,
    });
    let out = '', err = '';
    p.stdout.on('data', d => { out += d.toString(); });
    p.stderr.on('data', d => { err += d.toString(); });
    p.on('close', code => {
      if (code === 0) resolve(out);
      else reject(new Error(`python exit ${code}: ${err || out}`));
    });
    if (body) {
      p.stdin.write(body);
      p.stdin.end();
    }
  });
}

// GET /api/regime-eligibility
// Returns { strategies: [{strategy_id, eligible_regimes, metrics: {regime: {window: stats}}}] }
app.get('/api/regime-eligibility', async (req, res) => {
  try {
    // List manifest entries
    const listOut = await runPython([
      '-c',
      'import json; from strategies.eligibility_manager import list_strategies; print(json.dumps(list_strategies()))',
    ]);
    const strategies = JSON.parse(listOut);

    // Fetch latest rollup metrics
    const metricsOut = await runPython([
      '-c',
      `
import json, psycopg2, os
uri = os.environ.get('DATABASE_URL') or 'postgresql://openclaw:password@localhost:5432/openclaw'
sql = """
  SELECT strategy_id, regime_state, window_days,
         trade_count, win_count, total_pnl_pct, avg_pnl_pct,
         stdev_pnl_pct, sharpe_proxy, max_dd_proxy, avg_hold_days
    FROM strategy_regime_live_pnl_rollup
   WHERE run_at = (SELECT MAX(run_at) FROM strategy_regime_live_pnl_rollup)
"""
with psycopg2.connect(uri) as c:
    with c.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
for r in rows:
    for k, v in list(r.items()):
        if hasattr(v, 'isoformat'): r[k] = v.isoformat()
        elif v is not None and hasattr(v, '__float__'):
            try: r[k] = float(v)
            except (TypeError, ValueError): pass
print(json.dumps(rows))
      `.trim(),
    ]);
    const metrics = JSON.parse(metricsOut);

    // Nest metrics under strategy[regime][window]
    const byStrategy = {};
    for (const m of metrics) {
      byStrategy[m.strategy_id] = byStrategy[m.strategy_id] || {};
      byStrategy[m.strategy_id][m.regime_state] = byStrategy[m.strategy_id][m.regime_state] || {};
      byStrategy[m.strategy_id][m.regime_state][m.window_days] = m;
    }
    res.json({
      strategies: strategies.map(s => ({
        ...s,
        metrics: byStrategy[s.strategy_id] || {},
      })),
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /api/regime-eligibility/:strategy
// Body: { regimes: [...], actor: "...", reason: "...", source: "..." }
app.post('/api/regime-eligibility/:strategy', express.json(), async (req, res) => {
  const { strategy } = req.params;
  const { regimes, actor, reason, source } = req.body || {};
  if (!Array.isArray(regimes) || regimes.length === 0) {
    return res.status(400).json({ error: 'regimes must be non-empty array' });
  }
  if (!actor) return res.status(400).json({ error: 'actor required' });
  try {
    const args = [
      '-m', 'strategies.eligibility_manager',
      '--set', strategy, ...regimes,
      '--actor', actor,
      '--reason', reason || '',
      '--source', source || '',
    ];
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    res.status(400).json({ error: e.message });
  }
});

// GET /api/regime-eligibility/audit?limit=50
app.get('/api/regime-eligibility/audit', async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit, 10) || 50, 500);
  try {
    const out = await runPython([
      '-c',
      `import json; from strategies.eligibility_manager import recent_audit; rows = recent_audit(limit=${limit}); [r.update({k: v.isoformat() for k,v in r.items() if hasattr(v, "isoformat")}) for r in rows]; print(json.dumps(rows, default=str))`,
    ]);
    res.json(JSON.parse(out));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
```

- [ ] **Step 2: Restart johnbot service**

Run:
```bash
sudo systemctl restart johnbot.service && sleep 3 && sudo journalctl -u johnbot.service --since "10s ago" --no-pager | tail -20
```
Expected: clean startup, no errors mentioning regime-eligibility.

- [ ] **Step 3: Smoke-test the GET endpoint**

Run:
```bash
curl -s http://localhost:3000/api/regime-eligibility | python3 -m json.tool | head -40
```
Expected: JSON with `strategies` array containing strategy_id, eligible_regimes, and metrics nested by regime/window.

- [ ] **Step 4: Smoke-test the audit endpoint**

Run:
```bash
curl -s http://localhost:3000/api/regime-eligibility/audit?limit=5 | python3 -m json.tool
```
Expected: `[]` (no edits made yet) or audit rows if any were made via CLI.

- [ ] **Step 5: Commit**

```bash
git add src/channels/api/server.js
git commit -m "feat(api): regime-eligibility endpoints (list/set/audit)"
```

---

### Task 9: Dashboard UI tab

**Files:**
- Modify: `src/channels/api/server.js` (the inline HTML/JS that builds the dashboard)

- [ ] **Step 1: Locate the dashboard tab registration block**

Search server.js for the existing tab definitions (likely a `tabs` object or routes returning HTML). Confirm the pattern used by the Research tab — match it.

Run:
```bash
grep -n "Research\|tabs\|nav-link" /root/openclaw/src/channels/api/server.js | head -20
```

- [ ] **Step 2: Add new tab "Regime Eligibility"**

Following the existing pattern (mirror the Research tab structure), add a new entry. The tab body should render:

```html
<div class="regime-eligibility-tab">
  <header>
    <h2>Regime Eligibility — Live Metrics</h2>
    <p class="subtitle">
      Operator-driven trim/expand of <code>eligible_regimes</code> based on
      live per-strategy×regime PnL. Click a cell to edit. Manifest changes apply
      on the next strategy cycle (gate re-reads manifest every call).
    </p>
    <div id="rollup-freshness"></div>
  </header>

  <table id="regime-eligibility-grid">
    <thead>
      <tr>
        <th>Strategy</th>
        <th>LOW_VOL</th>
        <th>TRANSITIONING</th>
        <th>HIGH_VOL</th>
        <th>CRISIS</th>
      </tr>
    </thead>
    <tbody><!-- rows injected by JS --></tbody>
  </table>

  <section>
    <h3>Recent eligibility changes</h3>
    <table id="regime-eligibility-audit">
      <thead><tr><th>When</th><th>Actor</th><th>Strategy</th><th>Before → After</th><th>Reason</th></tr></thead>
      <tbody></tbody>
    </table>
  </section>
</div>

<style>
  .regime-eligibility-tab { font-family: inherit; }
  #regime-eligibility-grid td { padding: 8px; border: 1px solid #444; cursor: pointer; min-width: 120px; }
  #regime-eligibility-grid td.eligible { background: #1a3a1a; color: #b0e0b0; }
  #regime-eligibility-grid td.ineligible { background: #3a1a1a; color: #e0b0b0; opacity: 0.6; }
  #regime-eligibility-grid td .pnl { display: block; font-size: 11px; opacity: 0.8; }
  #regime-eligibility-grid td.stale-no-trades { background: #222; color: #777; }
  #rollup-freshness { padding: 8px 0; font-size: 12px; color: #888; }
</style>

<script>
(async function() {
  const REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

  function fmtCell(cell, isEligible, m) {
    if (!m || !m.trade_count) {
      cell.className = isEligible ? 'eligible stale-no-trades' : 'ineligible stale-no-trades';
      cell.innerHTML = (isEligible ? '✓' : '✗') + ' <span class="pnl">no trades</span>';
      return;
    }
    cell.className = isEligible ? 'eligible' : 'ineligible';
    const winRate = ((m.win_count / m.trade_count) * 100).toFixed(0);
    const avg = m.avg_pnl_pct != null ? m.avg_pnl_pct.toFixed(2) : '?';
    const sh = m.sharpe_proxy != null ? m.sharpe_proxy.toFixed(2) : '?';
    cell.innerHTML = (isEligible ? '✓' : '✗') +
      ` <span class="pnl">${m.trade_count}t · ${winRate}%w<br>avg ${avg}% · Sh ${sh}</span>`;
  }

  async function load() {
    const r = await fetch('/api/regime-eligibility');
    const data = await r.json();
    const tbody = document.querySelector('#regime-eligibility-grid tbody');
    tbody.innerHTML = '';
    for (const s of data.strategies) {
      const tr = document.createElement('tr');
      const nameTd = document.createElement('td');
      nameTd.textContent = s.strategy_id;
      nameTd.style.cursor = 'default';
      tr.appendChild(nameTd);
      const eligible = new Set(s.eligible_regimes || REGIMES); // null field = all
      for (const regime of REGIMES) {
        const td = document.createElement('td');
        const isEligible = eligible.has(regime);
        // Prefer the 90d window; fall back to all-time
        const m = (s.metrics[regime] && (s.metrics[regime][90] || s.metrics[regime][0])) || null;
        fmtCell(td, isEligible, m);
        td.onclick = () => onToggle(s.strategy_id, regime, eligible);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    const ar = await fetch('/api/regime-eligibility/audit?limit=20');
    const audit = await ar.json();
    const atbody = document.querySelector('#regime-eligibility-audit tbody');
    atbody.innerHTML = '';
    for (const row of audit) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${row.changed_at}</td>
        <td>${row.actor}</td>
        <td>${row.strategy_id}</td>
        <td>${JSON.stringify(row.before_regimes)} → ${JSON.stringify(row.after_regimes)}</td>
        <td>${row.reason || ''}</td>
      `;
      atbody.appendChild(tr);
    }
  }

  async function onToggle(strategy, regime, currentSet) {
    const next = new Set(currentSet);
    if (next.has(regime)) next.delete(regime);
    else next.add(regime);
    if (next.size === 0) {
      alert('Strategy must be eligible in at least one regime.');
      return;
    }
    const reason = prompt(
      `Reason for change to ${strategy}?\n` +
      `Before: ${JSON.stringify([...currentSet])}\n` +
      `After:  ${JSON.stringify([...next])}\n`
    );
    if (reason === null) return;  // cancelled
    const actor = (window.localStorage.getItem('operator_name')
                   || prompt('Operator name (saved for session):') || 'unknown');
    window.localStorage.setItem('operator_name', actor);
    const body = {
      regimes: [...next],
      actor: `operator:${actor}`,
      reason,
      source: 'dashboard',
    };
    const res = await fetch(`/api/regime-eligibility/${encodeURIComponent(strategy)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      alert('Update failed: ' + (e.error || res.statusText));
      return;
    }
    await load();
  }

  load();
})();
</script>
```

- [ ] **Step 3: Restart and visit the page**

Run:
```bash
sudo systemctl restart johnbot.service && sleep 3
curl -sI http://localhost:3000/regime-eligibility | head -5 || true
```

Then open `http://localhost/regime-eligibility` (or appropriate tab URL under nginx) and verify:
- Grid renders with strategies × 4 regimes
- Eligible cells green, ineligible red
- Cells show trade counts and avg PnL
- Audit table below populates after first edit

- [ ] **Step 4: Smoke-test a real edit**

In the UI, click a single cell on a strategy with little exposure, enter reason "smoke test", confirm:
- Cell color toggles
- New row appears in audit table
- `git diff src/strategies/manifest.json` shows the change

Then revert via the UI by clicking the cell again. Verify audit has both rows.

- [ ] **Step 5: Commit**

```bash
git add src/channels/api/server.js
git commit -m "feat(dashboard): regime-eligibility tab with live metrics grid"
```

---

## Phase D — Integration & Runbook

### Task 10: Operator runbook

**Files:**
- Create: `docs/runbooks/regime-eligibility-operator-runbook.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Operator Runbook — Regime Eligibility Trim/Expand

**Purpose:** safely tune `manifest.json` `eligible_regimes` based on live performance, without restarting any service.

## When to act

- Strategy shows consistently negative `sharpe_proxy` in a regime over the 90d window → **trim** that regime.
- Strategy shows positive sharpe + ≥10 trades in a regime it's not currently eligible for → consider **expand**.
- Strategy has zero trades over 90d in a regime → no signal; leave alone.

## How to act

**Via Dashboard (preferred):**
1. Open `http://<host>/regime-eligibility`
2. Click the cell at the (strategy, regime) intersection.
3. Enter a reason (will land in audit table).
4. Verify the audit row appears.

**Via CLI:**
```bash
cd /root/openclaw && python -m strategies.eligibility_manager \
    --set <strategy_id> LOW_VOL TRANSITIONING \
    --actor "operator:<name>" \
    --reason "trim HIGH_VOL after -0.6 Sh over 90d" \
    --source "live_90d"
```

## After acting

1. **Commit the manifest change** so it survives redeploys:
   ```bash
   cd /root/openclaw && git add src/strategies/manifest.json
   git commit -m "config: trim <strategy> eligible_regimes per live metrics"
   ```
   The `manifest_eligibility_drift` doctor check will FAIL in LIVE mode until you commit.

2. **Watch the next cycle.** The gate re-reads the manifest on every call, so changes take effect immediately. Confirm no signals fire from the trimmed regime by:
   ```bash
   docker exec openclaw-postgres psql -U openclaw -d openclaw -c \
     "SELECT strategy_id, regime_state, signal_date FROM execution_signals \
      WHERE strategy_id = '<id>' AND signal_date >= CURRENT_DATE - INTERVAL '1 day' ORDER BY signal_date DESC LIMIT 5;"
   ```

## Audit & rollback

- Full history: dashboard "Recent eligibility changes" or
  ```bash
  python -m strategies.eligibility_manager --audit --limit 50
  ```
- Rollback: set `eligible_regimes` back to the prior list via the same dashboard/CLI flow — every edit (forward or rollback) is audited.

## Cautions

- **Empty eligibility is rejected.** A strategy must be eligible in at least one regime; trim creates dead strategies if applied carelessly.
- **DRY-RUN vs LIVE.** In DRY-RUN, the eligibility filter runs but sizer is still the LLM. In LIVE (`OPENCLAW_REGIME_BLENDED_LIVE=1`), regime_blended_sizer also reads from this manifest. Trim/expand affects both paths.
- **No automatic flipping.** This is operator-gated by design (Phase 1). The eventual learned sizer (Phase 2) will propose changes but still require operator approval.
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/regime-eligibility-operator-runbook.md
git commit -m "docs: regime eligibility operator runbook"
```

---

### Task 11: End-to-end smoke test + spec backreference

**Files:**
- Modify: `docs/superpowers/specs/2026-05-12-regime-backtest-backfill-spec.md` (append Phase G)

- [ ] **Step 1: Backreference this plan in the spec**

Append to the spec file, after Phase F:

```markdown
## Phase G — Live metrics + operator trim/expand (pivot from backfill)

After Phase F we concluded the backfill Gate B is a regression smoke
test, not a validator (most strategies have 0 trades in HIGH_VOL /
CRISIS in the 10y window, so per-regime statistical power is
unavailable). The validation pivot is documented in:

  docs/superpowers/plans/2026-05-12-regime-live-metrics-and-operator-trim.md

Scope:
- Nightly `strategy_regime_live_pnl_rollup` from execution_signals × signal_pnl
- Dashboard tab: per-(strategy, regime) live metrics grid, click to trim/expand
- Audit table (regime_eligibility_changes), safe-write manifest editor
- Two doctor checks: rollup freshness, manifest-vs-HEAD drift

Out of scope (Phase 2 — learned sizer):
- Monte Carlo validation harness
- Literature-prior comparison & drift alerts
- Automatic eligibility flipping
```

- [ ] **Step 2: Full end-to-end smoke test**

Run (sequentially):
```bash
cd /root/openclaw

# 1) Rollup ran and has rows.
docker exec openclaw-postgres psql -U openclaw -d openclaw \
    -c "SELECT regime_state, window_days, COUNT(*) FROM strategy_regime_live_pnl_rollup \
        WHERE run_at = (SELECT MAX(run_at) FROM strategy_regime_live_pnl_rollup) \
        GROUP BY 1,2;"

# 2) Both doctor checks return non-error severity.
python -m maintenance.doctor 2>&1 | grep -E "regime_live_rollup|manifest_eligibility_drift"

# 3) API serves grid data.
curl -sf http://localhost:3000/api/regime-eligibility | python3 -c \
    "import sys, json; d = json.load(sys.stdin); print('strategies:', len(d['strategies']))"

# 4) CLI list works.
python -m strategies.eligibility_manager --list | wc -l

# 5) All new tests pass.
pytest tests/test_regime_live_pnl.py tests/test_eligibility_manager.py \
       tests/test_doctor_regime_live_metrics.py -v
```

Expected: rollup has rows, both doctor checks return PASS or WARN (not FAIL), API returns strategies array, CLI prints non-zero strategy count, all tests pass.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-05-12-regime-backtest-backfill-spec.md
git commit -m "docs(spec): Phase G — pivot from backfill gate to live metrics"
```

---

## Self-review checklist (post-write)

- ✅ Every task names exact file paths.
- ✅ Each step is one action with code/command + expected output.
- ✅ Tests precede implementation (TDD).
- ✅ No placeholders: every code block is complete; no "implement later".
- ✅ Method/symbol names match across tasks (`set_eligibility`, `recent_audit`, `_insert_audit`, `_latest_rollup_run_at`).
- ✅ Schemas match query/insert calls (rollup columns ↔ persist_rollup INSERT ↔ test fixture; audit columns ↔ `_insert_audit` ↔ `_query_audit`).
- ✅ Forward-compatible with Phase 2: rollup table carries per-window aggregates and can be re-derived; raw trades remain in signal_pnl × execution_signals.
- ✅ Honors `NEVER delete from master database` — all new tables append-only, no drops/deletes.
- ✅ Doctor checks have the right LIVE-vs-DRY-RUN truth table (drift WARN in DRY-RUN, FAIL in LIVE).
- ✅ Spec coverage: rollup, trim/expand UI, audit, doctor visibility, runbook, and backreference to spec — all present.
