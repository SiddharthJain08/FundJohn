# Options Surface + CBOE OI + Macro Risk-Free + NYSE Calendar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two-definition options features with one shared module and one history table feeding both live and backtest; wire CBOE open interest; source the risk-free rate from macro.parquet; give every calendar site a real NYSE session master.

**Architecture:** Four independent parts, ordered D (calendar) → C (risk-free) → A (surface) → B (open interest), then rollout. Each part is a pure library module (`src/lib/trading_calendar.py`, `src/backtest/risk_free.py`, `src/strategies/options_surface.py`, `src/strategies/options_oi.py`) plus thin wiring at the existing call sites. New masters are append-only parquets written through `src/data/parquet_store.append_dedup`. Live behaviour changes sit behind flags (`OPENCLAW_OPTIONS_SURFACE`, `OPENCLAW_RF_SOURCE`) so one shadow cycle precedes each flip.

**Tech Stack:** Python 3.13, pandas 3.0.2, numpy 2.4.4, scipy 1.15.3 (`PchipInterpolator`, `norm.ppf`), pyarrow filtered reads, DuckDB-backed `append_dedup`, pytest (`pytest.ini`: `testpaths = tests`, `pythonpath = src`), systemd timers documented under `docs/systemd/`.

**Spec:** `docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md`

## Global Constraints

- Master parquets under `data/master/` are append-only: rows added, never deleted; "removal" is `active=false` (CLAUDE.md core invariant). Every write goes through `append_dedup(path, df, key_cols, mode='replace')`.
- Never load whole `prices.parquet` or `options_eod.parquet`; use pyarrow `filters=` and column projection (VPS is 2-core / 8 GB, no swap).
- Long compute runs as transient systemd units: `Nice=19`, `MemoryMax=3500M`, `PYTHONUNBUFFERED=1`, outside Saturday 12:00–24:00 UTC (research lane) and before Monday 04:00 UTC (weights timer).
- Tests on this box reach the REAL Postgres (`.env` loads at import): stub DB access; fixture tickers must not collide with real symbols unless the test reads a checked-in fixture parquet.
- The working tree carries uncommitted edits from another session in `src/execution/pipeline_orchestrator.py`, `src/execution/resolve_script.js`, `src/strategies/manifest.json`, `src/strategies/strategy_signatures.json`. **Never `git add -A`; stage named files only. Never touch those four files.**
- Env flags (exact names): `OPENCLAW_OPTIONS_SURFACE` (default `0`), `OPENCLAW_RF_SOURCE` (`const` | `macro`, default `const`), `OPENCLAW_TRADING_CALENDAR_PATH` (test override), `OPENCLAW_MACRO_PARQUET` (test override), `OPENCLAW_OPTIONS_SURFACE_PATH` (test override), `OPENCLAW_CBOE_CHAINS_ROOT` (test override).
- Feature version constant: `OPTIONS_FEATURES_VERSION = 2`.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; one commit per task, task id in the subject.
- Imports: modules under `src/` are imported as `from lib.trading_calendar import ...`, `from backtest.risk_free import ...`, `from strategies.options_surface import ...` (pytest `pythonpath = src`; scripts do `sys.path.insert(0, ROOT/'src')`).

---

## File map

| File | Responsibility |
|---|---|
| `src/lib/trading_calendar.py` (new) | NYSE session queries, master-first, alpaca fallback, weekday last resort |
| `src/ingestion/ingest_trading_calendar.py` (new) | builds `data/master/trading_calendar.parquet` from `alpaca calendar` |
| `docs/systemd/openclaw-trading-calendar.{service,timer}` (new) | monthly refresh |
| `src/backtest/_trading_calendar.py`, `src/execution/engine.py`, `src/execution/trade_handoff_builder.py`, `src/execution/option_hedge.py`, `src/execution/alpaca_executor.py`, `src/backtest/options_pricing.py`, `src/ingestion/ingest_cboe_chains.py`, `src/ingestion/ingest_finra_short_interest.py`, `src/ingestion/ingest_nasdaq_earnings_calendar.py`, `src/system_checks/checks/{pipeline,acting_ingest_coverage,storage,fmp_provider_health}.py`, `src/maintenance/doctor.py`, `scripts/run_intraday_market_state.py`, `src/strategies/implementations/S_holiday_seasonality_energy_etf_tv1.py` | calendar call sites |
| `src/backtest/risk_free.py` (new) | DGS3MO loader, `excess_sharpe`, shadow line |
| `src/backtest/unified_backtest.py`, `src/backtest/benchmark_baseline.py`, `src/execution/bench_realized.py`, `src/strategies/auto_backtest.py`, `src/backtest/options_pricing.py`, `src/execution/trade_handoff_builder.py`, `src/execution/benchmark_sizing.py` | rf call sites |
| `src/strategies/options_surface.py` (new) | chain prep, smile fit, constant maturity, per-day features, series features |
| `src/strategies/options_oi.py` (new) | CBOE session lookup + OI features |
| `scripts/build_options_surface.py` (new) | historical builder → `data/master/options_surface.parquet` |
| `scripts/compute_rolling_options_fields.py` | enriched panel from the surface master |
| `scripts/refresh_options_aggregates.py` | daily runner uses the new stage 1 |
| `src/execution/options_aux_v2.py` (new) | live per-ticker assembly behind the flag |
| `src/strategies/aux_data_loader.py` | `FIELDS` gains the new keys |
| `src/data/parquet_store.py`, `src/strategies/sync_data_ledger.py`, `src/system_checks/checks/{master_freshness,options_aux_freshness}.py` | registrations |
| `scripts/rebacktest_options_sleeve.sh` (new), `docs/runbooks/2026-09-04-options-surface-rollout.md` (new) | rollout |

---

# Part D — NYSE session calendar

### Task 1: `trading_calendar` library

**Files:**
- Create: `src/lib/trading_calendar.py`
- Test: `tests/lib/test_trading_calendar.py`

**Interfaces:**
- Produces:
  - `is_session(d) -> bool`
  - `next_session(d) -> datetime.date` (strictly after `d`)
  - `prev_session(d) -> datetime.date` (strictly before `d`)
  - `sessions(start, end) -> list[datetime.date]` (inclusive)
  - `sessions_before(d, n) -> list[datetime.date]` (the `n` sessions strictly before `d`, ascending)
  - `is_open(now_et) -> bool` (regular hours incl. early closes; `now_et` is a tz-aware datetime in America/New_York)
  - `expiry_session(d) -> datetime.date` (`d` if a session else `prev_session(d)`)
  - `clear_cache() -> None`
  - `master_path() -> Path` (honours `OPENCLAW_TRADING_CALENDAR_PATH`)
  - date arguments accept `datetime.date`, `datetime.datetime`, `pandas.Timestamp`, or ISO `str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/lib/test_trading_calendar.py
from __future__ import annotations
import datetime as dt
import logging
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from lib import trading_calendar as tc

ET = ZoneInfo('America/New_York')


def _write_master(tmp_path, monkeypatch, sessions: list[tuple[str, str, str, bool]]):
    """sessions: (date, open, close, active)."""
    df = pd.DataFrame([{'date': dt.date.fromisoformat(d), 'open': o, 'close': c,
                        'session_open': '0400', 'session_close': '2000',
                        'settlement_date': None, 'active': a, 'source': 'alpaca',
                        'fetched_at': '2026-09-04T00:00:00Z'}
                       for d, o, c, a in sessions])
    p = tmp_path / 'trading_calendar.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    return p


@pytest.fixture
def master(tmp_path, monkeypatch):
    # April 2019 (Good Friday 2019-04-19 is the 3rd Friday), Jan 2025 (day of
    # mourning 2025-01-09), and Sept/Nov 2026 (Labor Day 09-07, early close 11-27).
    rows = []
    for d in pd.bdate_range('2019-04-15', '2019-04-26'):
        if d.date() != dt.date(2019, 4, 19):
            rows.append((d.date().isoformat(), '09:30', '16:00', True))
    for d in pd.bdate_range('2025-01-06', '2025-01-10'):
        rows.append((d.date().isoformat(), '09:30', '16:00', d.date() != dt.date(2025, 1, 9)))
    for d in pd.bdate_range('2026-09-01', '2026-09-11'):
        if d.date() != dt.date(2026, 9, 7):
            rows.append((d.date().isoformat(), '09:30', '16:00', True))
    rows.append(('2026-11-27', '09:30', '13:00', True))
    return _write_master(tmp_path, monkeypatch, rows)


def test_is_session_respects_holidays_and_inactive_rows(master):
    assert tc.is_session('2026-09-04') is True
    assert tc.is_session(dt.date(2026, 9, 7)) is False          # Labor Day
    assert tc.is_session(pd.Timestamp('2026-09-05')) is False   # Saturday
    assert tc.is_session('2025-01-09') is False                 # active=false row


def test_next_prev_session_skip_holiday_weekend(master):
    assert tc.next_session('2026-09-04') == dt.date(2026, 9, 8)
    assert tc.prev_session('2026-09-08') == dt.date(2026, 9, 4)
    assert tc.prev_session(dt.datetime(2026, 9, 8, 15, 0)) == dt.date(2026, 9, 4)


def test_sessions_and_sessions_before(master):
    assert tc.sessions('2026-09-03', '2026-09-09') == [
        dt.date(2026, 9, 3), dt.date(2026, 9, 4), dt.date(2026, 9, 8), dt.date(2026, 9, 9)]
    assert tc.sessions_before('2026-09-09', 2) == [dt.date(2026, 9, 4), dt.date(2026, 9, 8)]


def test_expiry_session_moves_holiday_third_friday_to_thursday(master):
    assert tc.expiry_session(dt.date(2019, 4, 19)) == dt.date(2019, 4, 18)
    assert tc.expiry_session(dt.date(2019, 4, 18)) == dt.date(2019, 4, 18)


def test_is_open_uses_master_close_for_early_close(master):
    assert tc.is_open(dt.datetime(2026, 11, 27, 12, 30, tzinfo=ET)) is True
    assert tc.is_open(dt.datetime(2026, 11, 27, 13, 30, tzinfo=ET)) is False
    assert tc.is_open(dt.datetime(2026, 9, 7, 12, 0, tzinfo=ET)) is False
    assert tc.is_open(dt.datetime(2026, 9, 4, 9, 29, tzinfo=ET)) is False
    assert tc.is_open(dt.datetime(2026, 9, 4, 15, 59, tzinfo=ET)) is True


def test_weekday_fallback_when_master_missing_logs_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(tmp_path / 'absent.parquet'))
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: None)
    tc.clear_cache()
    with caplog.at_level(logging.WARNING):
        assert tc.is_session('2026-09-07') is True   # weekday fallback cannot see Labor Day
        assert tc.next_session('2026-09-04') == dt.date(2026, 9, 7)
    assert any('weekday fallback' in r.message for r in caplog.records)


def test_alpaca_fallback_used_before_weekday_math(tmp_path, monkeypatch):
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(tmp_path / 'absent.parquet'))
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: {dt.date(2026, 9, 4), dt.date(2026, 9, 8)})
    tc.clear_cache()
    assert tc.is_session('2026-09-07') is False
    assert tc.next_session('2026-09-04') == dt.date(2026, 9, 8)


def test_out_of_master_range_falls_back(master, monkeypatch):
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: None)
    # 2030 is beyond the fixture: weekday arithmetic with a warning, never an exception.
    assert tc.next_session('2030-01-04') == dt.date(2030, 1, 7)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/lib/test_trading_calendar.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'lib.trading_calendar'`

- [ ] **Step 3: Implement the library**

```python
# src/lib/trading_calendar.py
"""NYSE session calendar — master-first, never silent.

Master: data/master/trading_calendar.parquet (built by
src/ingestion/ingest_trading_calendar.py from `alpaca calendar`, which serves
every session 1970–2029 including exchange-declared closures). Rows carry
`active`; a session the exchange later cancels is kept with active=false
(append-only invariant).

Resolution order for every query:
  1. the master (cached per file mtime);
  2. one `alpaca calendar --start --end` call for the requested range;
  3. weekday arithmetic, with a WARNING (holidays are NOT modelled there).
"""
from __future__ import annotations

import bisect
import datetime as dt
import functools
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
MASTER_PATH_ENV = 'OPENCLAW_TRADING_CALENDAR_PATH'
DEFAULT_MASTER = ROOT / 'data' / 'master' / 'trading_calendar.parquet'
_ALPACA_BIN = os.environ.get('ALPACA_CLI', '/root/go/bin/alpaca')
_FALLBACK_WARNED = False


def master_path() -> Path:
    return Path(os.environ.get(MASTER_PATH_ENV) or DEFAULT_MASTER)


def _as_date(d) -> dt.date:
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    if hasattr(d, 'to_pydatetime'):          # pandas.Timestamp
        return d.to_pydatetime().date()
    return dt.date.fromisoformat(str(d)[:10])


class _Calendar:
    def __init__(self, hours: dict[dt.date, tuple[dt.time, dt.time]]):
        self.hours = hours
        self.dates: list[dt.date] = sorted(hours)
        self.first = self.dates[0] if self.dates else None
        self.last = self.dates[-1] if self.dates else None

    def covers(self, d: dt.date) -> bool:
        return self.first is not None and self.first <= d <= self.last


def _parse_hhmm(s) -> dt.time:
    s = str(s)
    if ':' in s:
        h, m = s.split(':')[:2]
    else:
        h, m = s[:2], s[2:4]
    return dt.time(int(h), int(m))


@functools.lru_cache(maxsize=2)
def _load(path_str: str, mtime_ns: int) -> _Calendar:
    import pyarrow.parquet as pq
    tbl = pq.read_table(path_str, columns=['date', 'open', 'close', 'active'])
    hours: dict[dt.date, tuple[dt.time, dt.time]] = {}
    for row in tbl.to_pylist():
        if row.get('active') is False:
            continue
        d = _as_date(row['date'])
        hours[d] = (_parse_hhmm(row.get('open') or '09:30'), _parse_hhmm(row.get('close') or '16:00'))
    return _Calendar(hours)


def _calendar() -> _Calendar | None:
    p = master_path()
    if not p.exists():
        return None
    try:
        return _load(str(p), p.stat().st_mtime_ns)
    except Exception as exc:  # noqa: BLE001 — a corrupt master must not take the engine down
        log.warning('trading_calendar: master %s unreadable (%s)', p, exc)
        return None


def clear_cache() -> None:
    _load.cache_clear()
    global _FALLBACK_WARNED
    _FALLBACK_WARNED = False


def _alpaca_sessions(start: dt.date, end: dt.date) -> set[dt.date] | None:
    """Sessions in [start, end] from the alpaca CLI, or None when the probe fails."""
    try:
        r = subprocess.run([_ALPACA_BIN, 'calendar', '--start', start.isoformat(),
                            '--end', end.isoformat()],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        rows = json.loads(r.stdout) if r.stdout.strip() else []
        return {dt.date.fromisoformat(x['date']) for x in rows if isinstance(x, dict) and x.get('date')}
    except Exception:  # noqa: BLE001
        return None


def _warn_fallback() -> None:
    global _FALLBACK_WARNED
    if not _FALLBACK_WARNED:
        log.warning('trading_calendar: master missing/out of range at %s and alpaca probe failed — '
                    'weekday fallback (holidays NOT modelled)', master_path())
        _FALLBACK_WARNED = True


def _sessions_any(start: dt.date, end: dt.date) -> list[dt.date]:
    """Sessions in [start, end] by the resolution order."""
    cal = _calendar()
    if cal is not None and cal.covers(start) and cal.covers(end):
        lo = bisect.bisect_left(cal.dates, start)
        hi = bisect.bisect_right(cal.dates, end)
        return cal.dates[lo:hi]
    probe = _alpaca_sessions(start, end)
    if probe is not None:
        return sorted(d for d in probe if start <= d <= end)
    _warn_fallback()
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def is_session(d) -> bool:
    d = _as_date(d)
    return d in _sessions_any(d, d)


def sessions(start, end) -> list[dt.date]:
    return _sessions_any(_as_date(start), _as_date(end))


def next_session(d) -> dt.date:
    d = _as_date(d)
    found = _sessions_any(d + dt.timedelta(days=1), d + dt.timedelta(days=14))
    if found:
        return found[0]
    _warn_fallback()
    n = d + dt.timedelta(days=1)
    while n.weekday() >= 5:
        n += dt.timedelta(days=1)
    return n


def prev_session(d) -> dt.date:
    d = _as_date(d)
    found = _sessions_any(d - dt.timedelta(days=14), d - dt.timedelta(days=1))
    if found:
        return found[-1]
    _warn_fallback()
    p = d - dt.timedelta(days=1)
    while p.weekday() >= 5:
        p -= dt.timedelta(days=1)
    return p


def sessions_before(d, n: int) -> list[dt.date]:
    d = _as_date(d)
    span = max(14, int(n * 1.6) + 7)
    found = _sessions_any(d - dt.timedelta(days=span), d - dt.timedelta(days=1))
    while len(found) < n and span < 5000:
        span *= 2
        found = _sessions_any(d - dt.timedelta(days=span), d - dt.timedelta(days=1))
    return found[-n:] if n > 0 else []


def expiry_session(d) -> dt.date:
    d = _as_date(d)
    return d if is_session(d) else prev_session(d)


def is_open(now_et: dt.datetime) -> bool:
    """Regular trading hours on a session day, honouring the master's early closes."""
    d = now_et.date()
    if not is_session(d):
        return False
    cal = _calendar()
    o, c = (cal.hours.get(d) if cal is not None else None) or (dt.time(9, 30), dt.time(16, 0))
    t = now_et.time().replace(tzinfo=None)
    return o <= t < c
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/lib/test_trading_calendar.py -q 2>&1 | tail -3`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/lib/trading_calendar.py tests/lib/test_trading_calendar.py && git commit -q -m "feat(calendar): trading_calendar library — master-first NYSE sessions with alpaca and weekday fallbacks (task 1)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `ingest_trading_calendar` + monthly timer

**Files:**
- Create: `src/ingestion/ingest_trading_calendar.py`
- Create: `docs/systemd/openclaw-trading-calendar.service`, `docs/systemd/openclaw-trading-calendar.timer`
- Modify: `src/data/parquet_store.py` (add `CALENDAR_PATH`, `CALENDAR_KEYS` next to `MACRO_KEYS`)
- Modify: `src/system_checks/checks/master_freshness.py:38` (`_CADENCES` gains `'trading_calendar.parquet': ('fetched_at', 45)`)
- Modify: `docs/bootstrap.md:104` (enablement list gains `openclaw-trading-calendar`)
- Test: `tests/ingestion/test_ingest_trading_calendar.py`

**Interfaces:**
- Consumes: `append_dedup(path, df, key_cols, mode='replace')` from `src/data/parquet_store.py`.
- Produces: `fetch_year(year, run_cli) -> list[dict]`, `build_rows(years, run_cli) -> pd.DataFrame`, `mark_removed(existing, fetched, years) -> pd.DataFrame`, `main(argv) -> int`. Master columns: `date (date), open (str 'HH:MM'), close (str), session_open (str), session_close (str), settlement_date (date|None), active (bool), source (str), fetched_at (str ISO UTC)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_ingest_trading_calendar.py
from __future__ import annotations
import datetime as dt
import json
import pandas as pd

from ingestion import ingest_trading_calendar as itc


def _fake_cli(payload_by_year):
    def run_cli(start: str, end: str) -> str:
        return json.dumps(payload_by_year[int(start[:4])])
    return run_cli


def test_fetch_year_parses_sessions():
    rows = itc.fetch_year(2026, _fake_cli({2026: [
        {'date': '2026-09-04', 'open': '09:30', 'close': '16:00',
         'session_open': '0400', 'session_close': '2000', 'settlement_date': '2026-09-08'}]}))
    assert rows == [{'date': dt.date(2026, 9, 4), 'open': '09:30', 'close': '16:00',
                     'session_open': '0400', 'session_close': '2000',
                     'settlement_date': dt.date(2026, 9, 8)}]


def test_build_rows_stamps_active_source_fetched_at():
    df = itc.build_rows([2026], _fake_cli({2026: [
        {'date': '2026-09-04', 'open': '09:30', 'close': '16:00'}]}))
    assert list(df.columns) == itc.COLUMNS
    assert bool(df.loc[0, 'active']) is True and df.loc[0, 'source'] == 'alpaca'
    assert df.loc[0, 'fetched_at'].endswith('Z')


def test_mark_removed_flags_sessions_the_exchange_dropped():
    existing = pd.DataFrame({'date': [dt.date(2026, 9, 4), dt.date(2026, 9, 7), dt.date(2027, 1, 4)],
                             'open': ['09:30'] * 3, 'close': ['16:00'] * 3,
                             'session_open': ['0400'] * 3, 'session_close': ['2000'] * 3,
                             'settlement_date': [None] * 3, 'active': [True] * 3,
                             'source': ['alpaca'] * 3, 'fetched_at': ['x'] * 3})
    fetched = itc.build_rows([2026], _fake_cli({2026: [{'date': '2026-09-04', 'open': '09:30', 'close': '16:00'}]}))
    removed = itc.mark_removed(existing, fetched, [2026])
    assert removed['date'].tolist() == [dt.date(2026, 9, 7)]      # 2027 untouched: not a fetched year
    assert removed['active'].tolist() == [False]


def test_main_writes_master_and_is_idempotent(tmp_path, monkeypatch):
    payload = {2026: [{'date': '2026-09-04', 'open': '09:30', 'close': '16:00'},
                      {'date': '2026-09-08', 'open': '09:30', 'close': '16:00'}]}
    monkeypatch.setattr(itc, '_run_cli', _fake_cli(payload))
    out = tmp_path / 'trading_calendar.parquet'
    assert itc.main(['--start-year', '2026', '--end-year', '2026', '--path', str(out)]) == 0
    assert itc.main(['--start-year', '2026', '--end-year', '2026', '--path', str(out)]) == 0
    df = pd.read_parquet(out)
    assert sorted(df['date'].astype(str)) == ['2026-09-04', '2026-09-08']
    assert df['active'].all()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/ingestion/test_ingest_trading_calendar.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'ingestion.ingest_trading_calendar'`

- [ ] **Step 3: Add the store constants**

In `src/data/parquet_store.py`, next to the existing `MACRO_KEYS` definition (grep `MACRO_KEYS =`), add:

```python
CALENDAR_PATH = Path(__file__).resolve().parents[2] / 'data' / 'master' / 'trading_calendar.parquet'
CALENDAR_KEYS = ['date']
```

(If `MACRO_PATH` is defined from a `MASTER_DIR` constant in that file, define `CALENDAR_PATH = MASTER_DIR / 'trading_calendar.parquet'` in the same style instead.)

- [ ] **Step 4: Implement the ingester**

```python
# src/ingestion/ingest_trading_calendar.py
#!/usr/bin/env python3
"""Build / refresh data/master/trading_calendar.parquet from `alpaca calendar`.

`alpaca calendar --start --end` serves every NYSE session from 1970 through
2029 with open/close (early closes carry close=13:00) and exchange-declared
closures already removed (2025-01-09 day of mourning, Good Friday, …). One
call per year keeps each JSON payload small. Sessions the exchange drops after
we stored them are kept with active=false — the master never deletes rows.

Usage:
    python3 src/ingestion/ingest_trading_calendar.py [--start-year 1970] [--end-year <today+3>] [--path …]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.data.parquet_store import CALENDAR_KEYS, CALENDAR_PATH, append_dedup  # noqa: E402

log = logging.getLogger('ingest_trading_calendar')
_ALPACA_BIN = os.environ.get('ALPACA_CLI', '/root/go/bin/alpaca')
COLUMNS = ['date', 'open', 'close', 'session_open', 'session_close', 'settlement_date',
           'active', 'source', 'fetched_at']


def _run_cli(start: str, end: str) -> str:
    r = subprocess.run([_ALPACA_BIN, 'calendar', '--start', start, '--end', end],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f'alpaca calendar rc={r.returncode}: {r.stderr.strip()[:200]}')
    return r.stdout


def _d(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def fetch_year(year: int, run_cli=_run_cli) -> list[dict]:
    raw = run_cli(f'{year}-01-01', f'{year}-12-31')
    rows = json.loads(raw) if raw.strip() else []
    out = []
    for x in rows:
        d = _d(x.get('date'))
        if d is None:
            continue
        out.append({'date': d, 'open': str(x.get('open') or '09:30'), 'close': str(x.get('close') or '16:00'),
                    'session_open': str(x.get('session_open') or '0400'),
                    'session_close': str(x.get('session_close') or '2000'),
                    'settlement_date': _d(x.get('settlement_date'))})
    return out


def build_rows(years: list[int], run_cli=_run_cli) -> pd.DataFrame:
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    rows = []
    for y in years:
        for r in fetch_year(y, run_cli):
            rows.append({**r, 'active': True, 'source': 'alpaca', 'fetched_at': stamp})
    df = pd.DataFrame(rows, columns=COLUMNS)
    return df


def mark_removed(existing: pd.DataFrame, fetched: pd.DataFrame, years: list[int]) -> pd.DataFrame:
    """Rows of `existing` in the fetched years that the exchange no longer lists → active=False."""
    if existing is None or existing.empty:
        return pd.DataFrame(columns=COLUMNS)
    ex = existing.copy()
    ex['date'] = pd.to_datetime(ex['date']).dt.date
    in_years = ex[ex['date'].map(lambda d: d.year in set(years))]
    gone = in_years[~in_years['date'].isin(set(pd.to_datetime(fetched['date']).dt.date))].copy()
    gone['active'] = False
    return gone[COLUMNS]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start-year', type=int, default=1970)
    ap.add_argument('--end-year', type=int, default=dt.date.today().year + 3)
    ap.add_argument('--path', default=str(CALENDAR_PATH))
    a = ap.parse_args(argv)
    years = list(range(a.start_year, a.end_year + 1))
    path = Path(a.path)
    fetched = build_rows(years, _run_cli)
    if fetched.empty:
        log.error('no sessions fetched for %s..%s — master untouched', a.start_year, a.end_year)
        return 1
    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
    removed = mark_removed(existing, fetched, years)
    df = pd.concat([fetched, removed], ignore_index=True)
    df['date'] = pd.to_datetime(df['date']).dt.date
    df['settlement_date'] = df['settlement_date'].map(lambda v: _d(v))
    total = append_dedup(path, df, CALENDAR_KEYS, mode='replace')
    print(f'[trading_calendar] years={years[0]}..{years[-1]} fetched={len(fetched):,} '
          f'deactivated={len(removed)} total_rows={total:,} path={path}', flush=True)
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    sys.exit(main())
```

- [ ] **Step 5: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/ingestion/test_ingest_trading_calendar.py -q 2>&1 | tail -3`
Expected: `4 passed`

- [ ] **Step 6: Add the systemd units, freshness cadence and bootstrap line**

`docs/systemd/openclaw-trading-calendar.service`:
```ini
[Unit]
Description=Refresh NYSE session master (alpaca calendar -> data/master/trading_calendar.parquet)
After=network-online.target
Wants=network-online.target
OnFailure=openclaw-failure-notify@%n.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
Environment=PYTHONPATH=/root/openclaw/src
ExecStart=/usr/bin/python3 /root/openclaw/src/ingestion/ingest_trading_calendar.py
StandardOutput=append:/var/log/openclaw-trading-calendar.log
StandardError=append:/var/log/openclaw-trading-calendar.log
Nice=19
TimeoutStartSec=600
MemoryMax=500M

[Install]
WantedBy=multi-user.target
```

`docs/systemd/openclaw-trading-calendar.timer`:
```ini
[Unit]
Description=Refresh the NYSE session master monthly (1st, 06:00 UTC)

[Timer]
# ~60 small calls (one per year 1970..today+3). Persistent=true so the first
# enable builds the master immediately and a missed month catches up on boot.
OnCalendar=*-*-01 06:00:00 UTC
Persistent=true
Unit=openclaw-trading-calendar.service

[Install]
WantedBy=timers.target
```

`src/system_checks/checks/master_freshness.py` `_CADENCES`: add `'trading_calendar.parquet': ('fetched_at', 45),` after the `'iv_history.parquet'` line.

`docs/bootstrap.md` system-timers list: add `` `openclaw-trading-calendar`, `` after `` `openclaw-tradable-universe-refresh`, ``.

- [ ] **Step 7: Install, enable, and build the master**

```bash
cd /root/openclaw && sudo bash scripts/install_systemd.sh >/dev/null && sudo systemctl enable --now openclaw-trading-calendar.timer && sleep 20 && sudo systemctl start openclaw-trading-calendar.service && tail -2 /var/log/openclaw-trading-calendar.log
```
Expected: `[trading_calendar] years=1970..2029 fetched=15,0xx deactivated=0 total_rows=15,0xx path=/root/openclaw/data/master/trading_calendar.parquet`

Then verify with the library:
```bash
cd /root/openclaw && PYTHONPATH=src python3 -c "from lib import trading_calendar as tc; print(tc.is_session('2026-09-07'), tc.is_session('2026-04-03'), tc.expiry_session(__import__('datetime').date(2019,4,19)), tc.next_session('2026-09-04'))"
```
Expected: `False False 2019-04-18 2026-09-08`

- [ ] **Step 8: Commit**

```bash
cd /root/openclaw && git add src/ingestion/ingest_trading_calendar.py tests/ingestion/test_ingest_trading_calendar.py src/data/parquet_store.py docs/systemd/openclaw-trading-calendar.service docs/systemd/openclaw-trading-calendar.timer src/system_checks/checks/master_freshness.py docs/bootstrap.md && git commit -q -m "feat(calendar): trading_calendar master from alpaca calendar + monthly timer (task 2)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3a: Calendar sites — backtest iterator and option expiry

**Files:**
- Modify: `src/backtest/_trading_calendar.py` (whole file)
- Modify: `src/backtest/options_pricing.py:56-74` (`nearest_monthly_expiry`)
- Test: `tests/backtest/test_trading_calendar_sites.py`, extend `tests/backtest/test_options_pricing.py`

**Interfaces:**
- Consumes: `lib.trading_calendar.sessions`, `expiry_session`.
- Produces: `_trading_calendar.trading_days(start, end)` unchanged signature (yields `datetime.date`); `options_pricing.nearest_monthly_expiry(as_of, dte_target)` unchanged signature, now returns the session on or before the third Friday.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_trading_calendar_sites.py
from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from lib import trading_calendar as tc


@pytest.fixture
def master(tmp_path, monkeypatch):
    rows = []
    for d in pd.bdate_range('2026-03-30', '2026-04-10'):
        if d.date() != dt.date(2026, 4, 3):        # Good Friday
            rows.append({'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True})
    for d in pd.bdate_range('2019-04-15', '2019-04-26'):
        if d.date() != dt.date(2019, 4, 19):        # Good Friday on the 3rd Friday
            rows.append({'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True})
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    yield
    tc.clear_cache()


def test_backtest_trading_days_skips_good_friday(master):
    from backtest._trading_calendar import trading_days
    days = list(trading_days(dt.date(2026, 4, 1), dt.date(2026, 4, 7)))
    assert days == [dt.date(2026, 4, 1), dt.date(2026, 4, 2), dt.date(2026, 4, 6), dt.date(2026, 4, 7)]


def test_nearest_monthly_expiry_moves_to_thursday_when_third_friday_is_closed(master):
    from backtest.options_pricing import nearest_monthly_expiry
    assert nearest_monthly_expiry(dt.date(2019, 3, 20), dte_target=25) == dt.date(2019, 4, 18)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/backtest/test_trading_calendar_sites.py -q 2>&1 | tail -3`
Expected: 2 failures (Good Friday yielded; expiry returns 2019-04-19).

- [ ] **Step 3: Replace the backtest iterator**

```python
# src/backtest/_trading_calendar.py
"""SP-2 Phase A: trading-day iterator for resolver-driven backtests.

2026-09-04: yields NYSE SESSIONS from the trading_calendar master
(lib.trading_calendar), no longer Mon–Fri. Holidays are excluded; when the
master is absent the library falls back (alpaca CLI, then weekday math with a
WARNING) so this iterator never raises.
"""
from __future__ import annotations
from datetime import date
from typing import Iterator

from lib.trading_calendar import sessions


def trading_days(start: date, end: date) -> Iterator[date]:
    yield from sessions(start, end)
```

Note: callers import it as `from src.backtest._trading_calendar import trading_days` (ROOT on `sys.path`); `lib.trading_calendar` resolves because `ROOT/src` is also on `sys.path` in every entry point (`unified_backtest.py`, `engine.py`, pytest `pythonpath = src`). If a caller only has `ROOT` on the path, add `from src.lib.trading_calendar import sessions` as a fallback:

```python
try:
    from lib.trading_calendar import sessions
except ModuleNotFoundError:  # ROOT-only sys.path callers
    from src.lib.trading_calendar import sessions
```

- [ ] **Step 4: Fix the expiry rule**

In `src/backtest/options_pricing.py`, replace the body of `nearest_monthly_expiry`:

```python
def nearest_monthly_expiry(as_of: date, dte_target: int) -> date:
    """Nearest standard monthly expiry at least `dte_target` calendar days after
    as_of. The listed expiry is the third Friday, or the last session before it
    when that Friday is an exchange holiday (Good Friday 2019-04-19 → 04-18)."""
    from lib.trading_calendar import expiry_session

    def third_friday(year: int, month: int) -> date:
        d = date(year, month, 1)
        offset = (4 - d.weekday()) % 7
        first_friday = d + timedelta(days=offset)
        return first_friday + timedelta(days=14)

    earliest = as_of + timedelta(days=int(dte_target))
    y, m = as_of.year, as_of.month
    for _ in range(18):
        tf = expiry_session(third_friday(y, m))
        if tf >= earliest:
            return tf
        m += 1
        if m > 12:
            m = 1; y += 1
    return expiry_session(third_friday(y, m))
```

- [ ] **Step 5: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/backtest/test_trading_calendar_sites.py tests/backtest/test_options_pricing.py tests/backtest/test_bounded_resolver.py -q 2>&1 | tail -3`
Expected: all pass (the existing `test_nearest_monthly_expiry_at_least_dte` stays green: 2024-02-16 is a session).

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw && git add src/backtest/_trading_calendar.py src/backtest/options_pricing.py tests/backtest/test_trading_calendar_sites.py && git commit -q -m "feat(calendar): backtest iterator and monthly expiry use NYSE sessions (task 3a)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3b: Calendar sites — execution path

**Files:**
- Modify: `src/execution/engine.py` (`_next_trading_day` ~:160-203, `_is_trading_session` ~:229-244, `_panel_fresh_required` ~:255-266)
- Modify: `src/execution/trade_handoff_builder.py:319-328` (`_previous_trading_day`)
- Modify: `src/execution/option_hedge.py:11-19` (`_next_trading_day`)
- Modify: `src/execution/alpaca_executor.py:277-281` and `:295-300` (`_static_session` + weekend branch)
- Test: `tests/execution/test_calendar_sites_execution.py`

**Interfaces:**
- Consumes: `lib.trading_calendar.{is_session,next_session,prev_session,is_open}`.
- Produces: unchanged signatures: `engine._next_trading_day(run_date: date) -> date`, `engine._is_trading_session(d: date) -> bool | None`, `trade_handoff_builder._previous_trading_day(run_date: str) -> str`, `option_hedge._next_trading_day(d: date) -> date`, `alpaca_executor._static_session(now_et, pre_open, rth_open, rth_close, post_end) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/execution/test_calendar_sites_execution.py
from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from lib import trading_calendar as tc

ET = ZoneInfo('America/New_York')


@pytest.fixture
def master(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-08-31', '2026-09-11') if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    yield
    tc.clear_cache()


def test_engine_next_trading_day_skips_labor_day_without_cli(master, monkeypatch):
    from execution import engine
    monkeypatch.setattr(engine, '_ALPACA_BIN', '/nonexistent/alpaca', raising=False)
    assert engine._next_trading_day(dt.date(2026, 9, 4)) == dt.date(2026, 9, 8)
    assert engine._is_trading_session(dt.date(2026, 9, 7)) is False
    assert engine._is_trading_session(dt.date(2026, 9, 8)) is True


def test_handoff_previous_trading_day_skips_holiday(master):
    from execution.trade_handoff_builder import _previous_trading_day
    assert _previous_trading_day('2026-09-08') == '2026-09-04'


def test_option_hedge_next_trading_day_skips_holiday(master):
    from execution.option_hedge import _next_trading_day
    assert _next_trading_day(dt.date(2026, 9, 4)) == dt.date(2026, 9, 8)


def test_executor_static_session_closed_on_holiday(master):
    from execution.alpaca_executor import _static_session
    t = dt.time
    now = dt.datetime(2026, 9, 7, 11, 0, tzinfo=ET)     # Labor Day, mid-morning
    assert _static_session(now, t(4, 0), t(9, 30), t(16, 0), t(20, 0)) == 'closed'
    now = dt.datetime(2026, 9, 8, 11, 0, tzinfo=ET)
    assert _static_session(now, t(4, 0), t(9, 30), t(16, 0), t(20, 0)) == 'rth'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/execution/test_calendar_sites_execution.py -q 2>&1 | tail -3`
Expected: 4 failures (weekday math returns 09-07 / 'rth').

- [ ] **Step 3: Rewrite the engine helpers**

Replace `_next_trading_day` in `src/execution/engine.py` (keep the docstring's intent) with:

```python
def _next_trading_day(run_date: date) -> date:
    """Next NYSE session after run_date. Master-first (lib.trading_calendar),
    then the alpaca CLI, then weekday math — the library owns that order and
    logs the fallback."""
    from lib.trading_calendar import next_session
    return next_session(run_date)
```

Replace `_is_trading_session`:

```python
def _is_trading_session(d: date) -> bool | None:
    """True/False from the session master (alpaca CLI as fallback inside the
    library). Returns None only when the library itself raises."""
    try:
        from lib.trading_calendar import is_session
        return bool(is_session(d))
    except Exception:  # noqa: BLE001
        return None
```

In `_panel_fresh_required`, replace `if now_et.weekday() >= 5 or now_et.strftime('%H:%M') < '16:05':` with:

```python
    if now_et.strftime('%H:%M') < '16:05':
        return False
```
(the `trading = _is_trading_session(run_date)` line that follows already covers weekends and holidays.)

- [ ] **Step 4: Rewrite the three smaller sites**

`src/execution/trade_handoff_builder.py`:
```python
def _previous_trading_day(run_date: str) -> str:
    """Previous NYSE session in YYYY-MM-DD form (holiday-aware since 2026-09-04)."""
    from datetime import date as _d
    from lib.trading_calendar import prev_session
    return prev_session(_d.fromisoformat(run_date)).isoformat()
```

`src/execution/option_hedge.py`:
```python
def _next_trading_day(d):
    """Next NYSE session after d (lib.trading_calendar; holiday-aware)."""
    from lib.trading_calendar import next_session
    return next_session(d)
```

`src/execution/alpaca_executor.py` — in the `elif pre_open <= cur_t < rth_open:` branch replace `if now_et.weekday() >= 5:` with `if not _is_session_day(now_et):`, and in `_static_session` replace `if now_et.weekday() >= 5:` with `if not _is_session_day(now_et):`. Add next to `_static_session`:

```python
def _is_session_day(now_et) -> bool:
    """NYSE session day per the trading_calendar master (weekends AND holidays)."""
    from lib.trading_calendar import is_session
    return is_session(now_et.date())
```

- [ ] **Step 5: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/execution/test_calendar_sites_execution.py tests/execution/test_engine_equity_calendar.py tests/execution/test_engine_run_date_arg.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw && git add src/execution/engine.py src/execution/trade_handoff_builder.py src/execution/option_hedge.py src/execution/alpaca_executor.py tests/execution/test_calendar_sites_execution.py && git commit -q -m "feat(calendar): engine, handoff, hedge and executor use NYSE sessions (task 3b)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3c: Calendar sites — ingestion, system checks, intraday state

**Files:**
- Modify: `src/ingestion/ingest_cboe_chains.py:111-128` (`_prev_business_day`, `session_date_for`)
- Modify: `src/ingestion/ingest_finra_short_interest.py:97-104` (`_roll_back_weekend`, `_prev_bday`)
- Modify: `src/ingestion/ingest_nasdaq_earnings_calendar.py:201-219` (`business_days`)
- Modify: `src/system_checks/checks/pipeline.py:36-50`, `src/system_checks/checks/acting_ingest_coverage.py:46-47`, `src/system_checks/checks/storage.py:40-44`, `src/system_checks/checks/fmp_provider_health.py:40`, `src/maintenance/doctor.py:664-665`, `scripts/run_intraday_market_state.py:137-138`
- Test: `tests/ingestion/test_calendar_sites_ingestion.py`

**Interfaces:**
- Consumes: `lib.trading_calendar.{is_session,prev_session,sessions_before,sessions,is_open}`.
- Produces: unchanged signatures at every site.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingestion/test_calendar_sites_ingestion.py
from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from lib import trading_calendar as tc


@pytest.fixture
def master(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-08-24', '2026-09-18') if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    yield
    tc.clear_cache()


def test_cboe_session_date_rolls_holiday_to_prior_session(master):
    from ingestion.ingest_cboe_chains import session_date_for
    assert session_date_for('2026-09-07 17:05:00') == dt.date(2026, 9, 4)   # Labor Day stamp
    assert session_date_for('2026-09-08 08:00:00') == dt.date(2026, 9, 4)   # pre-open Tuesday
    assert session_date_for('2026-09-08 17:05:00') == dt.date(2026, 9, 8)


def test_finra_prev_bday_skips_holiday(master):
    from ingestion.ingest_finra_short_interest import _prev_bday
    assert _prev_bday(dt.date(2026, 9, 8)) == dt.date(2026, 9, 4)


def test_nasdaq_business_days_are_sessions(master):
    from ingestion.ingest_nasdaq_earnings_calendar import business_days
    days = business_days(dt.date(2026, 9, 4), days_back=2, days_ahead=2)
    assert days == [dt.date(2026, 9, 2), dt.date(2026, 9, 3), dt.date(2026, 9, 4),
                    dt.date(2026, 9, 8), dt.date(2026, 9, 9)]
    assert business_days(dt.date(2026, 9, 7), days_back=1, days_ahead=1) == [dt.date(2026, 9, 4), dt.date(2026, 9, 8)]


def test_pipeline_check_is_trading_day_uses_master(master, monkeypatch):
    from system_checks.checks import pipeline as pc
    monkeypatch.setattr(pc, 'ALPACA_CLI', '/nonexistent/alpaca')
    assert pc._is_trading_day(dt.date(2026, 9, 7)) is False
    assert pc._is_trading_day(dt.date(2026, 9, 8)) is True
```

(Check the exact helper name in `src/system_checks/checks/pipeline.py` — the function whose docstring begins "Asks `alpaca calendar`"; if it is not `_is_trading_day`, use its real name in the test.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/ingestion/test_calendar_sites_ingestion.py -q 2>&1 | tail -3`
Expected: 4 failures.

- [ ] **Step 3: Rewrite the sites**

`src/ingestion/ingest_cboe_chains.py`:
```python
def _prev_business_day(d: dt.date) -> dt.date:
    """Previous NYSE session (holiday-aware since 2026-09-04)."""
    from lib.trading_calendar import prev_session
    return prev_session(d)


def session_date_for(timestamp: str) -> dt.date:
    """Last completed session for a CBOE feed timestamp ('YYYY-MM-DD HH:MM:SS').
    Non-session stamps (weekend, holiday) → the prior session; a session-day
    stamp before 09:30 → the prior session."""
    from lib.trading_calendar import is_session
    ts = dt.datetime.strptime(str(timestamp)[:19], '%Y-%m-%d %H:%M:%S')
    d = ts.date()
    if not is_session(d):
        return _prev_business_day(d)
    if ts.time() < dt.time(9, 30):
        return _prev_business_day(d)
    return d
```
(`ingest_cboe_chains.py` inserts `ROOT` on `sys.path`; add `sys.path.insert(0, str(ROOT / 'src'))` beside it if `lib` fails to import when run as a script.)

`src/ingestion/ingest_finra_short_interest.py`:
```python
def _roll_back_weekend(d: date) -> date:
    """Roll d back to the nearest session on or before d (weekends AND holidays)."""
    from lib.trading_calendar import is_session, prev_session
    return d if is_session(d) else prev_session(d)


def _prev_bday(d: date) -> date:
    from lib.trading_calendar import prev_session
    return prev_session(d)
```

`src/ingestion/ingest_nasdaq_earnings_calendar.py`:
```python
def business_days(today: date, *, days_back: int, days_ahead: int) -> list[date]:
    """`days_back` sessions before `today`, `today` itself if a session, and
    `days_ahead` sessions after (NYSE sessions since 2026-09-04)."""
    from lib.trading_calendar import is_session, sessions_before, next_session
    back = sessions_before(today, days_back)
    ahead: list[date] = []
    d = today
    while len(ahead) < days_ahead:
        d = next_session(d)
        ahead.append(d)
    mid = [today] if is_session(today) else []
    return back + mid + ahead
```

`src/system_checks/checks/pipeline.py` — the trading-day helper: keep the alpaca probe but consult the master FIRST and fall back to it, i.e. replace the whole body with:

```python
    from lib.trading_calendar import is_session
    return bool(is_session(d))
```
(the library already does master → alpaca → weekday, so the check's own subprocess and weekday fallback are removed; leave `ALPACA_CLI` defined for other uses in the file.)

`src/system_checks/checks/acting_ingest_coverage.py`:
```python
def _is_due(now: datetime) -> bool:
    from lib.trading_calendar import is_session
    return is_session(now.date()) and now.hour >= _DUE_HOUR_ET
```

`src/system_checks/checks/storage.py` — replace the "Crude RTH check" block through `in_rth = ...` with:
```python
    from zoneinfo import ZoneInfo
    from lib.trading_calendar import is_open
    now_et = datetime.now(tz=ZoneInfo('America/New_York'))
    in_rth = is_open(now_et)
```

`src/system_checks/checks/fmp_provider_health.py:40`: `weekday = now.weekday() < 5` → 
```python
        from lib.trading_calendar import is_session
        weekday = is_session(now.date())
```

`src/maintenance/doctor.py:664`: replace
```python
    if now_et.weekday() >= 5:
        return _ok('intraday_features_freshness', 'weekend — skipped')
```
with
```python
    from lib.trading_calendar import is_session
    if not is_session(now_et.date()):
        return _ok('intraday_features_freshness', 'non-session day — skipped')
```

`scripts/run_intraday_market_state.py:137`: replace `if et.weekday() >= 5:` with
```python
    from lib.trading_calendar import is_session
    if not is_session(et.date()):
```
and update the docstring sentence "Market holidays are NOT modelled" to "Market holidays come from the trading_calendar master (2026-09-04)."

- [ ] **Step 4: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/ingestion/test_calendar_sites_ingestion.py tests/ingestion/test_cboe_vol_indices.py -q 2>&1 | tail -3 && python3 -m system_checks --check pipeline --json 2>/dev/null | head -c 300`
Expected: tests pass; the system check still runs.

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/ingestion/ingest_cboe_chains.py src/ingestion/ingest_finra_short_interest.py src/ingestion/ingest_nasdaq_earnings_calendar.py src/system_checks/checks/pipeline.py src/system_checks/checks/acting_ingest_coverage.py src/system_checks/checks/storage.py src/system_checks/checks/fmp_provider_health.py src/maintenance/doctor.py scripts/run_intraday_market_state.py tests/ingestion/test_calendar_sites_ingestion.py && git commit -q -m "feat(calendar): ingestion, system checks and intraday state use NYSE sessions (task 3c)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Holiday strategy on the exchange calendar

**Files:**
- Modify: `src/strategies/implementations/S_holiday_seasonality_energy_etf_tv1.py:34,49-70,236-239`
- Test: `tests/strategies/test_holiday_strategy_calendar.py`

**Interfaces:**
- Consumes: `lib.trading_calendar.{sessions,sessions_before,is_session}`.
- Produces: `_holidays_near(anchor, window_days) -> list[pd.Timestamp]` (weekday non-sessions), `_entry_exit_for_holiday(h) -> (pd.Timestamp, pd.Timestamp)` (session arithmetic); `ENTRY_OFFSET` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_holiday_strategy_calendar.py
from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from lib import trading_calendar as tc


@pytest.fixture
def master(tmp_path, monkeypatch):
    closed = {dt.date(2026, 9, 7), dt.date(2026, 11, 26), dt.date(2026, 4, 3)}   # Labor Day, Thanksgiving, Good Friday
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-03-01', '2026-12-31') if d.date() not in closed]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    yield
    tc.clear_cache()


def test_holidays_are_exchange_closures_not_federal(master):
    from strategies.implementations import S_holiday_seasonality_energy_etf_tv1 as s
    hs = {h.date() for h in s._holidays_near(pd.Timestamp('2026-10-15'), window_days=45)}
    assert dt.date(2026, 10, 12) not in hs      # Columbus Day: NYSE open
    assert dt.date(2026, 11, 26) in hs          # Thanksgiving
    hs2 = {h.date() for h in s._holidays_near(pd.Timestamp('2026-04-10'), window_days=20)}
    assert dt.date(2026, 4, 3) in hs2           # Good Friday: NYSE closed, not federal


def test_entry_exit_use_sessions(master):
    from strategies.implementations import S_holiday_seasonality_energy_etf_tv1 as s
    entry, exit_ = s._entry_exit_for_holiday(pd.Timestamp('2026-09-07'))
    assert exit_ == pd.Timestamp('2026-09-04')
    assert entry == pd.Timestamp('2026-08-26')   # prior[-8]: 09-04,09-03,09-02,09-01,08-31,08-28,08-27,08-26
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_holiday_strategy_calendar.py -q 2>&1 | tail -3`
Expected: 2 failures (Columbus Day present; Good Friday absent).

- [ ] **Step 3: Swap the calendar**

In `S_holiday_seasonality_energy_etf_tv1.py`: remove the `USFederalHolidayCalendar` and `CustomBusinessDay` imports and `_BDAY_US`; replace `_holidays_near` and `_entry_exit_for_holiday`:

```python
def _holidays_near(anchor: pd.Timestamp, window_days: int = 45) -> list:
    """Exchange closures (weekdays that are not NYSE sessions) within
    ±window_days of anchor, from the trading_calendar master. Pure calendar
    lookup — independent of which sessions have price data."""
    from lib.trading_calendar import sessions
    start = (anchor - pd.Timedelta(days=window_days)).date()
    end = (anchor + pd.Timedelta(days=window_days)).date()
    open_days = set(sessions(start, end))
    return [pd.Timestamp(d) for d in pd.bdate_range(start, end).date if d not in open_days]


def _entry_exit_for_holiday(h: pd.Timestamp) -> tuple:
    """(entry_day, exit_day): exit_day is the last session before holiday h;
    entry_day is ENTRY_OFFSET sessions before h (so the window spans
    ENTRY_OFFSET sessions inclusive of exit_day)."""
    from lib.trading_calendar import sessions_before
    prior = sessions_before(h.date(), 20)
    if len(prior) < ENTRY_OFFSET:
        return None, None
    return pd.Timestamp(prior[-ENTRY_OFFSET]), pd.Timestamp(prior[-1])
```

In the backtest-summary block (~:236-239) replace the `USFederalHolidayCalendar` use with:
```python
        holidays = _holidays_near(price_index[0] + (price_index[-1] - price_index[0]) / 2,
                                  window_days=int((price_index[-1] - price_index[0]).days / 2) + 1)
```

Update the docstrings that say "US federal holiday" to "NYSE closure".

- [ ] **Step 4: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_holiday_strategy_calendar.py -q 2>&1 | tail -3 && grep -n "USFederalHolidayCalendar\|_BDAY_US" src/strategies/implementations/S_holiday_seasonality_energy_etf_tv1.py | wc -l`
Expected: `2 passed`; grep count `0`.

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/strategies/implementations/S_holiday_seasonality_energy_etf_tv1.py tests/strategies/test_holiday_strategy_calendar.py && git commit -q -m "feat(calendar): holiday strategy uses NYSE closures, not the federal calendar (task 4; re-backtest owed)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```


---

# Part C — Macro risk-free

### Task 5: `risk_free` module

**Files:**
- Create: `src/backtest/risk_free.py`
- Test: `tests/backtest/test_risk_free.py`

**Interfaces:**
- Produces:
  - `RISK_FREE_ANNUAL_CONST = 0.05`, `TRADING_DAYS = 252`, `RF_SERIES = 'DGS3MO'`
  - `rf_source() -> str` (`'const'` | `'macro'`, from `OPENCLAW_RF_SOURCE`, default `'const'`)
  - `rf_annual_asof(d, source=None) -> float`
  - `rf_daily_for(dates, source=None) -> np.ndarray` (annual/252 per date)
  - `excess_sharpe(rets, dates=None, source=None, min_obs=2, asof=None) -> float | None`
  - `sharpe_pair(rets, dates=None, asof=None) -> dict` with keys `const`, `macro`, `rf_mean_annual`, `n`
  - `shadow_line(site, rets, dates=None, asof=None) -> str` formatted `[rf_shadow] site=<site> const=<x> macro=<y> n=<n> rf_mean=<annual>`
  - `macro_path() -> Path` (honours `OPENCLAW_MACRO_PARQUET`), `clear_cache()`

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_risk_free.py
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest

from backtest import risk_free as rf


@pytest.fixture
def macro(tmp_path, monkeypatch):
    dates = pd.bdate_range('2024-01-01', '2024-12-31')
    rows = [{'date': d.date(), 'series': 'DGS3MO', 'value': 5.0 if d.month < 7 else 4.0, 'source': 'fred'} for d in dates]
    rows += [{'date': d.date(), 'series': 'DGS10', 'value': 4.2, 'source': 'fred'} for d in dates[:5]]
    p = tmp_path / 'macro.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_MACRO_PARQUET', str(p))
    rf.clear_cache()
    yield
    rf.clear_cache()


def _old_sharpe(r):
    r = np.asarray(r, float)
    return float((r.mean() - 0.05 / 252) / r.std(ddof=1) * math.sqrt(252))


def test_const_source_reproduces_the_legacy_formula(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    rng = np.random.default_rng(0)
    r = rng.normal(0.0004, 0.01, 300)
    dates = pd.bdate_range('2024-01-02', periods=300)
    assert rf.excess_sharpe(r, dates) == pytest.approx(_old_sharpe(r), rel=1e-12)
    assert rf.excess_sharpe(r) == pytest.approx(_old_sharpe(r), rel=1e-12)


def test_macro_source_uses_dgs3mo_per_date(macro, monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro')
    assert rf.rf_annual_asof('2024-03-15') == pytest.approx(0.05)
    assert rf.rf_annual_asof('2024-09-15') == pytest.approx(0.04)
    assert rf.rf_annual_asof('2024-07-06') == pytest.approx(0.04)      # Saturday → ffill from Friday 07-05
    daily = rf.rf_daily_for(pd.to_datetime(['2024-03-15', '2024-09-16']))
    assert daily == pytest.approx([0.05 / 252, 0.04 / 252])


def test_excess_sharpe_macro_subtracts_time_varying_rf(macro, monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro')
    dates = pd.bdate_range('2024-06-24', periods=10)     # straddles the 5% → 4% step on 07-01
    r = np.full(10, 0.001)
    r[0] = 0.0011                                        # non-zero variance
    rfd = rf.rf_daily_for(dates)
    expect = float((r - rfd).mean() / r.std(ddof=1) * math.sqrt(252))
    assert rf.excess_sharpe(r, dates) == pytest.approx(expect)


def test_before_series_start_backfills_first_value_and_missing_file_warns(macro, monkeypatch, caplog):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro')
    assert rf.rf_annual_asof('2020-01-01') == pytest.approx(0.05)
    monkeypatch.setenv('OPENCLAW_MACRO_PARQUET', '/nonexistent/macro.parquet')
    rf.clear_cache()
    import logging
    with caplog.at_level(logging.WARNING):
        assert rf.rf_annual_asof('2024-03-15') == pytest.approx(0.05)
    assert any('falling back to constant' in r.message for r in caplog.records)


def test_sharpe_pair_and_shadow_line(macro, monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    dates = pd.bdate_range('2024-08-01', periods=40)
    r = np.linspace(-0.01, 0.012, 40)
    pair = rf.sharpe_pair(r, dates)
    assert set(pair) == {'const', 'macro', 'rf_mean_annual', 'n'}
    assert pair['n'] == 40 and pair['rf_mean_annual'] == pytest.approx(0.04)
    line = rf.shadow_line('unit_test', r, dates)
    assert line.startswith('[rf_shadow] site=unit_test const=') and ' macro=' in line and ' n=40 ' in line


def test_degenerate_inputs_return_none():
    assert rf.excess_sharpe([0.01]) is None
    assert rf.excess_sharpe([0.01, 0.01, 0.01]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/backtest/test_risk_free.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'backtest.risk_free'`

- [ ] **Step 3: Implement the module**

```python
# src/backtest/risk_free.py
"""Risk-free rate for every Sharpe and pricing site (spec 2026-09-04 Part C).

Two sources, selected by OPENCLAW_RF_SOURCE:
  const  — 5 % flat (the pre-2026-09-04 behaviour at all six sites)     [default]
  macro  — FRED DGS3MO from data/master/macro.parquet, per date, forward-filled

excess_sharpe(r, dates) = mean(r_t − rf_t) / std(r, ddof=1) · √252 — the
standard deviation of the RAW returns, so 'const' reproduces the legacy
formula (mean(r) − rf)/std(r) bit-for-bit.
"""
from __future__ import annotations

import functools
import logging
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
RISK_FREE_ANNUAL_CONST = 0.05
TRADING_DAYS = 252
RF_SERIES = 'DGS3MO'
MACRO_PATH_ENV = 'OPENCLAW_MACRO_PARQUET'
SOURCE_ENV = 'OPENCLAW_RF_SOURCE'
_WARNED = False


def macro_path() -> Path:
    return Path(os.environ.get(MACRO_PATH_ENV) or (ROOT / 'data' / 'master' / 'macro.parquet'))


def rf_source() -> str:
    s = (os.environ.get(SOURCE_ENV) or 'const').strip().lower()
    return s if s in ('const', 'macro') else 'const'


def clear_cache() -> None:
    _load.cache_clear()
    global _WARNED
    _WARNED = False


@functools.lru_cache(maxsize=2)
def _load(path_str: str, mtime_ns: int) -> pd.Series:
    import pyarrow.parquet as pq
    tbl = pq.read_table(path_str, columns=['date', 'series', 'value'],
                        filters=[('series', '==', RF_SERIES)])
    df = tbl.to_pandas()
    df['date'] = pd.to_datetime(df['date'])
    s = df.dropna(subset=['value']).set_index('date')['value'].sort_index()
    s = s[~s.index.duplicated(keep='last')].astype(float) / 100.0
    return s


def _series() -> pd.Series | None:
    global _WARNED
    p = macro_path()
    try:
        if p.exists():
            s = _load(str(p), p.stat().st_mtime_ns)
            if len(s):
                return s
    except Exception as exc:  # noqa: BLE001
        log.warning('risk_free: %s unreadable (%s)', p, exc)
    if not _WARNED:
        log.warning('risk_free: %s series unavailable at %s — falling back to constant %.2f%%',
                    RF_SERIES, p, RISK_FREE_ANNUAL_CONST * 100)
        _WARNED = True
    return None


def rf_annual_asof(d, source: str | None = None) -> float:
    src = source or rf_source()
    if src == 'const':
        return RISK_FREE_ANNUAL_CONST
    s = _series()
    if s is None:
        return RISK_FREE_ANNUAL_CONST
    ts = pd.Timestamp(d).normalize()
    if ts < s.index[0]:
        return float(s.iloc[0])
    v = s.asof(ts)
    return float(v) if v == v else float(s.iloc[-1])


def rf_daily_for(dates, source: str | None = None) -> np.ndarray:
    src = source or rf_source()
    idx = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize()
    if src == 'const' or _series() is None:
        return np.full(len(idx), RISK_FREE_ANNUAL_CONST / TRADING_DAYS)
    s = _series()
    aligned = s.reindex(s.index.union(idx)).sort_index().ffill().reindex(idx)
    aligned = aligned.fillna(float(s.iloc[0]))
    return aligned.to_numpy(dtype=float) / TRADING_DAYS


def _rf_vector(n: int, dates, source: str | None, asof) -> np.ndarray:
    if dates is not None:
        v = rf_daily_for(dates, source)
        if len(v) != n:
            raise ValueError(f'risk_free: {len(v)} dates for {n} returns')
        return v
    return np.full(n, rf_annual_asof(asof or pd.Timestamp.today(), source) / TRADING_DAYS)


def excess_sharpe(rets, dates=None, source: str | None = None, min_obs: int = 2, asof=None) -> float | None:
    r = np.asarray(list(rets), dtype=float)
    n = len(r)
    if n < max(int(min_obs), 2):
        return None
    sd = float(r.std(ddof=1))
    if not math.isfinite(sd) or sd < 1e-9:
        return None
    rfv = _rf_vector(n, dates, source, asof)
    return float((r - rfv).mean() / sd * math.sqrt(TRADING_DAYS))


def sharpe_pair(rets, dates=None, asof=None) -> dict:
    r = np.asarray(list(rets), dtype=float)
    n = len(r)
    macro_v = _rf_vector(n, dates, 'macro', asof) if n else np.array([])
    return {
        'const': excess_sharpe(r, dates, 'const', asof=asof),
        'macro': excess_sharpe(r, dates, 'macro', asof=asof),
        'rf_mean_annual': float(macro_v.mean() * TRADING_DAYS) if n else None,
        'n': int(n),
    }


def _fmt(v) -> str:
    return 'n/a' if v is None else f'{v:.3f}'


def shadow_line(site: str, rets, dates=None, asof=None) -> str:
    p = sharpe_pair(rets, dates, asof)
    return (f"[rf_shadow] site={site} const={_fmt(p['const'])} macro={_fmt(p['macro'])} "
            f"n={p['n']} rf_mean={_fmt(p['rf_mean_annual'])}")
```

- [ ] **Step 4: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/backtest/test_risk_free.py -q 2>&1 | tail -3`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/backtest/risk_free.py tests/backtest/test_risk_free.py && git commit -q -m "feat(rf): risk_free module — DGS3MO from macro.parquet behind OPENCLAW_RF_SOURCE, const default (task 5)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Risk-free call sites + shadow lines

**Files:**
- Modify: `src/backtest/unified_backtest.py:68` (constant), `:632-642` (Sharpe), `:1416-1440` (config_json)
- Modify: `src/backtest/benchmark_baseline.py:60-63`, `:118-128` (`_excess_sharpe`), `:194-206` (call with dates)
- Modify: `src/execution/bench_realized.py:27`, `:99-105`
- Modify: `src/strategies/auto_backtest.py:71`, `:439-440`
- Modify: `src/backtest/options_pricing.py:14-23` (`bs_price` etc. gain `as_of=None`)
- Modify: `src/execution/trade_handoff_builder.py:39`
- Modify: `src/execution/benchmark_sizing.py:268-283` (cache key)
- Modify: `tests/backtest/test_benchmark_baseline.py:225-230`
- Test: `tests/backtest/test_rf_sites.py`

**Interfaces:**
- Consumes: `backtest.risk_free.{excess_sharpe, sharpe_pair, shadow_line, rf_source, rf_annual_asof, RISK_FREE_ANNUAL_CONST, TRADING_DAYS}`.
- Produces: `benchmark_baseline._excess_sharpe(rets, min_obs, dates=None)`; `bench_realized._sharpe(rets, dates=None)`; `unified_backtest.aggregate_metrics(trades)` returns an extra key `rf_shadow` (dict from `sharpe_pair`) that the run-row write copies into `config_json['rf']`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_rf_sites.py
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest


def test_benchmark_baseline_sharpe_accepts_dates_and_matches_const(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from backtest import benchmark_baseline as bb
    r = [0.001, -0.002, 0.003, 0.0005, 0.002] * 10
    dates = [d.strftime('%Y-%m-%d') for d in pd.bdate_range('2025-01-02', periods=50)]
    legacy = (np.mean(r) - 0.05 / 252) / np.std(r, ddof=1) * math.sqrt(252)
    assert bb._excess_sharpe(r, 40, dates=dates) == pytest.approx(legacy, rel=1e-9)
    assert bb.RISK_FREE_ANNUAL == 0.05


def test_aggregate_metrics_emits_rf_shadow(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from backtest.unified_backtest import aggregate_metrics
    trades = []
    for i in range(30):
        d = pd.Timestamp('2025-03-03') + pd.tseries.offsets.BDay(i)
        trades.append({'ticker': 'AAA', 'pnl_pct': 0.01 * (1 if i % 3 else -1), 'holding_days': 1,
                       'daily_marks': [(d.strftime('%Y-%m-%d'), 0.01 * (1 if i % 3 else -1))]})
    m = aggregate_metrics(trades)
    assert m['sharpe'] is not None
    assert set(m['rf_shadow']) == {'const', 'macro', 'rf_mean_annual', 'n'}
    assert m['rf_shadow']['const'] == pytest.approx(m['sharpe'])


def test_bench_realized_sharpe_signature(monkeypatch):
    monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'const')
    from execution import bench_realized as br
    r = [0.001, -0.001, 0.002, 0.0, 0.0015] * 5
    dates = [d.strftime('%Y-%m-%d') for d in pd.bdate_range('2026-08-01', periods=25)]
    legacy = (np.mean(r) - 0.05 / 252) / np.std(r, ddof=1) * math.sqrt(252)
    assert br._sharpe(r, dates) == pytest.approx(legacy, rel=1e-9)
    assert br._sharpe(r) == pytest.approx(legacy, rel=1e-9)


def test_options_pricing_rate_asof(monkeypatch, tmp_path):
    from backtest import options_pricing as op, risk_free as rf
    rows = [{'date': d.date(), 'series': 'DGS3MO', 'value': 3.0, 'source': 'fred'} for d in pd.bdate_range('2026-01-01', '2026-12-31')]
    p = tmp_path / 'macro.parquet'; pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_MACRO_PARQUET', str(p)); monkeypatch.setenv('OPENCLAW_RF_SOURCE', 'macro'); rf.clear_cache()
    assert op.bs_price('c', 100, 100, 0.5, 0.2) == pytest.approx(op.bs_price('c', 100, 100, 0.5, 0.2, r=0.04))
    assert op.bs_price('c', 100, 100, 0.5, 0.2, as_of='2026-06-01') == pytest.approx(op.bs_price('c', 100, 100, 0.5, 0.2, r=0.03))
    rf.clear_cache()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/backtest/test_rf_sites.py -q 2>&1 | tail -3`
Expected: 4 failures (`_excess_sharpe() got an unexpected keyword 'dates'`, `KeyError: 'rf_shadow'`, `_sharpe() takes 1 positional argument`, `bs_price() got an unexpected keyword 'as_of'`).

- [ ] **Step 3: `unified_backtest.py`**

At `:68` replace `RISK_FREE_DAILY       = 0.05 / TRADING_DAYS_PER_YEAR` with:
```python
from backtest.risk_free import RISK_FREE_ANNUAL_CONST as _RF_CONST, excess_sharpe as _excess_sharpe_rf, sharpe_pair as _sharpe_pair_rf, rf_source as _rf_source
RISK_FREE_DAILY       = _RF_CONST / TRADING_DAYS_PER_YEAR   # legacy constant; Sharpe now goes through risk_free.excess_sharpe
```
(if `src/backtest/unified_backtest.py` imports with `from src.backtest...` style elsewhere, follow that style.)

At the Sharpe block (`std_dr = ...` through `sharpe = float(...)`) replace with:
```python
    std_dr = float(daily_returns.std(ddof=1)) if len(daily_returns) > 1 else 0.0
    if std_dr < 1e-9:
        sharpe = None
        rf_shadow = None
    else:
        sharpe = _excess_sharpe_rf(daily_returns, _dates)
        rf_shadow = _sharpe_pair_rf(daily_returns, _dates)
        _log(f"[rf_shadow] site=aggregate_metrics source={_rf_source()} "
             f"const={rf_shadow['const']:.3f} macro={(rf_shadow['macro'] if rf_shadow['macro'] is not None else float('nan')):.3f} "
             f"n={rf_shadow['n']} rf_mean={(rf_shadow['rf_mean_annual'] or 0):.4f}")
```
and add `'rf_shadow': rf_shadow,` to the returned dict of `aggregate_metrics` (every return path that has `'sharpe'` gets `'rf_shadow': None` where no series exists).

In the run-row `json.dumps({...})` (`:1417`) add after `'methodology': 'discovery',`:
```python
                'rf': {'source': _rf_source(), **(total_metrics.get('rf_shadow') or {})},
```

- [ ] **Step 4: `benchmark_baseline.py`**

Replace `:60-63` with:
```python
# rf goes through backtest.risk_free so every site (engine, S_m, bench_realized,
# auto_backtest) subtracts the same series; the constants stay exported for the
# equality test and for callers that only need the number.
from backtest.risk_free import RISK_FREE_ANNUAL_CONST as RISK_FREE_ANNUAL, excess_sharpe as _rf_excess_sharpe
RISK_FREE_DAILY = RISK_FREE_ANNUAL / TRADING_DAYS_PER_YEAR
```
Replace `_excess_sharpe`:
```python
def _excess_sharpe(rets: list[float], min_obs: int, dates=None) -> float | None:
    """Annualized excess Sharpe over the daily-marks union — the same estimator
    unified_backtest.aggregate_metrics applies to a sleeve's daily-marks
    equity curve. rf per date from backtest.risk_free (const or DGS3MO).
    None when thin (< min_obs) or degenerate (zero variance)."""
    n = len(rets)
    if n < max(min_obs, 2):
        return None
    return _rf_excess_sharpe(rets, dates, min_obs=max(min_obs, 2))
```
In `regime_benchmark_sharpe_by_horizon` replace `xs = [rets[j] ...]` / `out[regime][h] = _excess_sharpe(xs, min_obs)` with:
```python
            js = [j for j in sorted(marked) if rets[j] is not None]
            xs = [rets[j] for j in js]
            out[regime][h] = _excess_sharpe(xs, min_obs, dates=[dates[j] for j in js])
```

- [ ] **Step 5: `bench_realized.py`, `auto_backtest.py`, `trade_handoff_builder.py`, `benchmark_sizing.py`**

`bench_realized.py:27` → `from backtest.risk_free import RISK_FREE_ANNUAL_CONST as _RF_CONST, excess_sharpe as _rf_excess_sharpe` and `RISK_FREE_DAILY = _RF_CONST / 252`. Replace `_sharpe`:
```python
def _sharpe(rets: list[float], dates=None):
    if len(rets) < SHARPE_MIN_OBS:
        return None
    return _rf_excess_sharpe(rets, dates, min_obs=SHARPE_MIN_OBS)
```
and at its call sites pass the matching date list (the NAV-history keys the returns were built from; grep `_sharpe(` in the file — each caller has the sorted date keys in scope; name the variable `common` or `days` as the file does).

`auto_backtest.py:71` → `from backtest.risk_free import RISK_FREE_ANNUAL_CONST as _RF_CONST, excess_sharpe as _rf_excess_sharpe`; `RISK_FREE_DAILY = _RF_CONST / TRADING_DAYS`. At `:439-440` replace the two lines with:
```python
    sharpe  = _rf_excess_sharpe(daily_ret.values, daily_ret.index) or 0.0
```

`trade_handoff_builder.py:39` → `from backtest.risk_free import RISK_FREE_ANNUAL_CONST as _RF_CONST` and `RISK_FREE_DAILY       = _RF_CONST / TRADING_DAYS_PER_YEAR  # unused here; kept for the cross-module equality test`.

`options_pricing.py`: keep `RISK_FREE = 0.04`; add
```python
def _rate(r, as_of):
    if as_of is None:
        return RISK_FREE if r is None else r
    from backtest.risk_free import rf_annual_asof
    return rf_annual_asof(as_of) if r is None else r
```
and change the four signatures to `r: float | None = None, as_of=None`, using `r = _rate(r, as_of)` as the first statement (`bs_price`, `bs_greeks`, `implied_vol`, `strike_for_target_delta`). Behaviour with no `as_of` is unchanged (0.04).

`benchmark_sizing.py` `regime_benchmark_sharpe_for_sizing`: the cache hit condition gains `and cached.get('rf_source') == _rf_source()` and the `_write_cache` payload gains `'rf_source': _rf_source()`, with `from backtest.risk_free import rf_source as _rf_source` at module top.

- [ ] **Step 6: Update the equality test**

`tests/backtest/test_benchmark_baseline.py:225-230` — keep the test; it still passes because all three constants derive from `RISK_FREE_ANNUAL_CONST`. Add one assertion: `from backtest.risk_free import RISK_FREE_ANNUAL_CONST; assert bb.RISK_FREE_ANNUAL == RISK_FREE_ANNUAL_CONST`.

- [ ] **Step 7: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/backtest/test_rf_sites.py tests/backtest/test_risk_free.py tests/backtest/test_benchmark_baseline.py tests/backtest/test_options_pricing.py tests/backtest/test_options_backtest.py tests/execution/test_bench_realized.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
cd /root/openclaw && git add src/backtest/unified_backtest.py src/backtest/benchmark_baseline.py src/execution/bench_realized.py src/strategies/auto_backtest.py src/backtest/options_pricing.py src/execution/trade_handoff_builder.py src/execution/benchmark_sizing.py tests/backtest/test_rf_sites.py tests/backtest/test_benchmark_baseline.py && git commit -q -m "feat(rf): every Sharpe site goes through risk_free.excess_sharpe with rf_shadow lines; S_m cache keyed on rf_source (task 6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part A — Options surface features

### Task 7: `options_surface` core — chain prep, smile fit, constant maturity, per-day features

**Files:**
- Create: `src/strategies/options_surface.py`
- Test: `tests/strategies/test_options_surface.py`

**Interfaces:**
- Produces:
  - constants `OPTIONS_FEATURES_VERSION = 2`, `FIT_DTE = (7, 120)`, `CHAIN_DTE = (1, 120)`, `FRONT_DTE_MAX = 45`, `ATM_DELTA = (0.40, 0.60)`, `CM_TARGETS = (30, 90)`, `CM_ONE_SIDED_TOL = 10`, `MIN_STRIKES = 5`, `IV_MIN = 0.01`, `DELTA_BAND = (0.05, 0.95)`
  - `prepare_chain(df: pd.DataFrame, as_of) -> pd.DataFrame` (adds `dte`, upper-cases `option_type`, drops zero-greek rows, keeps `1 ≤ dte ≤ 120`)
  - `@dataclass SmileFit(dte:int, t:float, atm_iv:float, iv_25d_put:float|None, iv_25d_call:float|None, n_strikes:int, k_min:float, k_max:float)`
  - `fit_smile(strikes, ivs, spot, dte) -> SmileFit | None`
  - `constant_maturity(fits: dict[int, SmileFit], target_dte: int, attr: str) -> float | None`
  - `features_for_day(chain: pd.DataFrame, spot: float | None, as_of) -> dict` (keys listed in spec A.4; `chain` is a prepared or raw frame for ONE ticker and ONE session)
  - `SCALAR_KEYS: list[str]` — the ordered list of scalar keys persisted to the surface master.

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_options_surface.py
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from strategies import options_surface as osf


def _svi_iv(k, a=0.04, b=0.4, rho=-0.4, m=0.0, sig=0.2, t=30 / 365):
    w = a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + sig ** 2))
    return math.sqrt(w / t)


def _bs_delta(flag, S, K, t, iv):
    d1 = (math.log(S / K) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    return norm.cdf(d1) if flag == 'CALL' else norm.cdf(d1) - 1.0


def _chain(spot=100.0, as_of='2026-09-03', dtes=(10, 30, 60, 90), strikes=None, iv_fn=_svi_iv):
    strikes = strikes if strikes is not None else np.arange(70, 131, 2.5)
    rows = []
    for dte in dtes:
        t = dte / 365
        exp = (pd.Timestamp(as_of) + pd.Timedelta(days=dte)).date()
        for K in strikes:
            k = math.log(K / spot)
            iv = iv_fn(k, t=t)
            for flag in ('CALL', 'PUT'):
                d = _bs_delta(flag, spot, K, t, iv)
                rows.append({'ticker': 'ZZZT', 'date': as_of, 'expiry': exp, 'strike': float(K),
                             'option_type': flag, 'implied_volatility': iv, 'delta': d,
                             'gamma': 0.01, 'theta': -0.02, 'vega': 0.1, 'volume': 10.0, 'close': 1.0,
                             'open_interest': None})
    return pd.DataFrame(rows)


def test_prepare_chain_bands_dte_and_drops_zero_greeks():
    df = _chain(dtes=(0, 5, 30, 150))
    zero = df.iloc[[0]].copy(); zero[['delta', 'gamma', 'theta', 'vega']] = 0.0
    df = pd.concat([df, zero], ignore_index=True)
    out = osf.prepare_chain(df, '2026-09-03')
    assert sorted(out['dte'].unique()) == [5, 30]
    assert not ((out[['delta', 'gamma', 'theta', 'vega']].fillna(0) == 0).all(axis=1)).any()
    assert set(out['option_type'].unique()) <= {'CALL', 'PUT'}


def test_fit_smile_recovers_atm_and_25d_points():
    spot, dte = 100.0, 30
    strikes = np.arange(70, 131, 2.5)
    ivs = np.array([_svi_iv(math.log(K / spot)) for K in strikes])
    fit = osf.fit_smile(strikes, ivs, spot, dte)
    assert fit is not None and fit.n_strikes == len(strikes)
    assert fit.atm_iv == pytest.approx(_svi_iv(0.0), abs=1e-3)
    t = dte / 365
    # 25Δ put: find the strike whose BS put delta is -0.25 on the true smile, compare IVs
    ks = np.linspace(-0.3, 0.3, 6001)
    put_deltas = np.array([_bs_delta('PUT', spot, spot * math.exp(k), t, _svi_iv(k)) for k in ks])
    k_put = ks[np.argmin(np.abs(put_deltas + 0.25))]
    call_deltas = np.array([_bs_delta('CALL', spot, spot * math.exp(k), t, _svi_iv(k)) for k in ks])
    k_call = ks[np.argmin(np.abs(call_deltas - 0.25))]
    assert fit.iv_25d_put == pytest.approx(_svi_iv(k_put), abs=2e-3)
    assert fit.iv_25d_call == pytest.approx(_svi_iv(k_call), abs=2e-3)
    assert fit.iv_25d_put > fit.atm_iv > fit.iv_25d_call     # negative skew


def test_fit_smile_rejects_thin_or_one_sided_grids():
    assert osf.fit_smile(np.array([90, 95, 100, 105.0]), np.array([0.3, 0.28, 0.27, 0.26]), 100.0, 30) is None
    assert osf.fit_smile(np.array([101, 103, 105, 107, 109.0]), np.array([0.26] * 5), 100.0, 30) is None


def test_constant_maturity_interpolates_total_variance_and_one_sided_rule():
    f = lambda dte, iv: osf.SmileFit(dte=dte, t=dte / 365, atm_iv=iv, iv_25d_put=iv + 0.02, iv_25d_call=iv - 0.01, n_strikes=9, k_min=-0.3, k_max=0.3)
    fits = {20: f(20, 0.20), 40: f(40, 0.30)}
    v20, v40 = 0.20 ** 2 * 20 / 365, 0.30 ** 2 * 40 / 365
    vt = v20 + (v40 - v20) * (30 - 20) / (40 - 20)
    assert osf.constant_maturity(fits, 30, 'atm_iv') == pytest.approx(math.sqrt(vt / (30 / 365)))
    assert osf.constant_maturity({30: f(30, 0.25)}, 30, 'atm_iv') == pytest.approx(0.25)
    assert osf.constant_maturity({38: f(38, 0.25)}, 30, 'atm_iv') == pytest.approx(0.25)     # one-sided within 10 d
    assert osf.constant_maturity({45: f(45, 0.25)}, 30, 'atm_iv') is None                     # too far
    assert osf.constant_maturity({}, 30, 'atm_iv') is None


def test_features_for_day_keys_and_values():
    chain = _chain()
    row = osf.features_for_day(chain, 100.0, '2026-09-03')
    for k in ['iv30', 'iv90', 'near_iv', 'far_iv', 'ts_ratio', 'iv_25d_put_30d', 'iv_25d_call_30d',
              'skew_25d_30d', 'rr_25d_30d', 'skew_20d', 'iv_spread', 'term_slope', 'gamma_atm',
              'theta_atm', 'call_volume', 'put_volume', 'volume', 'pc_ratio', 'spot', 'last_price',
              'expiry_date', 'n_expiries_fit', 'n_strikes_30d', 'options_features_version']:
        assert k in row, k
    assert row['options_features_version'] == 2
    assert row['iv30'] == pytest.approx(_svi_iv(0.0), abs=2e-3)
    assert row['near_iv'] == row['iv30'] and row['far_iv'] == row['iv90']
    assert row['ts_ratio'] == pytest.approx(row['iv30'] / row['iv90'])
    assert row['skew_20d'] == row['skew_25d_30d'] == pytest.approx(row['iv_25d_put_30d'] - row['iv30'])
    assert row['pc_ratio'] == pytest.approx(1.0) and row['volume'] == pytest.approx(chain['volume'].sum())
    assert row['expiry_date'] == '2026-09-13'        # front usable expiry (10 d)
    assert row['n_expiries_fit'] == 4 and row['spot'] == 100.0 == row['last_price']
    assert row['iv_spread'] == pytest.approx(0.0, abs=1e-9)   # symmetric synthetic chain


def test_features_for_day_without_spot_or_empty_chain():
    empty = osf.features_for_day(_chain().iloc[0:0], 100.0, '2026-09-03')
    assert empty['iv30'] is None and empty['n_expiries_fit'] == 0
    nospot = osf.features_for_day(_chain(), None, '2026-09-03')
    assert nospot['iv30'] is None and nospot['pc_ratio'] == pytest.approx(1.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/strategies/test_options_surface.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'strategies.options_surface'`

- [ ] **Step 3: Implement the module**

```python
# src/strategies/options_surface.py
"""Options surface features — ONE implementation for live and backtest.

Spec: docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md Part A.
Pure functions over a single ticker's chain rows for a single session. No
environment reads, no I/O, deterministic. Both engine.load_aux_data (live) and
scripts/build_options_surface.py (history) call these; the parity test pins
that they agree on every shared key.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator
from scipy.stats import norm

OPTIONS_FEATURES_VERSION = 2
FIT_DTE = (7, 120)          # expiries eligible for a smile fit
CHAIN_DTE = (1, 120)        # rows kept at all
FRONT_DTE_MAX = 45          # "front usable expiry" for greeks / iv_spread
ATM_DELTA = (0.40, 0.60)
CM_TARGETS = (30, 90)
CM_ONE_SIDED_TOL = 10
MIN_STRIKES = 5
IV_MIN = 0.01
DELTA_BAND = (0.05, 0.95)
_GREEKS = ('delta', 'gamma', 'theta', 'vega')
_D1_25_CALL = float(norm.ppf(0.25))   # −0.6745
_D1_25_PUT = float(norm.ppf(0.75))    # +0.6745

SCALAR_KEYS = [
    'spot', 'iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d',
    'ts_ratio', 'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm',
    'call_volume', 'put_volume', 'volume', 'pc_ratio', 'expiry_date',
    'n_expiries_fit', 'n_strikes_30d', 'options_features_version',
]


@dataclass
class SmileFit:
    dte: int
    t: float
    atm_iv: float
    iv_25d_put: float | None
    iv_25d_call: float | None
    n_strikes: int
    k_min: float
    k_max: float


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _mean(s) -> float | None:
    x = pd.to_numeric(s, errors='coerce').dropna()
    return float(x.mean()) if len(x) else None


def prepare_chain(df: pd.DataFrame, as_of) -> pd.DataFrame:
    """Shared filters (spec A.3): zero-greek rows dropped, option_type upper,
    dte attached, 1 ≤ dte ≤ 120. Returns a copy; never mutates the input."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(df.columns) + ['dte'] if df is not None else ['dte'])
    out = df.copy()
    as_of_ts = pd.Timestamp(as_of).normalize()
    out['expiry'] = pd.to_datetime(out['expiry'], errors='coerce')
    out = out.dropna(subset=['expiry'])
    present = [c for c in _GREEKS if c in out.columns]
    if present:
        out = out[~(out[present].fillna(0) == 0).all(axis=1)]
    out['option_type'] = out['option_type'].astype(str).str.upper()
    out['dte'] = (out['expiry'].dt.normalize() - as_of_ts).dt.days.astype(int)
    out = out[(out['dte'] >= CHAIN_DTE[0]) & (out['dte'] <= CHAIN_DTE[1])]
    return out


def _otm_side(exp_rows: pd.DataFrame, spot: float) -> pd.DataFrame:
    """One IV per strike: PUT below spot, CALL above, mean of both at spot."""
    r = exp_rows.copy()
    r['iv'] = pd.to_numeric(r['implied_volatility'], errors='coerce')
    r = r[r['iv'] > IV_MIN]
    if 'delta' in r.columns:
        d = pd.to_numeric(r['delta'], errors='coerce').abs()
        r = r[(d.isna()) | (d == 0) | ((d >= DELTA_BAND[0]) & (d <= DELTA_BAND[1]))]
    r['strike'] = pd.to_numeric(r['strike'], errors='coerce')
    r = r.dropna(subset=['strike'])
    side = np.where(r['strike'] < spot, 'PUT', np.where(r['strike'] > spot, 'CALL', 'BOTH'))
    keep = (side == 'BOTH') | (r['option_type'].to_numpy() == side)
    r = r[keep]
    return r.groupby('strike', as_index=False)['iv'].mean().sort_values('strike')


def _moneyness_for_delta(smile, t: float, k_min: float, k_max: float, d1: float, sigma0: float) -> float:
    x = 0.0
    sig = sigma0
    for _ in range(3):
        x = -d1 * sig * math.sqrt(t) + 0.5 * sig * sig * t
        x = min(max(x, k_min), k_max)
        sig = float(smile(x))
        if not (sig > 0):
            sig = sigma0
    return x


def fit_smile(strikes, ivs, spot: float, dte: int) -> SmileFit | None:
    K = np.asarray(strikes, dtype=float)
    iv = np.asarray(ivs, dtype=float)
    ok = np.isfinite(K) & np.isfinite(iv) & (K > 0) & (iv > IV_MIN)
    K, iv = K[ok], iv[ok]
    if len(K) < MIN_STRIKES or not (spot and spot > 0):
        return None
    order = np.argsort(K)
    K, iv = K[order], iv[order]
    k = np.log(K / spot)
    if not (k[0] < 0.0 < k[-1]):
        return None
    smile = PchipInterpolator(k, iv, extrapolate=False)
    t = dte / 365.0
    atm = float(smile(0.0))
    if not (atm > 0):
        return None
    xp = _moneyness_for_delta(smile, t, k[0], k[-1], _D1_25_PUT, atm)
    xc = _moneyness_for_delta(smile, t, k[0], k[-1], _D1_25_CALL, atm)
    ivp = _f(smile(xp))
    ivc = _f(smile(xc))
    return SmileFit(dte=int(dte), t=t, atm_iv=atm, iv_25d_put=ivp, iv_25d_call=ivc,
                    n_strikes=int(len(K)), k_min=float(k[0]), k_max=float(k[-1]))


def constant_maturity(fits: dict, target_dte: int, attr: str) -> float | None:
    pts = sorted((f.dte, getattr(f, attr)) for f in fits.values() if getattr(f, attr) is not None)
    if not pts:
        return None
    for d, v in pts:
        if d == target_dte:
            return float(v)
    lower = [(d, v) for d, v in pts if d < target_dte]
    upper = [(d, v) for d, v in pts if d > target_dte]
    if lower and upper:
        d1, v1 = lower[-1]
        d2, v2 = upper[0]
        t1, t2, tt = d1 / 365.0, d2 / 365.0, target_dte / 365.0
        w1, w2 = v1 * v1 * t1, v2 * v2 * t2
        wt = w1 + (w2 - w1) * (tt - t1) / (t2 - t1)
        return float(math.sqrt(max(wt, 0.0) / tt)) if wt > 0 else None
    d, v = (lower[-1] if lower else upper[0])
    return float(v) if abs(d - target_dte) <= CM_ONE_SIDED_TOL else None


def _empty_row(spot, as_of) -> dict:
    row = {k: None for k in SCALAR_KEYS}
    row.update({'spot': _f(spot), 'last_price': _f(spot), 'near_iv': None, 'far_iv': None,
                'skew_20d': None, 'call_volume': 0.0, 'put_volume': 0.0, 'volume': 0.0,
                'n_expiries_fit': 0, 'n_strikes_30d': 0,
                'options_features_version': OPTIONS_FEATURES_VERSION})
    return row


def features_for_day(chain: pd.DataFrame, spot, as_of) -> dict:
    """Per-(ticker, session) surface features (spec A.4)."""
    row = _empty_row(spot, as_of)
    if chain is None or len(chain) == 0:
        return row
    ch = chain if 'dte' in chain.columns else prepare_chain(chain, as_of)
    if len(ch) == 0:
        return row
    calls = ch[ch['option_type'] == 'CALL']
    puts = ch[ch['option_type'] == 'PUT']
    cv = float(pd.to_numeric(calls.get('volume'), errors='coerce').fillna(0).sum()) if 'volume' in ch.columns else 0.0
    pv = float(pd.to_numeric(puts.get('volume'), errors='coerce').fillna(0).sum()) if 'volume' in ch.columns else 0.0
    row.update({'call_volume': cv, 'put_volume': pv, 'volume': cv + pv,
                'pc_ratio': (pv / cv) if cv > 0 else None})
    # Front usable expiry: greeks + iv_spread on the raw ATM band.
    front = ch[ch['dte'] <= FRONT_DTE_MAX]
    if front.empty:
        front = ch
    front_dte = int(front['dte'].min())
    fr = front[front['dte'] == front_dte]
    row['expiry_date'] = (pd.Timestamp(as_of).normalize() + pd.Timedelta(days=front_dte)).date().isoformat()
    if 'delta' in fr.columns:
        d = pd.to_numeric(fr['delta'], errors='coerce').abs()
        atm = fr[(d >= ATM_DELTA[0]) & (d <= ATM_DELTA[1])]
        row['gamma_atm'] = _mean(atm['gamma']) if 'gamma' in atm.columns else None
        row['theta_atm'] = _mean(atm['theta']) if 'theta' in atm.columns else None
        ca = _mean(atm[atm['option_type'] == 'CALL']['implied_volatility'])
        pa = _mean(atm[atm['option_type'] == 'PUT']['implied_volatility'])
        row['iv_spread'] = (ca - pa) if (ca is not None and pa is not None) else None
    spot_f = _f(spot)
    if not (spot_f and spot_f > 0):
        return row
    fits: dict[int, SmileFit] = {}
    for dte, exp_rows in ch[(ch['dte'] >= FIT_DTE[0]) & (ch['dte'] <= FIT_DTE[1])].groupby('dte'):
        side = _otm_side(exp_rows, spot_f)
        fit = fit_smile(side['strike'].to_numpy(), side['iv'].to_numpy(), spot_f, int(dte))
        if fit is not None:
            fits[int(dte)] = fit
    row['n_expiries_fit'] = len(fits)
    if not fits:
        return row
    iv30 = constant_maturity(fits, 30, 'atm_iv')
    iv90 = constant_maturity(fits, 90, 'atm_iv')
    p30 = constant_maturity(fits, 30, 'iv_25d_put')
    c30 = constant_maturity(fits, 30, 'iv_25d_call')
    near30 = min(fits, key=lambda d: abs(d - 30))
    row.update({
        'iv30': iv30, 'iv90': iv90, 'near_iv': iv30, 'far_iv': iv90,
        'ts_ratio': (iv30 / iv90) if (iv30 and iv90 and iv90 > 0) else None,
        'term_slope': (iv90 - iv30) if (iv30 is not None and iv90 is not None) else None,
        'iv_25d_put_30d': p30, 'iv_25d_call_30d': c30,
        'skew_25d_30d': (p30 - iv30) if (p30 is not None and iv30 is not None) else None,
        'rr_25d_30d': (p30 - c30) if (p30 is not None and c30 is not None) else None,
        'n_strikes_30d': fits[near30].n_strikes,
    })
    row['skew_20d'] = row['skew_25d_30d']
    return row
```

- [ ] **Step 4: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_options_surface.py -q 2>&1 | tail -3`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/strategies/options_surface.py tests/strategies/test_options_surface.py && git commit -q -m "feat(options): options_surface — shared chain prep, PCHIP smile, constant-maturity ATM/25d features (task 7)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `options_surface` series features

**Files:**
- Modify: `src/strategies/options_surface.py` (append)
- Test: `tests/strategies/test_options_surface_series.py`

**Interfaces:**
- Produces:
  - constants `HIST_LEN = 20`, `IV_RANK_WINDOW = 252`, `IV_RANK_MIN_OBS = 20`, `ZSCORE_WINDOW = 60`, `ZSCORE_MIN_OBS = 10`, `RV_WINDOW = 20`
  - `rv_series_from_closes(closes: pd.Series) -> pd.Series` (log-return rolling std × √252, index = dates)
  - `series_frame(df: pd.DataFrame) -> pd.DataFrame` — input columns `date, iv30, pc_ratio, rv_20` (one ticker, ascending, NaN allowed); output adds `vrp, iv_rank, vrp_zscore, iv_rank_history, vrp_history, hv20_history, pc_ratio_history`
  - `series_features(today: dict, history: pd.DataFrame, rv: pd.Series) -> dict` — `today` is a `features_for_day` row plus `'date'`; `history` has columns `date, iv30, pc_ratio` for dates before today; `rv` is `rv_series_from_closes` output. Returns the last row of `series_frame` over history+today as a dict (keys above plus `rv_20`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_options_surface_series.py
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest

from strategies import options_surface as osf


def test_rv_series_is_log_return_std_annualised():
    idx = pd.bdate_range('2026-01-02', periods=40)
    closes = pd.Series(100 * np.exp(np.cumsum(np.full(40, 0.01))), index=idx)
    rv = osf.rv_series_from_closes(closes)
    assert rv.index.equals(idx)
    assert math.isnan(rv.iloc[19])                  # needs 20 returns → first value at position 20
    assert rv.iloc[-1] == pytest.approx(0.0, abs=1e-12)   # constant log return → zero vol


def _frame(n, iv):
    idx = pd.bdate_range('2025-06-02', periods=n)
    return pd.DataFrame({'date': idx, 'iv30': iv, 'pc_ratio': np.linspace(0.8, 1.2, n), 'rv_20': np.full(n, 0.15)})


def test_series_frame_iv_rank_none_below_min_obs_then_percentile():
    df = _frame(25, np.linspace(0.10, 0.34, 25))
    out = osf.series_frame(df)
    assert out['iv_rank'].iloc[18] is None or pd.isna(out['iv_rank'].iloc[18])
    assert out['iv_rank'].iloc[19] == pytest.approx(100.0)          # 20th obs is the max of its window
    assert out['iv_rank'].iloc[-1] == pytest.approx(100.0)
    df2 = _frame(30, np.r_[np.linspace(0.30, 0.10, 29), 0.20])
    assert osf.series_frame(df2)['iv_rank'].iloc[-1] == pytest.approx(pd.Series(df2['iv30']).rank(pct=True).iloc[-1] * 100)


def test_series_frame_histories_and_zscore():
    df = _frame(80, 0.2 + 0.05 * np.sin(np.arange(80) / 5))
    out = osf.series_frame(df)
    last = out.iloc[-1]
    assert len(last['iv_rank_history']) == osf.HIST_LEN and len(last['vrp_history']) == osf.HIST_LEN
    assert len(last['hv20_history']) == osf.HIST_LEN and len(last['pc_ratio_history']) == osf.HIST_LEN
    assert last['vrp'] == pytest.approx(df['iv30'].iloc[-1] - 0.15)
    assert last['vrp_zscore'] is not None and math.isfinite(last['vrp_zscore'])
    assert out['vrp_zscore'].iloc[5] is None or pd.isna(out['vrp_zscore'].iloc[5])


def test_series_features_matches_last_row_of_series_frame():
    idx = pd.bdate_range('2025-06-02', periods=60)
    hist = pd.DataFrame({'date': idx[:-1], 'iv30': np.linspace(0.2, 0.3, 59), 'pc_ratio': 1.0})
    rv = pd.Series(np.full(60, 0.18), index=idx)
    today = {'date': idx[-1], 'iv30': 0.25, 'pc_ratio': 1.1}
    feat = osf.series_features(today, hist, rv)
    full = osf.series_frame(pd.concat([hist.assign(rv_20=0.18), pd.DataFrame([{**today, 'rv_20': 0.18}])], ignore_index=True)).iloc[-1]
    assert feat['iv_rank'] == pytest.approx(full['iv_rank'])
    assert feat['rv_20'] == pytest.approx(0.18) and feat['vrp'] == pytest.approx(0.07)
    assert feat['iv_rank_history'] == list(full['iv_rank_history'])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_options_surface_series.py -q 2>&1 | tail -3`
Expected: `AttributeError: module 'strategies.options_surface' has no attribute 'rv_series_from_closes'`

- [ ] **Step 3: Append the series functions**

```python
# --- append to src/strategies/options_surface.py ---
HIST_LEN = 20
IV_RANK_WINDOW = 252
IV_RANK_MIN_OBS = 20
ZSCORE_WINDOW = 60
ZSCORE_MIN_OBS = 10
RV_WINDOW = 20
SERIES_KEYS = ['rv_20', 'vrp', 'iv_rank', 'vrp_zscore', 'iv_rank_history', 'vrp_history',
               'hv20_history', 'pc_ratio_history']


def rv_series_from_closes(closes: pd.Series) -> pd.Series:
    """20-session std of log returns × √252, indexed like `closes` (spec A.5)."""
    c = pd.to_numeric(closes, errors='coerce').astype(float)
    lr = np.log(c / c.shift(1))
    return lr.rolling(RV_WINDOW).std() * math.sqrt(252)


def _pct_rank(s: pd.Series) -> pd.Series:
    return s.rolling(IV_RANK_WINDOW, min_periods=IV_RANK_MIN_OBS).rank(pct=True) * 100.0


def _zscore(s: pd.Series) -> pd.Series:
    m = s.rolling(ZSCORE_WINDOW, min_periods=ZSCORE_MIN_OBS).mean()
    sd = s.rolling(ZSCORE_WINDOW, min_periods=ZSCORE_MIN_OBS).std()
    return (s - m) / sd.replace(0, np.nan)


def _history(s: pd.Series, window: int = HIST_LEN, min_len: int = 5) -> pd.Series:
    vals = s.tolist()
    out = []
    for i in range(len(vals)):
        h = [float(v) for v in vals[max(0, i - window + 1):i + 1] if v is not None and not pd.isna(v)]
        out.append(h if len(h) >= min_len else None)
    return pd.Series(out, index=s.index, dtype=object)


def _none_if_nan(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def series_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker time-series features over an ascending frame with columns
    date, iv30, pc_ratio, rv_20 (spec A.5). The single implementation behind
    both the enriched backtest panel and the live per-ticker call."""
    out = df.sort_values('date').reset_index(drop=True).copy()
    for c in ('iv30', 'pc_ratio', 'rv_20'):
        out[c] = pd.to_numeric(out.get(c), errors='coerce')
    out['vrp'] = out['iv30'] - out['rv_20']
    out['iv_rank'] = _pct_rank(out['iv30'])
    out['vrp_zscore'] = _zscore(out['vrp'])
    out['iv_rank_history'] = _history(out['iv_rank'])
    out['vrp_history'] = _history(out['vrp'])
    out['hv20_history'] = _history(out['rv_20'])
    out['pc_ratio_history'] = _history(out['pc_ratio'])
    for c in ('vrp', 'iv_rank', 'vrp_zscore'):
        out[c] = out[c].astype(object).where(out[c].notna(), None)
    return out


def series_features(today: dict, history: pd.DataFrame, rv: pd.Series) -> dict:
    """Series features for ONE day given the ticker's prior surface rows and its
    realized-vol series. Equivalent to the last row of series_frame over
    history + today — the parity contract with the enriched panel."""
    cols = ['date', 'iv30', 'pc_ratio']
    h = history[cols].copy() if history is not None and len(history) else pd.DataFrame(columns=cols)
    t = pd.DataFrame([{c: today.get(c) for c in cols}])
    frame = pd.concat([h, t], ignore_index=True)
    frame['date'] = pd.to_datetime(frame['date']).dt.normalize()
    frame = frame[frame['date'] <= frame['date'].iloc[-1]].drop_duplicates('date', keep='last')
    rv_s = pd.to_numeric(rv, errors='coerce') if rv is not None else pd.Series(dtype=float)
    rv_s.index = pd.to_datetime(rv_s.index).normalize()
    frame['rv_20'] = frame['date'].map(rv_s.to_dict()).astype(float)
    last = series_frame(frame).iloc[-1]
    return {k: (_none_if_nan(last[k]) if not isinstance(last[k], list) else list(last[k])) for k in SERIES_KEYS}
```

- [ ] **Step 4: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_options_surface.py tests/strategies/test_options_surface_series.py -q 2>&1 | tail -3`
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/strategies/options_surface.py tests/strategies/test_options_surface_series.py && git commit -q -m "feat(options): series features — iv_rank percentile (None < 20 obs), vrp z-score, 20-length histories (task 8)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Surface master builder + registrations + real-chain fixture

**Files:**
- Create: `scripts/build_options_surface.py`
- Create: `tests/fixtures/options_chain_2026-09-03.parquet`, `tests/fixtures/options_chain_2026-09-03_spots.json` (generated once by the step below; checked in)
- Modify: `src/data/parquet_store.py` (add `SURFACE_PATH`, `SURFACE_KEYS = ['ticker', 'date']`)
- Modify: `src/strategies/sync_data_ledger.py:23-40` (`PARQUET_MAP['options_surface'] = 'options_surface'`) and `:66-90` (`PROVIDERS['options_surface'] = 'alpaca'`)
- Modify: `src/system_checks/checks/master_freshness.py` (`'options_surface.parquet': ('date', 5)`)
- Test: `tests/scripts/test_build_options_surface.py`

**Interfaces:**
- Consumes: `strategies.options_surface.{prepare_chain, features_for_day, SCALAR_KEYS, OPTIONS_FEATURES_VERSION}`, `append_dedup`.
- Produces: `build_options_surface.build_rows(chain_df, spots: dict[tuple[str, pd.Timestamp], float], oi_lookup=None) -> pd.DataFrame` (one row per (ticker, date): `ticker, date` + `SCALAR_KEYS` + `built_at`), `read_spots(tickers, start, end) -> dict`, `run(start, end, tickers=None, path=None) -> int`. CLI: `python3 scripts/build_options_surface.py --start YYYY-MM-DD --end YYYY-MM-DD [--tickers A,B] [--path …]`.

- [ ] **Step 1: Generate the fixture from the real archive (one-off, checked in)**

```bash
cd /root/openclaw && python3 - <<'EOF'
import json, pyarrow.parquet as pq, pandas as pd
cols=['ticker','date','expiry','strike','option_type','implied_volatility','delta','gamma','theta','vega','open_interest','volume','close','bid','ask']
t=pq.read_table('data/master/options_eod.parquet',columns=cols,filters=[('date','==','2026-09-03'),('ticker','in',['SPY','AAPL','XOM'])]).to_pandas()
t.to_parquet('tests/fixtures/options_chain_2026-09-03.parquet',index=False)
px=pq.read_table('data/master/prices.parquet',columns=['ticker','date','close'],filters=[('ticker','in',['SPY','AAPL','XOM']),('date','>=','2026-04-01'),('date','<=','2026-09-03')]).to_pandas()
px['date']=px['date'].astype(str)
json.dump({'spots':{r.ticker:float(r.close) for r in px[px.date=='2026-09-03'].itertuples()},
           'closes':{tk:{r.date:float(r.close) for r in g.itertuples()} for tk,g in px.groupby('ticker')}},
          open('tests/fixtures/options_chain_2026-09-03_spots.json','w'))
print(len(t),'rows', json.load(open('tests/fixtures/options_chain_2026-09-03_spots.json'))['spots'])
EOF
ls -la tests/fixtures/options_chain_2026-09-03.parquet
```
Expected: ~18,000 rows, spots for SPY/AAPL/XOM, file under 1 MB.

- [ ] **Step 2: Write the failing test**

```python
# tests/scripts/test_build_options_surface.py
from __future__ import annotations
import importlib.util, json, sys
from pathlib import Path
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def _mod():
    spec = importlib.util.spec_from_file_location('build_options_surface', ROOT / 'scripts' / 'build_options_surface.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_build_rows_from_real_chain_fixture():
    m = _mod()
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    spots = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))['spots']
    rows = m.build_rows(chain, {(t, pd.Timestamp('2026-09-03')): s for t, s in spots.items()})
    assert set(rows['ticker']) == {'SPY', 'AAPL', 'XOM'}
    spy = rows[rows.ticker == 'SPY'].iloc[0]
    assert 0.08 < spy['iv30'] < 0.20            # true 30d ATM, not the 0.40 chain mean
    assert spy['n_expiries_fit'] >= 5 and spy['options_features_version'] == 2
    assert 'built_at' in rows.columns and rows['date'].astype(str).unique().tolist() == ['2026-09-03']
    for c in m.SCALAR_KEYS:
        assert c in rows.columns


def test_run_writes_master_upsert(tmp_path, monkeypatch):
    m = _mod()
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    spots = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))['spots']
    monkeypatch.setattr(m, '_read_range', lambda s, e, tickers=None: chain)
    monkeypatch.setattr(m, 'read_spots', lambda tickers, s, e: {(t, pd.Timestamp('2026-09-03')): v for t, v in spots.items()})
    out = tmp_path / 'options_surface.parquet'
    assert m.run('2026-09-03', '2026-09-03', path=out) == 0
    assert m.run('2026-09-03', '2026-09-03', path=out) == 0        # idempotent upsert
    df = pd.read_parquet(out)
    assert len(df) == 3 and df.duplicated(['ticker', 'date']).sum() == 0
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/scripts/test_build_options_surface.py -q 2>&1 | tail -3`
Expected: `FileNotFoundError` for `scripts/build_options_surface.py`.

- [ ] **Step 4: Store constants + registrations**

`src/data/parquet_store.py` (beside `CALENDAR_PATH`): `SURFACE_PATH = <MASTER_DIR> / 'options_surface.parquet'`, `SURFACE_KEYS = ['ticker', 'date']`.
`src/strategies/sync_data_ledger.py`: `PARQUET_MAP` gains `'options_surface': 'options_surface',   # per-(ticker,date) smile-fit features (spec 2026-09-04 A.6)`; `PROVIDERS` gains `'options_surface': 'alpaca',   # derived from Alpaca-sourced options_eod`.
`src/system_checks/checks/master_freshness.py` `_CADENCES`: `'options_surface.parquet': ('date', 5),`.

- [ ] **Step 5: Implement the builder**

```python
#!/usr/bin/env python3
# scripts/build_options_surface.py
"""Build data/master/options_surface.parquet — one row per (ticker, session)
from options_eod.parquet via strategies.options_surface (spec 2026-09-04 A.7).

Replaces scripts/build_options_aggregates.py as stage 1 of
refresh_options_aggregates.py. Filtered, chunked reads (5 sessions per pass);
spot from prices.parquet; rows upserted with append_dedup on (ticker, date).
The monthly options_aggregates/ files are left untouched and unread.

Usage:
  python3 scripts/build_options_surface.py --start 2026-06-29 --end 2026-09-03 [--tickers SPY,AAPL] [--path …]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / 'src'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
from src.data.parquet_store import SURFACE_KEYS, SURFACE_PATH, append_dedup  # noqa: E402
from strategies.options_surface import (SCALAR_KEYS, OPTIONS_FEATURES_VERSION,  # noqa: E402
                                        features_for_day, prepare_chain)

OPTS_PATH = ROOT / 'data' / 'master' / 'options_eod.parquet'
PRICES_PATH = ROOT / 'data' / 'master' / 'prices.parquet'
COLS = ['ticker', 'date', 'expiry', 'strike', 'option_type', 'implied_volatility',
        'delta', 'gamma', 'theta', 'vega', 'volume']
CHUNK_DAYS = 5
OUT_COLS = ['ticker', 'date'] + SCALAR_KEYS + ['built_at']


def _read_range(start: pd.Timestamp, end: pd.Timestamp, tickers=None) -> pd.DataFrame:
    flt = (pc.field('date') >= pc.scalar(start.strftime('%Y-%m-%d'))) & \
          (pc.field('date') <= pc.scalar(end.strftime('%Y-%m-%d')))
    if tickers:
        flt = flt & pc.field('ticker').isin(list(tickers))
    tbl = pq.read_table(OPTS_PATH, columns=COLS, filters=flt, read_dictionary=['ticker', 'option_type'])
    df = tbl.to_pandas()
    del tbl
    if df.empty:
        return df
    df['ticker'] = df['ticker'].astype(str)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    return df.dropna(subset=['date', 'ticker'])


def read_spots(tickers, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    flt = (pc.field('date') >= pc.scalar(start.strftime('%Y-%m-%d'))) & \
          (pc.field('date') <= pc.scalar(end.strftime('%Y-%m-%d')))
    if tickers:
        flt = flt & pc.field('ticker').isin(list(tickers))
    px = pq.read_table(PRICES_PATH, columns=['ticker', 'date', 'close'], filters=flt,
                       read_dictionary=['ticker']).to_pandas()
    px['ticker'] = px['ticker'].astype(str)
    px['date'] = pd.to_datetime(px['date'])
    return {(r.ticker, r.date): float(r.close) for r in px.itertuples() if r.close == r.close}


def build_rows(chain: pd.DataFrame, spots: dict, oi_lookup=None) -> pd.DataFrame:
    """One surface row per (ticker, date). `oi_lookup(ticker, date) -> dict | None`
    (Part B) merges open-interest keys when supplied."""
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    chain = chain.assign(date=pd.to_datetime(chain['date']).dt.normalize(), ticker=chain['ticker'].astype(str))
    rows = []
    for (ticker, day), grp in chain.groupby(['ticker', 'date'], sort=True):
        prepared = prepare_chain(grp, day)
        row = features_for_day(prepared, spots.get((ticker, day)), day)
        if oi_lookup is not None:
            row.update(oi_lookup(ticker, day) or {})
        rows.append({'ticker': ticker, 'date': day.date(), **{k: row.get(k) for k in SCALAR_KEYS},
                     **{k: v for k, v in row.items() if k.startswith(('gex', 'pcr_oi', 'max_pain', 'contracts_liquid',
                                                                       'iv_centroid_delta', 'surface_premium', 'oi_session'))},
                     'built_at': stamp})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=OUT_COLS)
    df['options_features_version'] = OPTIONS_FEATURES_VERSION
    return df


def run(start: str, end: str, tickers=None, path=None, oi_lookup=None) -> int:
    path = Path(path or SURFACE_PATH)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    total = 0
    t0 = time.time()
    cur = s
    while cur <= e:
        ce = min(cur + pd.Timedelta(days=CHUNK_DAYS - 1), e)
        chain = _read_range(cur, ce, tickers)
        if not chain.empty:
            spots = read_spots(sorted(chain['ticker'].unique()), cur - pd.Timedelta(days=7), ce)
            rows = build_rows(chain, spots, oi_lookup)
            del chain
            if not rows.empty:
                total = append_dedup(path, rows, SURFACE_KEYS, mode='replace')
                print(f'[options_surface] {cur.date()}..{ce.date()} rows={len(rows):,} master={total:,} '
                      f'{time.time() - t0:.0f}s', flush=True)
        cur = ce + pd.Timedelta(days=1)
    print(f'[options_surface] done {start}..{end} master_rows={total:,}', flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--tickers', default=None)
    ap.add_argument('--path', default=None)
    a = ap.parse_args(argv)
    tickers = [t.strip() for t in a.tickers.split(',')] if a.tickers else None
    oi_lookup = None
    try:
        from strategies.options_oi import oi_lookup_factory          # Part B (task 13); absent until then
        oi_lookup = oi_lookup_factory()
    except ImportError:
        pass
    return run(a.start, a.end, tickers, a.path, oi_lookup)


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 6: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/scripts/test_build_options_surface.py -q 2>&1 | tail -3`
Expected: `2 passed`

- [ ] **Step 7: Commit**

```bash
cd /root/openclaw && git add scripts/build_options_surface.py tests/scripts/test_build_options_surface.py tests/fixtures/options_chain_2026-09-03.parquet tests/fixtures/options_chain_2026-09-03_spots.json src/data/parquet_store.py src/strategies/sync_data_ledger.py src/system_checks/checks/master_freshness.py && git commit -q -m "feat(options): build_options_surface — surface master from options_eod, registered in ledger + freshness; real-chain fixture (task 9)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Enriched panel from the surface master + refresh runner

**Files:**
- Modify: `scripts/compute_rolling_options_fields.py` (whole `main`, `load_all_aggregates` → `load_surface`, `add_rolling` → `series_frame`)
- Modify: `src/strategies/aux_data_loader.py:112-124` (`FIELDS`), `:184-186` (drop the `skew_20d ← skew` alias block; the column is emitted directly)
- Modify: `scripts/refresh_options_aggregates.py:41-46`
- Test: `tests/scripts/test_compute_rolling_from_surface.py`

**Interfaces:**
- Consumes: `strategies.options_surface.{series_frame, rv_series_from_closes, SERIES_KEYS, SCALAR_KEYS}`.
- Produces: `compute_rolling_options_fields.build_panel(surface: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame` where `closes` has `ticker, date, close`; the output carries every name in `aux_data_loader.FIELDS`. Legacy aliases filled: `iv_front=iv30, iv_back=iv90, otm_put_iv=iv_25d_put_30d, otm_call_iv=iv_25d_call_30d, skew=skew_25d_30d, put_call_vol_ratio=pc_ratio, unusual_flow=int(pc_ratio>1.5)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_compute_rolling_from_surface.py
from __future__ import annotations
import importlib.util
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location('crof', ROOT / 'scripts' / 'compute_rolling_options_fields.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_build_panel_from_surface_rows():
    m = _mod()
    from strategies.aux_data_loader import FIELDS
    idx = pd.bdate_range('2026-05-01', periods=70)
    surf = pd.DataFrame({'ticker': 'ZZZT', 'date': idx.date, 'spot': 100.0, 'iv30': np.linspace(0.2, 0.3, 70),
                         'iv90': 0.28, 'iv_25d_put_30d': 0.27, 'iv_25d_call_30d': 0.22, 'skew_25d_30d': 0.02,
                         'rr_25d_30d': 0.05, 'ts_ratio': 0.9, 'term_slope': 0.03, 'iv_spread': 0.0,
                         'gamma_atm': 0.01, 'theta_atm': -0.02, 'call_volume': 100.0, 'put_volume': 160.0,
                         'volume': 260.0, 'pc_ratio': 1.6, 'expiry_date': '2026-06-19', 'n_expiries_fit': 4,
                         'n_strikes_30d': 20, 'options_features_version': 2, 'built_at': 'x'})
    closes = pd.DataFrame({'ticker': 'ZZZT', 'date': pd.bdate_range('2026-03-01', periods=115).date,
                           'close': 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 115)))})
    panel = m.build_panel(surf, closes)
    for f in FIELDS:
        assert f in panel.columns, f
    last = panel.sort_values('date').iloc[-1]
    assert last['iv_front'] == last['iv30'] and last['skew_20d'] == last['skew_25d_30d']
    assert last['iv_rank'] == 100.0 and last['unusual_flow'] == 1
    assert isinstance(last['iv_rank_history'], list) and len(last['iv_rank_history']) == 20
    assert panel['iv_rank'].isna().sum() == 19            # first 19 rows below IV_RANK_MIN_OBS
    assert 0.0 < last['rv_20'] < 1.0 and abs(last['vrp'] - (last['iv30'] - last['rv_20'])) < 1e-12
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/scripts/test_compute_rolling_from_surface.py -q 2>&1 | tail -3`
Expected: `AttributeError: module 'crof' has no attribute 'build_panel'`

- [ ] **Step 3: Rewrite `compute_rolling_options_fields.py`**

Replace the file body below the imports with:

```python
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from strategies.options_surface import SERIES_KEYS, series_frame, rv_series_from_closes  # noqa: E402

SURFACE_PATH = ROOT / 'data' / 'master' / 'options_surface.parquet'
OUT_PATH = ROOT / 'data' / 'master' / 'options_aggregates_enriched.parquet'
PRICES_PATH = ROOT / 'data' / 'master' / 'prices.parquet'

LEGACY_ALIASES = {'iv_front': 'iv30', 'iv_back': 'iv90', 'otm_put_iv': 'iv_25d_put_30d',
                  'otm_call_iv': 'iv_25d_call_30d', 'skew': 'skew_25d_30d', 'skew_20d': 'skew_25d_30d',
                  'put_call_vol_ratio': 'pc_ratio', 'near_iv': 'iv30', 'far_iv': 'iv90'}


def load_surface() -> pd.DataFrame:
    if not SURFACE_PATH.exists():
        raise SystemExit(f'No surface master at {SURFACE_PATH} — run scripts/build_options_surface.py first')
    df = pd.read_parquet(SURFACE_PATH)
    df['date'] = pd.to_datetime(df['date'])
    return df.sort_values(['ticker', 'date']).reset_index(drop=True)


def load_closes(tickers: set, floor: pd.Timestamp) -> pd.DataFrame:
    tbl = pq.read_table(PRICES_PATH, columns=['ticker', 'date', 'close'], read_dictionary=['ticker', 'date'],
                        filters=(pc.field('date') >= pc.scalar(floor.strftime('%Y-%m-%d'))))
    px = tbl.to_pandas(); del tbl
    px['ticker'] = px['ticker'].astype(str)
    px = px[px['ticker'].isin(tickers)]
    px['date'] = pd.to_datetime(px['date'])
    return px[['ticker', 'date', 'close']]


def build_panel(surface: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """Enriched backtest panel: surface scalars + series features + legacy aliases."""
    surf = surface.copy()
    surf['date'] = pd.to_datetime(surf['date'])
    closes = closes.copy(); closes['date'] = pd.to_datetime(closes['date'])
    rv = (closes.sort_values(['ticker', 'date']).groupby('ticker', group_keys=False)
                .apply(lambda g: pd.DataFrame({'ticker': g['ticker'].values, 'date': g['date'].values,
                                               'rv_20': rv_series_from_closes(g.set_index('date')['close']).values})))
    surf = surf.merge(rv, on=['ticker', 'date'], how='left')
    parts = []
    for _, g in surf.groupby('ticker', sort=True):
        parts.append(series_frame(g))
    out = pd.concat(parts, ignore_index=True)
    for alias, src in LEGACY_ALIASES.items():
        out[alias] = out[src]
    out['unusual_flow'] = (pd.to_numeric(out['pc_ratio'], errors='coerce') > 1.5).astype(int)
    out['contracts_liquid'] = out.get('contracts_liquid')
    for c in ('gex', 'iv_centroid_delta', 'surface_premium', 'max_pain', 'pcr_oi', 'oi_session'):
        if c not in out.columns:
            out[c] = None
    return out


def main():
    t0 = time.time()
    df = load_surface()
    print(f'surface rows: {len(df):,} tickers: {df["ticker"].nunique():,} dates: {df["date"].nunique()}')
    closes = load_closes(set(df['ticker'].unique()), df['date'].min() - pd.Timedelta(days=90))
    panel = build_panel(df, closes)
    tmp = OUT_PATH.with_suffix('.parquet.tmp')
    panel.to_parquet(tmp, index=False)
    os.replace(tmp, OUT_PATH)
    print(f'wrote {OUT_PATH} rows={len(panel):,} in {time.time() - t0:.0f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```
(`import os` at the top; keep the existing docstring but replace its field list with: `iv_rank` = 252-session percentile of `iv30` (None below 20 obs); `rv_20` = 20-session log-return vol; `vrp = iv30 − rv_20`; aliases per `LEGACY_ALIASES`.)

- [ ] **Step 4: `aux_data_loader.FIELDS` and the alias block**

Replace `FIELDS` with:
```python
FIELDS = [
    'iv_front', 'iv_back', 'term_slope', 'otm_put_iv', 'otm_call_iv', 'skew',
    'put_call_vol_ratio', 'contracts_liquid', 'spot',
    'rv_20', 'vrp', 'iv_rank', 'vrp_zscore',
    'pc_ratio', 'iv_spread', 'ts_ratio', 'near_iv', 'far_iv', 'iv30',
    'unusual_flow',
    'gamma_atm', 'theta_atm', 'gex',
    'iv_centroid_delta', 'surface_premium',
    'iv_rank_history', 'hv20_history', 'vrp_history', 'pc_ratio_history',
    'volume',
    # options_surface v2 (spec 2026-09-04 A.4/B.2)
    'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d', 'skew_20d',
    'expiry_date', 'n_expiries_fit', 'n_strikes_30d', 'options_features_version',
    'max_pain', 'pcr_oi', 'oi_session',
]
```
and delete the three-line `if hasattr(row, 'skew') ...: sid['skew_20d'] = row.skew` block (the field is now in `FIELDS`). Leave the `spot → last_price` and `earnings_dte` lines.

- [ ] **Step 5: Refresh runner**

In `scripts/refresh_options_aggregates.py` replace the `build_options_aggregates.py` `_run([...])` call with:
```python
    _run(['scripts/build_options_surface.py',
          '--start', start.isoformat(), '--end', end.isoformat()])
```
and update the docstring's stage 1 line accordingly.

- [ ] **Step 6: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/scripts/test_compute_rolling_from_surface.py tests/strategies/test_aux_data_loader_insider_long.py tests/strategies/test_aux_macro_slice.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd /root/openclaw && git add scripts/compute_rolling_options_fields.py src/strategies/aux_data_loader.py scripts/refresh_options_aggregates.py tests/scripts/test_compute_rolling_from_surface.py && git commit -q -m "feat(options): enriched backtest panel from the surface master via series_frame; FIELDS v2; refresh runner stage 1 (task 10)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Live path behind `OPENCLAW_OPTIONS_SURFACE`

**Files:**
- Create: `src/execution/options_aux_v2.py`
- Modify: `src/execution/engine.py` — keep the `_px` window (`:1063-1069`, store as `_px_window`), and after `aux['options'] = opts_dict` (`:1360`) add the v2 hook
- Test: `tests/execution/test_engine_options_surface_shadow.py`

**Interfaces:**
- Consumes: `strategies.options_surface.{prepare_chain, features_for_day, series_features, rv_series_from_closes, OPTIONS_FEATURES_VERSION}`; `strategies.options_oi.{oi_features_for_ticker}` (Part B, optional import).
- Produces:
  - `options_aux_v2.enabled() -> bool` (`OPENCLAW_OPTIONS_SURFACE == '1'`)
  - `options_aux_v2.build(opts: pd.DataFrame, universe, today, master_dir: Path, px_window: pd.DataFrame, earnings_upcoming=None) -> dict[str, dict]`
  - `options_aux_v2.shadow_summary(old: dict, new: dict) -> str` → `[options_surface] shadow n=<n> iv30 old/new median=<r> p90=<r> iv_rank_nonnull=<pct> version=2`
  - `options_aux_v2.load_history(master_dir, tickers, before) -> dict[str, pd.DataFrame]`

- [ ] **Step 1: Write the failing test**

```python
# tests/execution/test_engine_options_surface_shadow.py
from __future__ import annotations
import json, logging
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def _inputs(tmp_path):
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    chain['date'] = pd.to_datetime(chain['date']); chain['expiry'] = pd.to_datetime(chain['expiry'])
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c}
                       for t, m in meta['closes'].items() for d, c in m.items()])
    master = tmp_path / 'master'; master.mkdir()
    # 30 prior sessions of surface history so iv_rank is computable
    hist = pd.DataFrame([{'ticker': t, 'date': d.date(), 'iv30': 0.20 + 0.001 * i, 'pc_ratio': 1.0,
                          'options_features_version': 2}
                         for t in ('SPY', 'AAPL', 'XOM') for i, d in enumerate(pd.bdate_range('2026-07-20', '2026-09-02'))])
    hist.to_parquet(master / 'options_surface.parquet', index=False)
    return chain, px, master


def test_build_returns_v2_keys_with_iv_rank(tmp_path):
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    out = v2.build(chain, ['SPY', 'AAPL', 'XOM', 'ZZZT'], pd.Timestamp('2026-09-03'), master, px)
    assert set(out) == {'SPY', 'AAPL', 'XOM'}
    spy = out['SPY']
    assert spy['options_features_version'] == 2 and 0.08 < spy['iv30'] < 0.20
    assert spy['iv_rank'] is not None and 0 <= spy['iv_rank'] <= 100
    assert spy['last_price'] == pytest.approx(px[(px.ticker == 'SPY') & (px.date == '2026-09-03')]['close'].iloc[0])
    assert isinstance(spy['hv20_history'], list) and spy['rv_20'] > 0
    for k in ('gamma_atm', 'theta_atm', 'pc_ratio', 'vrp', 'expiry_date', 'earnings_dte'):
        assert k in spy


def test_master_row_precedence(tmp_path):
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    m = pd.read_parquet(master / 'options_surface.parquet')
    m = pd.concat([m, pd.DataFrame([{'ticker': 'SPY', 'date': pd.Timestamp('2026-09-03').date(), 'iv30': 0.777,
                                     'pc_ratio': 1.0, 'options_features_version': 2}])], ignore_index=True)
    m.to_parquet(master / 'options_surface.parquet', index=False)
    out = v2.build(chain, ['SPY'], pd.Timestamp('2026-09-03'), master, px)
    assert out['SPY']['iv30'] == pytest.approx(0.777)


def test_shadow_summary_line():
    from execution import options_aux_v2 as v2
    old = {'A': {'iv30': 0.40, 'iv_rank': 50.0}, 'B': {'iv30': 0.50, 'iv_rank': 50.0}}
    new = {'A': {'iv30': 0.20, 'iv_rank': 33.0}, 'B': {'iv30': 0.25, 'iv_rank': None}}
    line = v2.shadow_summary(old, new)
    assert line.startswith('[options_surface] shadow n=2 iv30 old/new median=2.000') and 'iv_rank_nonnull=50%' in line and 'version=2' in line


def test_engine_flag_selects_dict(monkeypatch, caplog, tmp_path):
    from execution import engine, options_aux_v2 as v2
    old = {'SPY': {'iv30': 0.4, 'iv_rank': 50.0}}
    new = {'SPY': {'iv30': 0.12, 'iv_rank': 40.0, 'options_features_version': 2}}
    monkeypatch.setattr(v2, 'build', lambda *a, **k: new)
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE', raising=False)
    with caplog.at_level(logging.INFO):
        assert engine._apply_options_surface(old, None, [], None, None, None) is old
    assert any('[options_surface] shadow' in r.message for r in caplog.records)
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE', '1')
    assert engine._apply_options_surface(old, None, [], None, None, None) is new
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/execution/test_engine_options_surface_shadow.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'execution.options_aux_v2'`

- [ ] **Step 3: Implement `options_aux_v2.py`**

```python
# src/execution/options_aux_v2.py
"""Live per-ticker options aux, version 2 (spec 2026-09-04 A.7) — the same
strategies.options_surface functions the backtest panel is built from.

build() returns {ticker: {…}} with every key the strategies read. The engine
serves it when OPENCLAW_OPTIONS_SURFACE=1 and otherwise logs a one-line
shadow comparison against the legacy dict.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from strategies.options_surface import (OPTIONS_FEATURES_VERSION, SERIES_KEYS, features_for_day,
                                        prepare_chain, rv_series_from_closes, series_features)

logger = logging.getLogger(__name__)
FLAG = 'OPENCLAW_OPTIONS_SURFACE'
HISTORY_DAYS = 400
_HIST_COLS = ['ticker', 'date', 'iv30', 'pc_ratio']


def enabled() -> bool:
    return os.environ.get(FLAG, '0') == '1'


def load_history(master_dir: Path, tickers, before: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Surface-master rows per ticker with date < before (last HISTORY_DAYS calendar days),
    plus the row dated `before` itself when the master already holds it."""
    path = Path(os.environ.get('OPENCLAW_OPTIONS_SURFACE_PATH') or (Path(master_dir) / 'options_surface.parquet'))
    if not path.exists():
        logger.warning('[options_surface] master %s missing — iv_rank/histories unavailable this run', path)
        return {}
    floor = (before - pd.Timedelta(days=HISTORY_DAYS)).strftime('%Y-%m-%d')
    flt = (pc.field('date') >= pc.scalar(pd.Timestamp(floor).date())) & (pc.field('date') <= pc.scalar(before.date())) \
        & pc.field('ticker').isin(list(tickers))
    try:
        df = pq.read_table(path, columns=_HIST_COLS, filters=flt).to_pandas()
    except Exception:  # date stored as string in some vintages
        flt = (pc.field('date') >= pc.scalar(floor)) & (pc.field('date') <= pc.scalar(before.strftime('%Y-%m-%d'))) \
            & pc.field('ticker').isin(list(tickers))
        df = pq.read_table(path, columns=_HIST_COLS, filters=flt).to_pandas()
    df['date'] = pd.to_datetime(df['date']).dt.normalize()
    return {t: g.sort_values('date') for t, g in df.groupby('ticker')}


def build(opts: pd.DataFrame, universe, today, master_dir, px_window: pd.DataFrame,
          earnings_upcoming: pd.DataFrame | None = None) -> dict[str, dict]:
    today = pd.Timestamp(today).normalize()
    uni = set(universe)
    if opts is None or len(opts) == 0:
        return {}
    opts = opts[opts['ticker'].astype(str).isin(uni)]
    tickers = sorted(opts['ticker'].astype(str).unique())
    history = load_history(master_dir, tickers, today)
    px = px_window.copy() if px_window is not None else pd.DataFrame(columns=['ticker', 'date', 'close'])
    px['date'] = pd.to_datetime(px['date']).dt.normalize()
    px['ticker'] = px['ticker'].astype(str)
    closes_by = {t: g.set_index('date')['close'].sort_index() for t, g in px.groupby('ticker')}
    try:
        from strategies.options_oi import oi_features_for_ticker      # Part B; optional until task 13 lands
    except ImportError:
        oi_features_for_ticker = None
    out: dict[str, dict] = {}
    for ticker, grp in opts.groupby(opts['ticker'].astype(str)):
        chain_date = pd.to_datetime(grp['date']).max().normalize()
        chain = grp[pd.to_datetime(grp['date']).dt.normalize() == chain_date]
        closes = closes_by.get(ticker)
        spot = None
        if closes is not None and len(closes):
            upto = closes[closes.index <= chain_date]
            spot = float(upto.iloc[-1]) if len(upto) else None
        hist = history.get(ticker)
        master_today = None
        if hist is not None and (hist['date'] == chain_date).any():
            master_today = hist[hist['date'] == chain_date].iloc[-1]
            hist = hist[hist['date'] < chain_date]
        row = features_for_day(prepare_chain(chain, chain_date), spot, chain_date)
        if master_today is not None:                       # precedence: the official record wins
            row['iv30'] = float(master_today['iv30']) if master_today['iv30'] == master_today['iv30'] else row['iv30']
            row['near_iv'] = row['iv30']
            if master_today['pc_ratio'] == master_today['pc_ratio']:
                row['pc_ratio'] = float(master_today['pc_ratio'])
        rv = rv_series_from_closes(closes) if closes is not None else pd.Series(dtype=float)
        row.update(series_features({'date': chain_date, 'iv30': row['iv30'], 'pc_ratio': row['pc_ratio']},
                                   hist if hist is not None else pd.DataFrame(columns=_HIST_COLS[1:]), rv))
        if oi_features_for_ticker is not None:
            row.update(oi_features_for_ticker(ticker, chain_date, master_dir) or {})
        else:
            row.update({'gex': None, 'iv_centroid_delta': None, 'surface_premium': None, 'contracts_liquid': None,
                        'open_interest_by_strike': {}, 'max_pain': None, 'pcr_oi': None, 'oi_session': None})
        row['earnings_dte'] = None
        if earnings_upcoming is not None and len(earnings_upcoming):
            e = earnings_upcoming[earnings_upcoming['ticker'] == ticker]
            if not e.empty:
                row['earnings_dte'] = int((pd.to_datetime(e['date']).min() - today).days)
        row['surface_date'] = chain_date.date().isoformat()
        out[ticker] = row
    return out


def shadow_summary(old: dict, new: dict) -> str:
    common = [t for t in new if t in old]
    ratios = [old[t]['iv30'] / new[t]['iv30'] for t in common
              if old[t].get('iv30') and new[t].get('iv30')]
    nonnull = sum(1 for t in new if new[t].get('iv_rank') is not None)
    med = float(np.median(ratios)) if ratios else float('nan')
    p90 = float(np.percentile(ratios, 90)) if ratios else float('nan')
    pct = round(100.0 * nonnull / len(new)) if new else 0
    return (f'[options_surface] shadow n={len(new)} iv30 old/new median={med:.3f} p90={p90:.3f} '
            f'iv_rank_nonnull={pct}% version={OPTIONS_FEATURES_VERSION}')
```

- [ ] **Step 4: Engine hook**

In `src/execution/engine.py`:
1. In the HV20 pre-compute block, after `_px = _px.sort_values(['ticker', 'date'])` add `_px_window = _px.copy()`; initialise `_px_window = None` just before that `try:` so it always exists.
2. Add a module-level helper (near `_inject_intraday_options`):

```python
def _apply_options_surface(old: dict, opts, universe, today, master_dir, px_window, earnings=None) -> dict:
    """OPENCLAW_OPTIONS_SURFACE=1 → serve the v2 dict; else serve the legacy
    dict and log the shadow comparison (spec 2026-09-04 A.7)."""
    from execution import options_aux_v2 as _v2
    try:
        new = _v2.build(opts, universe, today, master_dir, px_window, earnings)
    except Exception as exc:  # noqa: BLE001 — the v2 path must never take the legacy path down
        logger.warning('[options_surface] v2 build failed (%s); serving legacy dict', exc)
        return old
    logger.info(_v2.shadow_summary(old, new))
    return new if _v2.enabled() else old
```
3. Replace `aux['options'] = opts_dict` with:
```python
            aux['options'] = _apply_options_surface(opts_dict, opts, universe, today, master_dir,
                                                    _px_window, _upcoming_earnings)
```

- [ ] **Step 5: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/execution/test_engine_options_surface_shadow.py tests/execution/test_engine_intraday_options_overlay.py tests/execution/test_engine_options_window.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw && git add src/execution/options_aux_v2.py src/execution/engine.py tests/execution/test_engine_options_surface_shadow.py && git commit -q -m "feat(options): live options aux v2 behind OPENCLAW_OPTIONS_SURFACE with shadow line; master-row precedence (task 11)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Parity test — engine path ≡ builder path ≡ panel row

**Files:**
- Test: `tests/strategies/test_options_surface_parity.py`

**Interfaces:**
- Consumes: `execution.options_aux_v2.build`, `build_options_surface.build_rows`, `compute_rolling_options_fields.build_panel`, the Task 9 fixture.

- [ ] **Step 1: Write the test**

```python
# tests/strategies/test_options_surface_parity.py
from __future__ import annotations
import importlib.util, json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'
SHARED = ['iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d', 'ts_ratio',
          'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm', 'call_volume', 'put_volume', 'volume',
          'pc_ratio', 'expiry_date', 'n_expiries_fit', 'n_strikes_30d', 'options_features_version']


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str):
        return a == b
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))


def test_live_and_builder_agree_on_every_shared_key(tmp_path):
    from execution import options_aux_v2 as v2
    bos = _script('build_options_surface')
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    day = pd.Timestamp('2026-09-03')
    built = bos.build_rows(chain.assign(date=pd.to_datetime(chain['date'])), {(t, day): s for t, s in meta['spots'].items()})
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c} for t, m in meta['closes'].items() for d, c in m.items()])
    master = tmp_path / 'master'; master.mkdir()
    live = v2.build(chain.assign(date=pd.to_datetime(chain['date']), expiry=pd.to_datetime(chain['expiry'])),
                    ['SPY', 'AAPL', 'XOM'], day, master, px)
    for t in ('SPY', 'AAPL', 'XOM'):
        brow = built[built.ticker == t].iloc[0]
        for k in SHARED:
            assert _same(live[t][k], brow[k]), (t, k, live[t][k], brow[k])


def test_panel_row_equals_series_features_on_the_same_history(tmp_path):
    from execution import options_aux_v2 as v2
    crof = _script('compute_rolling_options_fields')
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    day = pd.Timestamp('2026-09-03')
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c} for t, m in meta['closes'].items() for d, c in m.items()])
    hist = pd.DataFrame([{'ticker': t, 'date': d.date(), 'iv30': 0.20 + 0.002 * i, 'pc_ratio': 1.0 + 0.01 * i,
                          'options_features_version': 2}
                         for t in ('SPY', 'AAPL', 'XOM') for i, d in enumerate(pd.bdate_range('2026-07-20', '2026-09-02'))])
    master = tmp_path / 'master'; master.mkdir()
    hist.to_parquet(master / 'options_surface.parquet', index=False)
    live = v2.build(chain.assign(date=pd.to_datetime(chain['date']), expiry=pd.to_datetime(chain['expiry'])),
                    ['SPY', 'AAPL', 'XOM'], day, master, px)
    surf = pd.concat([hist, pd.DataFrame([{'ticker': t, 'date': day.date(), 'iv30': live[t]['iv30'],
                                           'pc_ratio': live[t]['pc_ratio'], 'options_features_version': 2}
                                          for t in live])], ignore_index=True)
    panel = crof.build_panel(surf, px)
    for t in live:
        prow = panel[(panel.ticker == t) & (panel.date == day)].iloc[0]
        assert live[t]['iv_rank'] == pytest.approx(prow['iv_rank'])
        assert live[t]['rv_20'] == pytest.approx(prow['rv_20'])
        assert live[t]['vrp'] == pytest.approx(prow['vrp'])
        assert live[t]['iv_rank_history'] == pytest.approx(list(prow['iv_rank_history']))
```

- [ ] **Step 2: Run the test**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_options_surface_parity.py -q 2>&1 | tail -3`
Expected: `2 passed`. If a key differs, the defect is in whichever side deviates from `strategies.options_surface` — fix the caller, never special-case the test.

- [ ] **Step 3: Commit**

```bash
cd /root/openclaw && git add tests/strategies/test_options_surface_parity.py && git commit -q -m "test(options): live ≡ builder ≡ panel parity on the 2026-09-03 real-chain fixture (task 12)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Part B — CBOE open interest

### Task 13: `options_oi` — session lookup and OI features

**Files:**
- Create: `src/strategies/options_oi.py`
- Test: `tests/strategies/test_options_oi.py`

**Interfaces:**
- Produces:
  - `cboe_root() -> Path` (honours `OPENCLAW_CBOE_CHAINS_ROOT`, default `data/master/cboe_chains`)
  - `cboe_session_for(as_of, root=None) -> datetime.date | None` — the latest session partition with `date ≤ as_of − 1 day` (spec B.3)
  - `load_cboe_session(session, tickers, root=None) -> pd.DataFrame` (columns `underlying, expiry, option_type, strike, open_interest, iv, delta, gamma, vega, underlying_price`)
  - `oi_features_for_day(rows: pd.DataFrame, as_of) -> dict` with keys `open_interest_by_strike, max_pain, contracts_liquid, gex, pcr_oi, iv_centroid_delta, surface_premium, oi_session`
  - `oi_features_for_ticker(ticker, as_of, master_dir=None) -> dict` (session lookup + load + features; cached per session)
  - `oi_lookup_factory(root=None) -> callable(ticker, date) -> dict | None` (for the builder; returns only the scalar keys, not `open_interest_by_strike`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_options_oi.py
from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from strategies import options_oi as oi


def _partition(root, session, rows):
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(root / f'date={session}.parquet', index=False)


def _rows(session='2026-09-02', underlying='ZZZT', spot=100.0):
    exp_front = (pd.Timestamp(session) + pd.Timedelta(days=16)).date()
    exp_back = (pd.Timestamp(session) + pd.Timedelta(days=44)).date()
    rows = []
    for exp in (exp_front, exp_back):
        for K, coi, poi in ((90.0, 100, 400), (100.0, 300, 300), (110.0, 500, 50)):
            for typ, o in (('C', coi), ('P', poi)):
                rows.append({'date': dt.date.fromisoformat(session), 'underlying': underlying, 'contract_symbol': 'x',
                             'root': underlying, 'expiry': exp, 'option_type': typ, 'strike': K,
                             'bid': 1.0, 'bid_size': 1, 'ask': 1.2, 'ask_size': 1, 'iv': 0.25 + (0.02 if typ == 'P' else 0.0),
                             'open_interest': float(o), 'volume': 10.0, 'delta': (0.5 if typ == 'C' else -0.5) * (1 if K == 100 else (0.6 if K == 90 else 0.4)),
                             'gamma': 0.02, 'vega': 0.1, 'theta': -0.01, 'rho': 0.0, 'theo': 1.1, 'last_trade_price': 1.1,
                             'last_trade_time': None, 'prev_day_close': 1.0, 'underlying_price': spot,
                             'feed_timestamp': f'{session} 17:00:00', 'source': 'cboe'})
    return rows


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / 'cboe_chains'
    _partition(r, '2026-09-01', _rows('2026-09-01'))
    _partition(r, '2026-09-02', _rows('2026-09-02'))
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(r))
    oi.clear_cache()
    return r


def test_session_lookup_is_strictly_before_as_of(root):
    assert oi.cboe_session_for('2026-09-03') == dt.date(2026, 9, 2)
    assert oi.cboe_session_for('2026-09-02') == dt.date(2026, 9, 1)
    assert oi.cboe_session_for('2026-09-01') is None
    assert oi.cboe_session_for('2026-09-08') == dt.date(2026, 9, 2)


def test_oi_features_values(root):
    rows = oi.load_cboe_session(dt.date(2026, 9, 2), ['ZZZT'])
    f = oi.oi_features_for_day(rows, '2026-09-03')
    assert f['oi_session'] == '2026-09-02'
    assert f['open_interest_by_strike'] == {90.0: 500.0, 100.0: 600.0, 110.0: 550.0}
    # max pain: payout minimised at 100 (symmetric OI); front expiry only
    assert f['max_pain'] == 100.0
    assert f['contracts_liquid'] == 6
    assert f['pcr_oi'] == pytest.approx((400 + 300 + 50) * 2 / ((100 + 300 + 500) * 2))
    gex_expected = (0.02 * (100 + 300 + 500) - 0.02 * (400 + 300 + 50)) * 100
    assert f['gex'] == pytest.approx(gex_expected)
    assert f['iv_centroid_delta'] is not None and f['surface_premium'] is not None


def test_ticker_helper_and_builder_lookup(root):
    f = oi.oi_features_for_ticker('ZZZT', '2026-09-03')
    assert f['gex'] is not None and f['oi_session'] == '2026-09-02'
    assert oi.oi_features_for_ticker('NOPE', '2026-09-03')['gex'] is None
    look = oi.oi_lookup_factory()
    d = look('ZZZT', pd.Timestamp('2026-09-03'))
    assert 'open_interest_by_strike' not in d and d['max_pain'] == 100.0
    assert look('ZZZT', pd.Timestamp('2026-09-01')) is None       # no session strictly before
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/strategies/test_options_oi.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'strategies.options_oi'`

- [ ] **Step 3: Implement the module**

```python
# src/strategies/options_oi.py
"""Open-interest features from the CBOE chain partitions (spec 2026-09-04 Part B).

Alpaca's snapshots never carry open interest; CBOE's delayed chains do
(data/master/cboe_chains/date=<session>.parquet since 2026-08-21). Point in
time: for a decision on `as_of` the latest CBOE session STRICTLY before
as_of (T−1 for the 15:00 ET compute; the same rule for a backtest bar).
"""
from __future__ import annotations

import datetime as dt
import functools
import os
import re
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
ROOT_ENV = 'OPENCLAW_CBOE_CHAINS_ROOT'
FRONT_DTE_MAX = 45
OI_KEYS = ['max_pain', 'contracts_liquid', 'gex', 'pcr_oi', 'iv_centroid_delta', 'surface_premium', 'oi_session']
_COLS = ['underlying', 'expiry', 'option_type', 'strike', 'open_interest', 'iv', 'delta', 'gamma', 'vega', 'underlying_price']
_PART_RE = re.compile(r'^date=(\d{4}-\d{2}-\d{2})\.parquet$')


def cboe_root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or (ROOT / 'data' / 'master' / 'cboe_chains'))


def clear_cache() -> None:
    _sessions.cache_clear()
    _load.cache_clear()


@functools.lru_cache(maxsize=4)
def _sessions(root_str: str) -> tuple:
    root = Path(root_str)
    if not root.exists():
        return ()
    out = []
    for p in root.iterdir():
        m = _PART_RE.match(p.name)
        if m:
            out.append(dt.date.fromisoformat(m.group(1)))
    return tuple(sorted(out))


def cboe_session_for(as_of, root: Path | None = None) -> dt.date | None:
    d = pd.Timestamp(as_of).date()
    prior = [s for s in _sessions(str(root or cboe_root())) if s < d]
    return prior[-1] if prior else None


@functools.lru_cache(maxsize=2)
def _load(path_str: str) -> pd.DataFrame:
    df = pq.read_table(path_str, columns=_COLS).to_pandas()
    df['underlying'] = df['underlying'].astype(str)
    df['expiry'] = pd.to_datetime(df['expiry'])
    df['option_type'] = df['option_type'].astype(str).str.upper().str[0]
    return df


def load_cboe_session(session: dt.date, tickers=None, root: Path | None = None) -> pd.DataFrame:
    path = Path(root or cboe_root()) / f'date={session.isoformat()}.parquet'
    if not path.exists():
        return pd.DataFrame(columns=_COLS)
    df = _load(str(path))
    return df[df['underlying'].isin(set(tickers))] if tickers is not None else df


def _empty(session) -> dict:
    return {'open_interest_by_strike': {}, 'max_pain': None, 'contracts_liquid': None, 'gex': None,
            'pcr_oi': None, 'iv_centroid_delta': None, 'surface_premium': None,
            'oi_session': session.isoformat() if session else None}


def oi_features_for_day(rows: pd.DataFrame, as_of) -> dict:
    """OI features for ONE underlying from ONE CBOE session (spec B.2)."""
    if rows is None or rows.empty:
        return _empty(None)
    session = cboe_session_for(as_of)
    out = _empty(session)
    r = rows.copy()
    r['open_interest'] = pd.to_numeric(r['open_interest'], errors='coerce').fillna(0.0)
    r['strike'] = pd.to_numeric(r['strike'], errors='coerce')
    as_of_ts = pd.Timestamp(as_of).normalize()
    r['dte'] = (pd.to_datetime(r['expiry']).dt.normalize() - as_of_ts).dt.days
    r = r[r['dte'] >= 1]
    if r.empty:
        return out
    calls_all, puts_all = r[r['option_type'] == 'C'], r[r['option_type'] == 'P']
    coi, poi = float(calls_all['open_interest'].sum()), float(puts_all['open_interest'].sum())
    out['pcr_oi'] = (poi / coi) if coi > 0 else None
    front = r[r['dte'] <= FRONT_DTE_MAX]
    if front.empty:
        front = r
    fr = front[front['dte'] == front['dte'].min()]
    by_strike = fr.groupby('strike')['open_interest'].sum()
    by_strike = by_strike[by_strike > 0]
    out['open_interest_by_strike'] = {float(k): float(v) for k, v in by_strike.items()}
    out['contracts_liquid'] = int((fr['open_interest'] > 0).sum())
    calls, puts = fr[fr['option_type'] == 'C'], fr[fr['option_type'] == 'P']
    if len(by_strike):
        ks = sorted(by_strike.index)
        best, best_pay = None, None
        for s in ks:
            pay = float(((s - calls['strike']).clip(lower=0) * calls['open_interest']).sum()
                        + ((puts['strike'] - s).clip(lower=0) * puts['open_interest']).sum())
            if best_pay is None or pay < best_pay:
                best, best_pay = float(s), pay
        out['max_pain'] = best
    gc = float((pd.to_numeric(calls['gamma'], errors='coerce').fillna(0) * calls['open_interest']).sum())
    gp = float((pd.to_numeric(puts['gamma'], errors='coerce').fillna(0) * puts['open_interest']).sum())
    out['gex'] = round((gc - gp) * 100, 2) if (coi + poi) > 0 else None
    w = pd.to_numeric(fr['vega'], errors='coerce').abs().fillna(0) * fr['open_interest']
    tw = float(w.sum())
    if tw > 0:
        d = pd.to_numeric(fr['delta'], errors='coerce').fillna(0)
        iv = pd.to_numeric(fr['iv'], errors='coerce').fillna(0)
        out['iv_centroid_delta'] = round(float((d * w).sum() / tw), 4)
        vwiv = float((iv * w).sum() / tw)
        atm5 = fr[pd.to_numeric(fr['delta'], errors='coerce').abs().between(0.45, 0.55)]
        atm_iv5 = pd.to_numeric(atm5['iv'], errors='coerce').dropna()
        out['surface_premium'] = round(vwiv - (float(atm_iv5.mean()) if len(atm_iv5) else vwiv), 4)
    return out


def oi_features_for_ticker(ticker: str, as_of, master_dir=None) -> dict:
    root = (Path(master_dir) / 'cboe_chains') if master_dir and not os.environ.get(ROOT_ENV) else cboe_root()
    session = cboe_session_for(as_of, root)
    if session is None:
        return _empty(None)
    rows = load_cboe_session(session, [ticker], root)
    if rows.empty:
        return _empty(session)
    return oi_features_for_day(rows, as_of)


def oi_lookup_factory(root: Path | None = None):
    """(ticker, date) -> scalar OI keys for the surface-master builder, or None
    when no CBOE session precedes the date."""
    def look(ticker: str, day) -> dict | None:
        session = cboe_session_for(day, root)
        if session is None:
            return None
        rows = load_cboe_session(session, [ticker], root)
        f = oi_features_for_day(rows, day) if not rows.empty else _empty(session)
        return {k: f[k] for k in OI_KEYS}
    return look
```

- [ ] **Step 4: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_options_oi.py -q 2>&1 | tail -3`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/strategies/options_oi.py tests/strategies/test_options_oi.py && git commit -q -m "feat(options): options_oi — CBOE open interest features with strict point-in-time session lookup (task 13)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: Wire OI into the builder, the live path, and the freshness guard

**Files:**
- Modify: `scripts/build_options_surface.py` (already imports `oi_lookup_factory` when present — verify the builder writes the OI scalars)
- Modify: `src/execution/options_aux_v2.py` (already imports `oi_features_for_ticker` when present)
- Modify: `src/system_checks/checks/options_aux_freshness.py` (new guard)
- Test: `tests/scripts/test_build_options_surface_oi.py`, `tests/system_checks/test_options_aux_freshness_oi.py` (create `tests/system_checks/__init__.py` if absent)

**Interfaces:**
- Consumes: Task 13 functions; Task 9 builder; Task 11 live path.
- Produces: surface-master rows carry `max_pain, contracts_liquid, gex, pcr_oi, iv_centroid_delta, surface_premium, oi_session`; live dict carries those plus `open_interest_by_strike`; the freshness check FAILs when the latest panel date has fewer than 400 tickers with non-null `pcr_oi` AND the CBOE root has a session for the prior day.

- [ ] **Step 1: Write the failing tests**

```python
# tests/scripts/test_build_options_surface_oi.py
from __future__ import annotations
import datetime as dt, importlib.util, json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def test_builder_writes_oi_scalars_from_cboe_session(tmp_path, monkeypatch):
    from strategies import options_oi as oi
    spec = importlib.util.spec_from_file_location('bos', ROOT / 'scripts' / 'build_options_surface.py')
    bos = importlib.util.module_from_spec(spec); spec.loader.exec_module(bos)
    root = tmp_path / 'cboe_chains'; root.mkdir()
    rows = [{'date': dt.date(2026, 9, 2), 'underlying': 'SPY', 'expiry': dt.date(2026, 9, 18), 'option_type': t,
             'strike': k, 'open_interest': o, 'iv': 0.12, 'delta': d, 'gamma': 0.01, 'vega': 0.5, 'underlying_price': 640.0}
            for k, o, d, t in ((630.0, 1000.0, 0.6, 'C'), (640.0, 2000.0, 0.5, 'C'), (650.0, 500.0, 0.4, 'C'),
                               (630.0, 900.0, -0.4, 'P'), (640.0, 2200.0, -0.5, 'P'), (650.0, 300.0, -0.6, 'P'))]
    pd.DataFrame(rows).to_parquet(root / 'date=2026-09-02.parquet', index=False)
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(root)); oi.clear_cache()
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet'); chain['date'] = pd.to_datetime(chain['date'])
    spots = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))['spots']
    out = bos.build_rows(chain, {(t, pd.Timestamp('2026-09-03')): s for t, s in spots.items()}, oi.oi_lookup_factory())
    spy = out[out.ticker == 'SPY'].iloc[0]
    assert spy['oi_session'] == '2026-09-02' and spy['gex'] is not None and spy['max_pain'] == 640.0
    assert out[out.ticker == 'AAPL'].iloc[0]['gex'] is None      # no CBOE rows for AAPL in this fixture
```

```python
# tests/system_checks/test_options_aux_freshness_oi.py
from __future__ import annotations
import datetime as dt
import pandas as pd

from system_checks.checks import options_aux_freshness as chk
from system_checks.types import Status


def test_oi_coverage_guard(tmp_path, monkeypatch):
    panel = tmp_path / 'enriched.parquet'
    today = pd.Timestamp.today().normalize()
    df = pd.DataFrame({'ticker': [f'T{i}' for i in range(500)], 'date': today, 'gex': None,
                       'contracts_liquid': None, 'iv_centroid_delta': None, 'surface_premium': None,
                       'pcr_oi': [None] * 300 + [1.0] * 200})
    df.to_parquet(panel, index=False)
    monkeypatch.setenv('OPTIONS_ENRICHED_PANEL', str(panel))
    root = tmp_path / 'cboe_chains'; root.mkdir()
    pd.DataFrame([{'underlying': 'T1'}]).to_parquet(root / f'date={(today - pd.Timedelta(days=1)).date()}.parquet', index=False)
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(root))
    status, msg = chk.oi_coverage(min_tickers=400)
    assert status == Status.FAIL and '200' in msg
    df['pcr_oi'] = 1.0; df.to_parquet(panel, index=False)
    assert chk.oi_coverage(min_tickers=400)[0] == Status.PASS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/scripts/test_build_options_surface_oi.py tests/system_checks/test_options_aux_freshness_oi.py -q 2>&1 | tail -3`
Expected: the builder test fails on `oi_session` (None) only if Task 9's `build_rows` did not pass the lookup — confirm it does; the freshness test fails with `AttributeError: ... has no attribute 'oi_coverage'`.

- [ ] **Step 3: Add the freshness guard**

Append to `src/system_checks/checks/options_aux_freshness.py`:

```python
_OI_MIN_TICKERS = int(os.environ.get('OPTIONS_OI_MIN_TICKERS', '400'))


def oi_coverage(min_tickers: int = _OI_MIN_TICKERS):
    """FAIL when the latest panel date carries CBOE open interest for fewer than
    `min_tickers` tickers while a CBOE session for the prior day exists
    (spec 2026-09-04 B.4). PASS when OI is present; WARN when no CBOE session
    is available yet (the stream started 2026-08-21)."""
    import pandas as pd
    from strategies.options_oi import cboe_session_for
    panel = Path(os.environ.get('OPTIONS_ENRICHED_PANEL', str(_PANEL)))
    try:
        df = pd.read_parquet(panel, columns=['ticker', 'date', 'pcr_oi'])
    except Exception as e:  # noqa: BLE001
        return Status.WARN, f'panel unreadable: {e}'
    if df.empty:
        return Status.FAIL, 'enriched options panel is empty'
    latest = pd.to_datetime(df['date']).max()
    if cboe_session_for(latest) is None:
        return Status.WARN, f'no CBOE session before {latest.date()} — OI features legitimately NULL'
    n = int(df[pd.to_datetime(df['date']) == latest]['pcr_oi'].notna().sum())
    if n < min_tickers:
        return Status.FAIL, f'only {n} tickers carry CBOE open interest on {latest.date()} (need ≥ {min_tickers})'
    return Status.PASS, f'{n} tickers carry CBOE open interest on {latest.date()}'


@check('options_oi_coverage', tags=('strategies', 'storage'))
def _check_oi_coverage():
    return oi_coverage()
```
(Match the `@check(...)` decorator signature used by the existing check in the same file — copy its exact form and argument names.)

- [ ] **Step 4: Run the tests**

Run: `cd /root/openclaw && python3 -m pytest tests/scripts/test_build_options_surface_oi.py tests/system_checks/test_options_aux_freshness_oi.py tests/strategies/test_options_surface_parity.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add scripts/build_options_surface.py src/execution/options_aux_v2.py src/system_checks/checks/options_aux_freshness.py tests/scripts/test_build_options_surface_oi.py tests/system_checks/__init__.py tests/system_checks/test_options_aux_freshness_oi.py && git commit -q -m "feat(options): CBOE open interest wired into the surface master, the live v2 dict and a coverage check (task 14)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

# Rollout

### Task 15: Panel rebuild, re-backtests, runbook, changelog

**Files:**
- Create: `scripts/rebacktest_options_sleeve.sh`
- Create: `docs/runbooks/2026-09-04-options-surface-rollout.md`
- Modify: `docs/archive/changelog.md` (newest-first entry)

**Interfaces:**
- Consumes: everything above; `python3 -m backtest.unified_backtest --strategy-file <abs> --universe-cap tier_liquid --start-date 2023-09-04`.

- [ ] **Step 1: Preserve the v1 panel and rebuild the surface master + panel**

```bash
cd /root/openclaw && mkdir -p data/derived && cp -n data/master/options_aggregates_enriched.parquet data/derived/options_aggregates_enriched.v1-2026-09-04.parquet && \
sudo systemd-run --unit=options-surface-build-20260905 --nice=19 -p MemoryMax=3500M -p RuntimeMaxSec=3h \
  -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src --working-directory=/root/openclaw \
  /bin/bash -c 'python3 scripts/build_options_surface.py --start 2026-06-29 --end 2026-09-04 && python3 scripts/compute_rolling_options_fields.py' && \
sleep 5 && journalctl -u options-surface-build-20260905 --no-pager | tail -3
```
Expected within ~40 min: `[options_surface] done 2026-06-29..2026-09-04 master_rows=~250,000` then `wrote …/options_aggregates_enriched.parquet rows=~250,000`. Verify:

```bash
cd /root/openclaw && python3 - <<'EOF'
import pandas as pd
p=pd.read_parquet('data/master/options_aggregates_enriched.parquet')
last=p[p.date==p.date.max()]
print('dates',p.date.nunique(),'latest',p.date.max().date(),'tickers',last.ticker.nunique(),
      'iv30 median',round(last.iv30.median(),3),'iv_rank nonnull %',round(last.iv_rank.notna().mean()*100),
      'pcr_oi nonnull',int(last.pcr_oi.notna().sum()),'version',last.options_features_version.unique())
print(last[last.ticker=='SPY'][['iv30','iv90','skew_25d_30d','iv_rank','pcr_oi','gex']].round(4).to_string(index=False))
EOF
python3 -m system_checks --check options_aux_freshness --json | head -c 400; python3 -m system_checks --check options_oi_coverage --json | head -c 300
```
Expected: SPY iv30 ≈ 0.12–0.13 (not 0.40); iv_rank non-null > 90 % of tickers with ≥ 20 sessions; pcr_oi non-null ≈ 550.

- [ ] **Step 2: Write the re-backtest script**

```bash
#!/usr/bin/env bash
# scripts/rebacktest_options_sleeve.sh — serial re-backtests after the options
# surface v2 panel rebuild (spec 2026-09-04 A.8) + the holiday strategy (D.3).
# Run as a transient unit outside the Saturday research lane:
#   sudo systemd-run --unit=rebacktest-options-20260905 --nice=19 -p MemoryMax=3500M \
#     -p RuntimeMaxSec=6h -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src \
#     --working-directory=/root/openclaw /bin/bash scripts/rebacktest_options_sleeve.sh
set -uo pipefail
cd /root/openclaw
IMPL=src/strategies/implementations
STRATS=(
  "$IMPL/S21_iv_hv_spread.py"
  "$IMPL/shv8_gamma_theta_carry.py"
  "$IMPL/shv19_iv_surface_tilt.py"
  "$IMPL/shv20_iv_dispersion_reversion.py"
  "$IMPL/S_options_flow_confirmed_momentum.py"
  "$IMPL/S_pre_earnings_vol_runup.py"
  "$IMPL/S_holiday_seasonality_energy_etf_tv1.py"
)
rc_all=0
for f in "${STRATS[@]}"; do
  echo "[rebacktest] $(date -u +%FT%TZ) start $(basename "$f")"
  python3 -m backtest.unified_backtest --strategy-file "$PWD/$f" --universe-cap tier_liquid --start-date 2023-09-04
  rc=$?; echo "[rebacktest] $(date -u +%FT%TZ) rc=$rc $(basename "$f")"; [ $rc -ne 0 ] && rc_all=1
done
echo "[rebacktest] done rc_all=$rc_all"
exit $rc_all
```

- [ ] **Step 3: Launch it in a quiet window and record the results**

Quiet window: Saturday 2026-09-05 before 12:00 UTC, or Sunday after 04:00 UTC; never overlapping `openclaw-sunday-research-*` (Sat 12:00/18:00/22:00 UTC) or the Monday 04:00 UTC weights timer.

```bash
cd /root/openclaw && sudo systemd-run --unit=rebacktest-options-20260905 --nice=19 -p MemoryMax=3500M -p RuntimeMaxSec=6h \
  -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src --working-directory=/root/openclaw /bin/bash scripts/rebacktest_options_sleeve.sh
```
Then, after `[rebacktest] done`:
```bash
cd /root/openclaw && python3 - <<'EOF'
import os; from dotenv import load_dotenv; load_dotenv('/root/openclaw/.env')
import psycopg2, psycopg2.extras
c=psycopg2.connect(os.environ['POSTGRES_URI'],cursor_factory=psycopg2.extras.RealDictCursor); cur=c.cursor()
sids=('S21_iv_hv_spread','S_HV8_gamma_theta_carry','S_HV19_iv_surface_tilt','S_HV20_iv_dispersion_reversion','S_options_flow_confirmed_momentum','S_pre_earnings_vol_runup','S_holiday_seasonality_energy_etf_tv1')
cur.execute("""select r.strategy_id, g.regime_state, round(g.sharpe::numeric,2) s, g.trade_count n, u.run_at::date d, u.config_json->'rf'->>'source' rf
  from strategy_backtest_regimes g join strategy_backtest_runs u on u.run_id=g.run_id
  join (select strategy_id, max(run_at) mx from strategy_backtest_runs where strategy_id in %s group by 1) r on r.strategy_id=u.strategy_id and u.run_at=r.mx order by 1,2""",(sids,))
for r in cur.fetchall(): print(r['strategy_id'],r['regime_state'],r['s'],r['n'],r['d'],r['rf'])
EOF
```
Record the before/after table (before: the 07-30..08-04 numbers in the spec §0 context) in the runbook.

- [ ] **Step 4: Write the runbook**

`docs/runbooks/2026-09-04-options-surface-rollout.md`:

```markdown
# Options surface v2 / CBOE OI / macro rf / NYSE calendar — rollout runbook

Spec: docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md · Plan: docs/superpowers/plans/2026-09-04-options-surface-cboe-oi-rf-calendar.md

## State after the build (fill in)
- Surface master: `data/master/options_surface.parquet` rows=… dates=… (first 2026-06-29)
- Enriched panel rebuilt …; v1 copy at `data/derived/options_aggregates_enriched.v1-2026-09-04.parquet`
- Re-backtests (run …): table before → after per regime for the seven strategies.
- Assigners: activation + eligibility re-gate at cadence (daily activation step; Monday 04:00 UTC weights). No manual promotion.

## Flags
| flag | now | flip when |
|---|---|---|
| `OPENCLAW_OPTIONS_SURFACE` | 0 (shadow) | after the first clean `[options_surface] shadow …` line — Tue 2026-09-08 15:00 ET compute (Mon 09-07 Labor Day). Clean = n ≥ 3,000, iv30 old/new median in 1.5–3.5, iv_rank_nonnull ≥ 80 %. Set `=1` in `.env` and `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service`. |
| `OPENCLAW_RF_SOURCE` | const (shadow) | after ≥ 5 live `[rf_shadow]` lines (bench_realized / aggregate_metrics) and one backtest; set `=macro`, restart johnbot; then schedule the fleet re-backtest below. |

## Fleet re-backtest under macro rf (operator-triggered, weekend)
`strategy_backtest_runs.config_json->'rf'->>'source'` distinguishes populations. Run the fleet as serial transient units (`scripts/rebacktest_options_sleeve.sh` pattern over `--all-live`) in a quiet window; the assigners then re-gate. Until then, gate levels mix const (older rows) and macro (newer rows) — the Sharpe delta is ≤ ±0.1 over the 2023-09+ window.

## Watch list
- Tue 2026-09-08 15:00 ET: `[options_surface] shadow` line; `[rf_shadow]` lines; `[exit_hook]` unaffected.
- `python3 -m system_checks --check options_aux_freshness` and `--check options_oi_coverage` daily.
- `openclaw-trading-calendar.timer` monthly; `trading_calendar.parquet` `fetched_at` < 45 d (master_freshness).
- S5_max_pain re-backtest once ≥ 60 CBOE sessions exist (≈ 2026-11-12).

## Rollback
- Options: `OPENCLAW_OPTIONS_SURFACE=0` serves the legacy dict; the legacy block is intact in engine.py. The backtest panel can be restored from the v1 copy.
- rf: `OPENCLAW_RF_SOURCE=const`.
- Calendar: delete/rename `data/master/trading_calendar.parquet` → library falls back (alpaca, then weekday) with a WARNING.
```

- [ ] **Step 5: Changelog entry and commit**

Add at the top of `docs/archive/changelog.md` a dated entry (2026-09-05) summarising: surface v2 shared module + master, CBOE OI, macro rf module + shadow, NYSE calendar master + timer, holiday strategy calendar, re-backtests scheduled, flags and flip conditions.

```bash
cd /root/openclaw && git add scripts/rebacktest_options_sleeve.sh docs/runbooks/2026-09-04-options-surface-rollout.md docs/archive/changelog.md && git commit -q -m "ops(options): surface v2 rollout — panel rebuild, sleeve re-backtest script, runbook, changelog (task 15)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" && git push origin main
```

---

## Self-review (done at authoring time)

- **Spec coverage:** A.1–A.5 → Tasks 7–8; A.6 → Task 9; A.7 → Tasks 10–11; A.8 → Task 15; A.9 → Tasks 7, 8, 11, 12. B.1–B.4 → Tasks 13–14; B.5 → runbook watch list. C.1–C.3 → Tasks 5–6; C.4 → Task 6 shadow + runbook. D.1 → Task 2; D.2 → Task 1; D.3 → Tasks 3a/3b/3c/4; D.4 → Task 1 tests + site tests. E sequencing → task order. F out of scope → nothing.
- **Placeholders:** none; every step carries code or an exact command. Two "verify the real name" notes (Task 3c pipeline helper, Task 14 decorator form) are grep instructions with the expected symbol, not TBDs.
- **Type consistency:** `features_for_day(chain, spot, as_of) -> dict` (7, 9, 11, 12); `series_features(today, history, rv) -> dict` and `series_frame(df)` (8, 10, 11, 12); `excess_sharpe(rets, dates=None, source=None, min_obs=2, asof=None)` (5, 6); `oi_features_for_ticker(ticker, as_of, master_dir=None)` and `oi_lookup_factory(root=None)` (13, 9, 11, 14); `is_session/next_session/prev_session/sessions/sessions_before/is_open/expiry_session` (1, 3a, 3b, 3c, 4); `build_rows(chain, spots, oi_lookup=None)` (9, 12, 14); `build_panel(surface, closes)` (10, 12).
