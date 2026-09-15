# Stream A — gates truthful (fundamentals PIT, gap fill, exit census, epoch) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the promotion/activation gates measure something true. Three engine changes — fundamentals become visible only once they were actually filed (A1), a bar that gaps through a stop fills at the open instead of at the untouched stop level (A2), and every run records its own exit-reason census plus modelled cost drag (A3) — plus the ops epoch that re-derives the whole fleet under A1+A2 and flips them live (A4), plus three stale-registry/manifest hygiene fixes.

**Architecture:** Every behaviour change is a pure-function edit behind an env flag whose unset value is byte-identical to today. A1 lives entirely in `src/strategies/aux_data_loader.py`: a module-cached availability frame (`available_at` per (ticker, period-end), derived from `earnings.parquet` by a single `merge_asof`) that `_financials_slice` slices on instead of the period-end date when `OPENCLAW_FINANCIALS_PIT=1`. The live path (`src/execution/engine.py::load_aux_data`) is *not* changed — it already sees only filed data — so A1 ships with a shape-parity test instead. A2 adds one keyword-only `open_` argument to `src/backtest/unified_backtest.py::_bar_exit`; both call sites (`simulate_trade` and `backtest/open_book.advance_open_book`) already hold the bar Series and pass `bar.get('open')`, so no other signature moves. A3 adds two pure module-level functions to `unified_backtest.py` and two keys to the `json.dumps({...})` literal written to `strategy_backtest_runs.config_json`. A4 copies the proven `target_mode` epoch machinery verbatim: rotate the fleet checkpoint, drop an `Environment=` drop-in on `openclaw-fleet-overnight-resume.service`, arm a weekend transient unit via `scripts/fleet_weekend_window.sh`, and flip `.env` from a gated timer.

**Tech Stack:** Python 3.13, pandas 3.0.2 (`merge_asof`, `groupby.last`), numpy 2.4.4, pyarrow filtered reads (`pq.read_table(..., read_dictionary=['ticker','date'], filters=[('ticker','in',…)])`), psycopg2, pytest (`pytest.ini`: `testpaths = tests`, `pythonpath = src`), bash + systemd transient units and drop-ins, Postgres tables `strategy_backtest_runs` / `strategy_backtest_trades`.

**Spec:** `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` (§0 non-negotiables, §1 Stream A: A1–A4)

## Global Constraints

Spec §0, one per line — these bind every task below:

- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history.
- Backtest side is AUTHORITATIVE (08-07 ruling). Any live/backtest disagreement is fixed on the live side unless the backtest is provably look-ahead — items 1 and 2 are exactly that case.
- Every new behaviour ships behind an env flag whose unset value is byte-identical to today's behaviour, unless the item is a pure bug fix that the operator has explicitly approved (items 3, 10, 11, 16, and the circuit-breaker regime change).
- 2-core / 8 GB / no swap: never load whole `prices.parquet` or `options_eod.parquet`; slice by date/ticker; no always-on threads; no new packages.
- Production = working tree on `main`; timer-spawned scripts pick up the tree on their next run. Work on a worktree branch; merge to main only when the whole stream is green; never leave main half-edited across a timer boundary.
- Tests on this box reach the REAL DB (`.env` loads at import) — stub gates in fixtures; never run the full suite while the fleet runs; never include `test_regime_stratified_backtest`.
- Every file:line cited below was grep-verified on 2026-09-12 against `worktree-qd-adoptions` (base `main d5e6c235`); re-verify before editing (lines drift).
- Log to `docs/archive/changelog.md` (newest first) per stream, not to CLAUDE.md.

Test rules for this plan:

- Run ONLY the task's own test file plus the touching module's existing test file, e.g. `cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_gap_fill.py -q`.
- NEVER run the whole suite. NEVER run `test_regime_stratified_backtest`.
- Tests reach the real Postgres and the real `.env` at import. Stub every DB/gate access in fixtures; no test in this plan opens a connection.
- Synthetic frames only. No test reads `data/master/` — build the financials/earnings/price frames inline and `monkeypatch` the module paths, exactly as `tests/strategies/test_aux_macro_slice.py::test_financials_slice_point_in_time` does.
- `tests/backtest/conftest.py` is autouse and pins `OPENCLAW_BT_FILL_MODEL=close` (legacy t+1) and `OPENCLAW_RF_SOURCE=const`. Do not fight it; write A2 expectations in t+1 geometry.
- A fleet backtest may be running. Never launch a `python3 -m backtest.unified_backtest` run from a task step; the only compute is scheduled by the operator in Task 8.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; one commit per task, task id in the subject.

## File Structure

| File | Responsibility |
|---|---|
| `src/strategies/aux_data_loader.py` (modify) | `available_at` per (ticker, period-end) from `earnings.parquet`; `_financials_slice` slices on it under `OPENCLAW_FINANCIALS_PIT=1` |
| `src/backtest/unified_backtest.py` (modify) | `_bar_exit` gap-fill rule; `simulate_trade` passes the bar open; `exit_reason_census` / `cost_drag_bps` pure functions; four new `config_json` keys |
| `src/backtest/open_book.py` (modify) | the exit-hook stepper passes its bar's open into `_bar_exit` |
| `src/strategies/lifecycle.py` (modify) | `(ARCHIVED, CANDIDATE)` becomes a valid revival transition |
| `src/strategies/registry.py` (modify) | drop the `S_fomc_presell_spy_long` row whose implementation file no longer exists |
| `src/strategies/manifest.json` (modify) | correct two false "prices_30m is no longer collected" reasons; revive both strategies to `candidate` |
| `scripts/measure_gap_fill_impact.py` (new) | read-only per-strategy Δ from re-pricing stored stop exits at the exit bar's open |
| `scripts/pit_gap_flip_gate.py` (new) | read-only G1 uniformity verdict for the live flip (`OK` / `NOT_YET`) |
| `scripts/pit_gap_flip_after_fleet.sh` (new) | gated one-shot: flip both flags in `.env`, drop the fleet drop-in, restart user-scope johnbot |
| `docs/systemd/openclaw-fleet-overnight-resume.service.d/pit-gap.conf` (new) | canonical snapshot of the epoch drop-in |
| `tests/strategies/test_financials_pit.py` (new) | A1 availability rules + legacy byte-identity |
| `tests/strategies/test_financials_pit_parity.py` (new) | backtest slice keys ≡ engine.py live `aux['financials']` keys |
| `tests/backtest/test_gap_fill.py` (new) | A2 gap rules, flag-unset frozen values, missing-`open`-column fallback |
| `tests/backtest/test_exit_census.py` (new) | A3 census + cost drag on a synthetic trade list |
| `tests/scripts/test_measure_gap_fill_impact.py` (new) | the measurement script's pure re-pricing arithmetic |
| `tests/scripts/test_pit_gap_flip_gate.py` (new) | the flip gate's G1 verdict from a rows fixture (no DB) |
| `tests/strategies/test_lifecycle_revival.py` (new) | archived → candidate transition |
| `docs/archive/changelog.md` (modify) | dated Stream A entry, newest first |

---

### Task 1: A1 — `available_at` and the point-in-time financials slice

**Files:**
- Modify: `src/strategies/aux_data_loader.py` — add constants + `_financials_pit_enabled()` + `_financials_with_availability()` immediately after `_load_financials` (currently ends at line 441); edit `_financials_slice` (currently 444-492) at its `df = _load_financials()` / `asof = df[df['date'] <= ts]` head and at the dict comprehension's excluded-key tuple.
- Test: `tests/strategies/test_financials_pit.py` (new)

**Interfaces:**
- Consumes: `aux_data_loader._load_financials() -> pd.DataFrame` (columns include `ticker`, `date`, `period`, numeric fields); `aux_data_loader._load_earnings() -> pd.DataFrame` (columns `ticker`, `date`, where `date` is the report date — see `_load_earnings` at lines 95-110, which coalesces `date` / `report_date` / `earnings_date`).
- Produces:
  - `FINANCIALS_PIT_MAX_LAG_DAYS: int = 120`
  - `FINANCIALS_PIT_FALLBACK_DAYS: int = 60`
  - `_FIN_AVAIL_DF: Optional[pd.DataFrame]` module cache (reset alongside `_FIN_DF` in tests)
  - `_financials_pit_enabled() -> bool`
  - `_financials_with_availability() -> pd.DataFrame` (`_load_financials()` plus an `available_at` datetime64 column)
  - `_financials_slice(date_str: str) -> dict` — signature unchanged

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_financials_pit.py
"""A1 (spec 2026-09-12 §1) — fundamentals become visible on their FILING date,
not their period end. Synthetic frames only; never reads data/master."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies import aux_data_loader as adl


def _install(monkeypatch, tmp_path, fin: pd.DataFrame, earn: pd.DataFrame | None):
    fp = tmp_path / 'financials.parquet'
    fin.to_parquet(fp, index=False)
    monkeypatch.setattr(adl, 'FINANCIALS_PATH', fp)
    ep = tmp_path / 'earnings.parquet'
    (earn if earn is not None else pd.DataFrame({'ticker': [], 'date': []})).to_parquet(ep, index=False)
    monkeypatch.setattr(adl, 'EARNINGS_PATH', ep)
    monkeypatch.setattr(adl, '_FIN_DF', None)
    monkeypatch.setattr(adl, '_FIN_AVAIL_DF', None)
    monkeypatch.setattr(adl, '_EARNINGS_DF', None)


FIN = pd.DataFrame({
    'ticker':       ['AAA', 'BBB'],
    'period':       ['2026Q1', '2026Q1'],
    'date':         ['2026-03-31', '2026-03-31'],
    'roe':          [1.40, 0.20],
    'total_assets': [3.6e11, 1.0e11],
})
EARN = pd.DataFrame({'ticker': ['AAA'], 'date': ['2026-05-05']})


def test_pit_hides_unfiled_quarter(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    assert adl._financials_slice('2026-04-15') == {}


def test_pit_reveals_on_the_report_date(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    out = adl._financials_slice('2026-05-05')
    assert set(out) == {'AAA'}
    assert out['AAA']['returnOnEquity'] == 1.40
    assert 'available_at' not in out['AAA']


def test_pit_fallback_sixty_days_when_no_earnings_row(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    assert 'BBB' not in adl._financials_slice('2026-05-29')   # 03-31 + 60 d = 05-30
    assert 'BBB' in adl._financials_slice('2026-05-30')


def test_pit_ignores_a_report_beyond_the_120_day_window(monkeypatch, tmp_path):
    late = pd.DataFrame({'ticker': ['AAA'], 'date': ['2026-08-15']})   # 03-31 + 137 d
    _install(monkeypatch, tmp_path, FIN, late)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    assert 'AAA' in adl._financials_slice('2026-05-30')   # falls back to +60 d, not 08-15


def test_flag_unset_is_the_legacy_period_end_slice(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.delenv('OPENCLAW_FINANCIALS_PIT', raising=False)
    legacy = adl._financials_slice('2026-04-15')
    assert set(legacy) == {'AAA', 'BBB'}
    assert legacy['AAA']['returnOnEquity'] == 1.40


def test_availability_frame_is_cached(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, FIN, EARN)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    first = adl._financials_with_availability()
    assert adl._financials_with_availability() is first
    assert str(first.loc[first['ticker'] == 'AAA', 'available_at'].iloc[0])[:10] == '2026-05-05'
    assert str(first.loc[first['ticker'] == 'BBB', 'available_at'].iloc[0])[:10] == '2026-05-30'


def test_prior_period_aliases_survive_pit(monkeypatch, tmp_path):
    fin = pd.DataFrame({
        'ticker':          ['AAA', 'AAA'],
        'period':          ['2025Q1', '2026Q1'],
        'date':            ['2025-03-31', '2026-03-31'],
        'total_assets':    [3.0e11, 3.6e11],
        'working_capital': [1.0e10, 1.2e10],
    })
    earn = pd.DataFrame({'ticker': ['AAA', 'AAA'], 'date': ['2025-05-05', '2026-05-05']})
    _install(monkeypatch, tmp_path, fin, earn)
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', '1')
    out = adl._financials_slice('2026-06-01')
    assert out['AAA']['totalAssets'] == 3.6e11
    assert out['AAA']['totalAssetsPriorYear'] == 3.0e11
    assert out['AAA']['workingCapitalPriorYear'] == 1.0e10
```

- [ ] **Step 2: Run them and see the expected failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_financials_pit.py -q
```
Expected: `AttributeError: <module 'strategies.aux_data_loader'> does not have the attribute '_FIN_AVAIL_DF'` from `_install`'s `monkeypatch.setattr` — every test errors, 0 passed.

- [ ] **Step 3: Add the availability frame**

Insert immediately after `_load_financials` (after its `return _FIN_DF`, currently line 441) in `src/strategies/aux_data_loader.py`:

```python
# ── Point-in-time fundamentals availability (spec 2026-09-12 §A1) ────────────
# financials.parquet's `date` is the FMP statement PERIOD END, not a filing
# date (src/pipeline/backfillers/fmp.py::build_financial_rows stores no filing
# date), so slicing on it lets a backtest bar read numbers that were not public
# for weeks — look-ahead the live path cannot have. `available_at` is the first
# earnings report date in earnings.parquet STRICTLY AFTER the period end and
# within FINANCIALS_PIT_MAX_LAG_DAYS; with no such row it is period end +
# FINANCIALS_PIT_FALLBACK_DAYS (the SEC 10-Q deadline neighbourhood).
FINANCIALS_PIT_MAX_LAG_DAYS = 120
FINANCIALS_PIT_FALLBACK_DAYS = 60


def _financials_pit_enabled() -> bool:
    """OPENCLAW_FINANCIALS_PIT=1 selects the availability-date slice. Unset (or
    any other value) keeps the legacy period-end slice, byte-identical."""
    return os.environ.get('OPENCLAW_FINANCIALS_PIT', '0') == '1'


def _financials_with_availability() -> pd.DataFrame:
    """``_load_financials()`` plus an ``available_at`` datetime64 column.

    Module-cached: ``_financials_slice`` is uncached and called once per
    backtest bar, so an un-memoised merge_asof here would cost the fleet a
    merge per bar per strategy over a ~53 h serial epoch.
    """
    global _FIN_AVAIL_DF
    if _FIN_AVAIL_DF is not None:
        return _FIN_AVAIL_DF
    fin = _load_financials()
    if fin.empty:
        _FIN_AVAIL_DF = fin
        return _FIN_AVAIL_DF
    df = fin.copy()
    df['date'] = pd.to_datetime(df['date'])
    earn = _load_earnings()
    if earn.empty or 'date' not in earn.columns:
        df['available_at'] = df['date'] + pd.Timedelta(days=FINANCIALS_PIT_FALLBACK_DAYS)
    else:
        keys = (df[['ticker', 'date']].drop_duplicates()
                  .sort_values('date', kind='mergesort').reset_index(drop=True))
        right = earn[['ticker', 'date']].dropna().rename(columns={'date': 'report_date'})
        right['report_date'] = pd.to_datetime(right['report_date'])
        right = right.sort_values('report_date', kind='mergesort').reset_index(drop=True)
        matched = pd.merge_asof(
            keys, right, left_on='date', right_on='report_date', by='ticker',
            direction='forward', allow_exact_matches=False,
            tolerance=pd.Timedelta(days=FINANCIALS_PIT_MAX_LAG_DAYS))
        df = df.merge(matched, on=['ticker', 'date'], how='left')
        df['available_at'] = df['report_date'].fillna(
            df['date'] + pd.Timedelta(days=FINANCIALS_PIT_FALLBACK_DAYS))
        df = df.drop(columns=['report_date'])
    _FIN_AVAIL_DF = df.sort_values('date', kind='mergesort')
    log.info('aux_data_loader: financials availability built rows=%d (pit lag<=%dd, fallback %dd)',
             len(_FIN_AVAIL_DF), FINANCIALS_PIT_MAX_LAG_DAYS, FINANCIALS_PIT_FALLBACK_DAYS)
    return _FIN_AVAIL_DF
```

Add the cache global next to `_FIN_DF` (currently line 53, `_FIN_DF: Optional[pd.DataFrame] = None`) — insert directly beneath it:

```python
_FIN_AVAIL_DF: Optional[pd.DataFrame] = None
```

- [ ] **Step 4: Slice on `available_at` under the flag**

In `_financials_slice`, replace these four lines (currently 455-459):

```python
    df = _load_financials()
    if df.empty:
        return {}
    ts = pd.to_datetime(date_str)
    asof = df[df['date'] <= ts]
```

with:

```python
    _pit = _financials_pit_enabled()
    df = _financials_with_availability() if _pit else _load_financials()
    if df.empty:
        return {}
    ts = pd.to_datetime(date_str)
    # PIT (spec §A1): a row is visible once it was FILED. Legacy: visible at
    # its period end. The prior-year / prior-quarter transforms below stay on
    # `date` either way — "≥3 quarters older" is a statement about periods.
    asof = df[df['available_at'] <= ts] if _pit else df[df['date'] <= ts]
```

and extend the excluded-key tuple in the dict comprehension (currently `if k not in ('date', 'period') and not isinstance(v, str)`) to:

```python
            if k not in ('date', 'period', 'available_at') and not isinstance(v, str)
```

(`available_at` is a `pd.Timestamp`; leaving it in would raise `TypeError` on `float(v)` and would not exist in the live dict.)

- [ ] **Step 5: Run and PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_financials_pit.py tests/strategies/test_aux_macro_slice.py -q
```
Expected: `7 passed` from the new file and every existing `test_aux_macro_slice.py` test still green (its `test_financials_slice_point_in_time` is the legacy-behaviour lock).

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/strategies/aux_data_loader.py tests/strategies/test_financials_pit.py && git commit -q -m "feat(aux): fundamentals point-in-time availability behind OPENCLAW_FINANCIALS_PIT (Stream A task 1)

available_at per (ticker, period end) = first earnings report strictly after
the period end and within 120 d, else period end + 60 d. Flag unset keeps the
legacy period-end slice byte-identical. Spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md A1.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: A1 — backtest/live shape parity test

**Files:**
- Test: `tests/strategies/test_financials_pit_parity.py` (new)
- Modify: none.

**Interfaces:**
- Consumes: `strategies.aux_data_loader._financials_slice(date_str: str) -> dict` (Task 1); `execution.engine.load_aux_data(universe: list, as_of=None) -> dict` (verified signature at `src/execution/engine.py:894`; its financials block runs at 908-977 and reads `master_dir / 'financials.parquet'` where `master_dir = ROOT / 'data' / 'master'`).
- Produces: the assertion that, on ONE shared synthetic financials frame, `set(bt[ticker]) == set(live[ticker])` for every ticker in both — i.e. PIT does not add, drop, or rename a key the strategies read.

The live path is *not* changed by A1 (spec §A1: "Live path already sees only filed data, so no live change"). This task locks the shape so the PIT frame's extra column can never leak into the dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_financials_pit_parity.py
"""A1 parity (spec 2026-09-12 §A1): the backtest financials slice must have the
SAME dict shape as engine.py's live aux['financials'] on the same frame — PIT
must not add, drop, or rename a key any strategy reads.

Synthetic frames only. engine.load_aux_data's financials block reads
ROOT/'data'/'master'/'financials.parquet', so ROOT is monkeypatched at the
module to a tmp dir holding ONLY that file; every other aux block (insider,
sentiment, options) is absent or DB-backed and degrades to a warning, which is
exactly what we want — this test asserts on aux['financials'] and nothing else.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from strategies import aux_data_loader as adl

FIN = pd.DataFrame({
    'ticker':            ['AAA', 'BBB'],
    'period':            ['2026Q1', '2026Q1'],
    'date':              ['2026-03-31', '2026-03-31'],
    'roe':               [0.31, 0.12],
    'roic':              [0.22, 0.09],
    'gross_margin':      [0.45, 0.30],
    'debt_equity_ratio': [0.80, 1.40],
    'ev_ebitda':         [14.0, 9.0],
    'p_fcf_ratio':       [22.0, 11.0],
    'total_assets':      [3.6e11, 1.0e11],
    'total_liabilities': [2.6e11, 0.7e11],
    'retained_earnings': [0.5e11, 0.2e11],
    'working_capital':   [1.2e10, 0.4e10],
    'operating_income':  [1.1e11, 0.2e11],
    'market_cap':        [3.0e12, 4.0e11],
    'net_income':        [0.9e11, 0.1e11],
})
EARN = pd.DataFrame({'ticker': ['AAA', 'BBB'], 'date': ['2026-05-05', '2026-05-06']})


@pytest.fixture()
def _shared_frames(monkeypatch, tmp_path):
    master = tmp_path / 'data' / 'master'
    master.mkdir(parents=True)
    FIN.to_parquet(master / 'financials.parquet', index=False)
    EARN.to_parquet(master / 'earnings.parquet', index=False)
    monkeypatch.setattr(adl, 'FINANCIALS_PATH', master / 'financials.parquet')
    monkeypatch.setattr(adl, 'EARNINGS_PATH', master / 'earnings.parquet')
    monkeypatch.setattr(adl, '_FIN_DF', None)
    monkeypatch.setattr(adl, '_FIN_AVAIL_DF', None)
    monkeypatch.setattr(adl, '_EARNINGS_DF', None)
    from execution import engine
    monkeypatch.setattr(engine, 'ROOT', tmp_path)
    return engine


@pytest.mark.parametrize('pit', ['0', '1'])
def test_backtest_slice_shape_equals_live_shape(_shared_frames, monkeypatch, pit):
    engine = _shared_frames
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', pit)
    live = engine.load_aux_data(['AAA', 'BBB'], as_of='2026-06-30').get('financials', {})
    bt = adl._financials_slice('2026-06-30')
    assert set(bt) == set(live) == {'AAA', 'BBB'}
    for ticker in ('AAA', 'BBB'):
        assert set(bt[ticker]) == set(live[ticker]), ticker
        assert bt[ticker]['returnOnEquity'] == live[ticker]['returnOnEquity']
        assert bt[ticker]['totalAssets'] == live[ticker]['totalAssets']
    assert 'available_at' not in bt['AAA']
    assert 'ticker' not in bt['AAA']
```

- [ ] **Step 2: Run it and see the expected failure or pass**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_financials_pit_parity.py -q
```
Expected on a correct Task 1: `2 passed`. If Task 1 left `available_at` in the comprehension, the `pit=1` case fails with `TypeError: float() argument must be ... not 'Timestamp'` (or a set-inequality on `available_at`) — that is the regression this test exists to catch, and it is the only acceptable failure mode here.

- [ ] **Step 3: If it failed, fix Task 1's excluded-key tuple, then re-run**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_financials_pit_parity.py tests/strategies/test_financials_pit.py -q
```
Expected: `9 passed`.

- [ ] **Step 4: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add tests/strategies/test_financials_pit_parity.py && git commit -q -m "test(aux): PIT financials slice keeps engine.py's live aux['financials'] shape (Stream A task 2)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: A2 — gap-through-a-level fills at the bar's open

**Files:**
- Modify: `src/backtest/unified_backtest.py` — `_bar_exit` (currently 391-411); the `_bar_exit` call inside `simulate_trade`'s bar loop (currently 470-472); the `json.dumps({...})` literal written to `strategy_backtest_runs.config_json` (currently 1438-1470, after the `'double_touch'` key at 1459).
- Modify: `src/backtest/open_book.py` — `advance_open_book`'s bar unpack + `_bar_exit` call (currently 135-138).
- Test: `tests/backtest/test_gap_fill.py` (new)

**Interfaces:**
- Consumes: `os.environ['OPENCLAW_BT_GAP_FILL']` ∈ {unset, `level`, `open`}; the per-bar `bar` Series already held by both call sites.
- Produces:
  - `_bar_exit(direction: int, high: float, low: float, stop_loss: float, target_1: float, dt_priority: str, *, open_: Optional[float] = None) -> tuple[Optional[float], Optional[str]]` — keyword-only `open_`, so every existing positional and keyword call (`tests/backtest/test_open_book.py::TestBarExit` calls it both ways) is unchanged.
  - `config_json['gap_fill']: str` and `config_json['financials_pit']: bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_gap_fill.py
"""A2 (spec 2026-09-12 §1): a bar that OPENS beyond a bracket level fills at
that open, not at the untouched level. Behind OPENCLAW_BT_GAP_FILL=open;
unset/'level' is byte-identical to the pre-2026-09-12 engine.

tests/backtest/conftest.py is autouse and pins OPENCLAW_BT_FILL_MODEL=close
(legacy t+1); these tests call simulate_trade directly so that pin is inert,
but the frozen-value expectations below are written in t+1 geometry anyway.
"""
from __future__ import annotations

import contextlib
import os

import pandas as pd
import pytest

import backtest.unified_backtest as ub
from backtest.open_book import OpenTrade, advance_open_book

_FLAG_VARS = ('OPENCLAW_BT_GAP_FILL', 'OPENCLAW_BT_DOUBLE_TOUCH',
              'OPENCLAW_BACKTEST_SLIPPAGE', 'OPENCLAW_TRUE_MTM_MARKS')


@contextlib.contextmanager
def _clean_flags(**overrides):
    """Unset every flag this file's behaviour depends on, then apply overrides.
    Mirrors tests/backtest/test_adverse_slippage.py::_clean_flags, whose
    _FLAG_VARS predates OPENCLAW_BT_GAP_FILL."""
    saved = {k: os.environ.get(k) for k in _FLAG_VARS}
    try:
        for k in _FLAG_VARS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _ohlc(rows, start='2024-01-02'):
    """rows: (open, high, low, close) per bar."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame({'open': [r[0] for r in rows], 'high': [r[1] for r in rows],
                         'low': [r[2] for r in rows], 'close': [r[3] for r in rows]}, index=idx)


def _hl(rows, start='2024-01-02'):
    """Same bars WITHOUT an 'open' column — the shape several existing test
    helpers build (tests/backtest/test_adverse_slippage.py::_bars)."""
    return _ohlc(rows, start=start).drop(columns=['open'])


class TestBarExitGapRule:
    def test_long_stop_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=95.0) == (95.0, 'stop')

    def test_short_stop_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(-1, high=108.0, low=104.0, stop_loss=102.0, target_1=92.0,
                                dt_priority='stop', open_=106.0) == (106.0, 'stop')

    def test_long_target_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=112.0, low=110.0, stop_loss=95.0, target_1=108.0,
                                dt_priority='stop', open_=111.0) == (111.0, 'target')

    def test_short_target_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(-1, high=90.0, low=88.0, stop_loss=105.0, target_1=92.0,
                                dt_priority='stop', open_=89.0) == (89.0, 'target')

    def test_no_gap_is_unchanged_under_the_flag(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=101.0, low=94.0, stop_loss=95.0, target_1=108.0,
                                dt_priority='stop', open_=100.0) == (95.0, 'stop')
            assert ub._bar_exit(1, high=101.0, low=99.0, stop_loss=95.0, target_1=108.0,
                                dt_priority='stop', open_=100.0) == (None, None)

    def test_double_touch_priority_unchanged_when_the_open_is_inside(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            both = dict(high=110.0, low=90.0, stop_loss=95.0, target_1=108.0, open_=100.0)
            assert ub._bar_exit(1, dt_priority='stop', **both) == (95.0, 'stop')
            assert ub._bar_exit(1, dt_priority='target', **both) == (108.0, 'target')

    def test_flag_unset_ignores_the_open(self):
        with _clean_flags():
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=95.0) == (98.0, 'stop')

    def test_flag_level_ignores_the_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='level'):
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=95.0) == (98.0, 'stop')

    def test_open_none_ignores_the_flag(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=None) == (98.0, 'stop')


class TestSimulateTrade:
    BARS = _ohlc([(100.0, 100.5, 99.5, 100.2),      # entry bar (walk starts after it)
                  (95.0, 96.0, 94.0, 95.5)])         # gaps down through stop=98

    def test_gap_flag_fills_the_stop_at_the_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open', OPENCLAW_BACKTEST_SLIPPAGE='0'):
            out = ub.simulate_trade(self.BARS, self.BARS.index[0], +1, 100.0, 98.0, 108.0, 5)
        assert out['exit_reason'] == 'stop'
        assert out['exit_price'] == pytest.approx(95.0)
        assert out['pnl_pct'] == pytest.approx(-0.05)

    def test_flag_unset_reproduces_todays_frozen_values(self):
        """FROZEN: these are the numbers the engine produces today. If this
        test moves, the 'unset == byte-identical' contract is broken."""
        with _clean_flags():
            out = ub.simulate_trade(self.BARS, self.BARS.index[0], +1, 100.0, 98.0, 108.0, 5)
        assert out['exit_reason'] == 'stop'
        assert out['exit_price'] == pytest.approx(98.0)
        assert out['pnl_pct'] == pytest.approx(-0.02)
        assert out['holding_days'] == 1

    def test_missing_open_column_falls_back_to_level(self):
        bars = _hl([(100.0, 100.5, 99.5, 100.2), (95.0, 96.0, 94.0, 95.5)])
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 98.0, 108.0, 5)
        assert out['exit_reason'] == 'stop'
        assert out['exit_price'] == pytest.approx(98.0)


class TestOpenBookStepper:
    def test_stepper_passes_the_bar_open(self):
        bars = _ohlc([(100.0, 100.5, 99.5, 100.2), (95.0, 96.0, 94.0, 95.5)])
        by_ticker = {'AAA': bars}

        class _NoHook:
            exit_hook = False

        trade = OpenTrade(ticker='AAA', direction=1, entry_date=bars.index[0],
                          entry_price=100.0, entry_fill=100.0, stop_loss=98.0,
                          target_1=108.0, hold_cap=21, entry_regime='LOW_VOL',
                          signal_params={}, slippage=0.0, prev_mark=100.0)
        book = [trade]
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            closed = advance_open_book(book, bars.index[1], by_ticker, bars.loc[:bars.index[1]],
                                       {'state': 'LOW_VOL'}, {'options': {}}, _NoHook(),
                                       dt_priority='stop', counters={})
        assert len(closed) == 1
        assert closed[0]['exit_reason'] == 'stop'
        assert closed[0]['exit_price'] == pytest.approx(95.0)
```

- [ ] **Step 2: Run them and see the expected failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_gap_fill.py -q
```
Expected: `TypeError: _bar_exit() got an unexpected keyword argument 'open_'` on the `TestBarExitGapRule` and `TestSimulateTrade` gap cases; `test_flag_unset_reproduces_todays_frozen_values` and `test_missing_open_column_falls_back_to_level` PASS already (they lock current behaviour). Roughly `2 passed, 11 failed`.

- [ ] **Step 3: Add the gap rule to `_bar_exit`**

Replace `src/backtest/unified_backtest.py` lines 391-411 in full with:

```python
def _bar_exit(direction: int, high: float, low: float,
              stop_loss: float, target_1: float, dt_priority: str,
              *, open_: Optional[float] = None):
    """Intra-bar bracket decision shared by simulate_trade and the exit-hook
    open-book stepper. Returns (exit_level, reason) or (None, None).
    Long: target when high >= target_1, stop when low <= stop_loss; short
    mirrored. Double-touch resolves by dt_priority ('stop' default).

    Gap fill (spec 2026-09-12 §A2): a stop does NOT protect against an
    overnight gap — if the bar already OPENS beyond a level, that is the fill.
    Under OPENCLAW_BT_GAP_FILL='open': long `open_ <= stop_loss` fills 'stop'
    at open_ and `open_ >= target_1` fills 'target' at open_; short mirrored.
    This is checked BEFORE the touch/double-touch logic because the open is the
    bar's first price. Unset / 'level' (the default) ignores open_ entirely, as
    does any call passing open_=None (a bars frame with no 'open' column) —
    both are byte-identical to the pre-2026-09-12 engine. The env read is
    guarded behind `open_ is not None` so legacy callers pay nothing.
    """
    if open_ is not None and os.environ.get('OPENCLAW_BT_GAP_FILL', 'level') == 'open':
        o = float(open_)
        if direction > 0:
            if o <= stop_loss:
                return o, 'stop'
            if o >= target_1:
                return o, 'target'
        else:
            if o >= stop_loss:
                return o, 'stop'
            if o <= target_1:
                return o, 'target'
    if direction > 0:
        t_hit = high >= target_1
        s_hit = low <= stop_loss
    else:
        t_hit = low <= target_1
        s_hit = high >= stop_loss
    if t_hit and s_hit:
        if dt_priority == 'target':
            return float(target_1), 'target'
        return float(stop_loss), 'stop'
    if t_hit:
        return float(target_1), 'target'
    if s_hit:
        return float(stop_loss), 'stop'
    return None, None
```

- [ ] **Step 4: Both call sites pass the bar open**

In `simulate_trade`'s loop, replace these two lines (currently 471-472):

```python
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        exit_level, reason = _bar_exit(direction, high, low, stop_loss, target_1, _dt_priority)
```

with:

```python
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        # `open` is absent from some synthetic/legacy bars frames; None there
        # means _bar_exit ignores the gap rule (spec §A2 fallback).
        _o = bar.get('open')
        _open = float(_o) if _o is not None and pd.notna(_o) else None
        exit_level, reason = _bar_exit(direction, high, low, stop_loss, target_1, _dt_priority,
                                       open_=_open)
```

In `src/backtest/open_book.py::advance_open_book`, replace these four lines (currently 135-138):

```python
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        t.holding_days += 1
        # 1. intra-bar bracket
        exit_level, reason = _bar_exit(t.direction, high, low, t.stop_loss, t.target_1, dt_priority)
```

with:

```python
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        # `open` is absent from some synthetic bars frames; None there means
        # _bar_exit ignores the gap rule (spec §A2 fallback).
        _o = bar.get('open')
        _open = float(_o) if _o is not None and pd.notna(_o) else None
        t.holding_days += 1
        # 1. intra-bar bracket
        exit_level, reason = _bar_exit(t.direction, high, low, t.stop_loss, t.target_1, dt_priority,
                                       open_=_open)
```

- [ ] **Step 5: Record the provenance in `config_json`**

In `src/backtest/unified_backtest.py`, in the `json.dumps({...})` literal, insert immediately after the `'double_touch': os.environ.get('OPENCLAW_BT_DOUBLE_TOUCH', 'stop'),` line (currently 1459):

```python
                # Gap-fill provenance (2026-09-12 §A2): 'level' = a bracket
                # touch returns the LEVEL (legacy); 'open' = a bar that opens
                # beyond the level fills at that open. Read by
                # scripts/pit_gap_flip_gate.py gate G1.
                'gap_fill': os.environ.get('OPENCLAW_BT_GAP_FILL', 'level'),
                # Fundamentals point-in-time provenance (2026-09-12 §A1). Read
                # straight from the env at persist time rather than importing
                # aux_data_loader — no new cross-module dependency here.
                'financials_pit': os.environ.get('OPENCLAW_FINANCIALS_PIT', '0') == '1',
```

- [ ] **Step 6: Run and PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_gap_fill.py tests/backtest/test_open_book.py tests/backtest/test_adverse_slippage.py -q
```
Expected: `13 passed` from the new file, and every existing `test_open_book.py` (its `TestBarExit` class calls `_bar_exit` positionally and by keyword without `open_`) and `test_adverse_slippage.py` (its `_bars()` helper builds frames with no `open` column) test still green — 0 failures.

- [ ] **Step 7: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/backtest/unified_backtest.py src/backtest/open_book.py tests/backtest/test_gap_fill.py && git commit -q -m "feat(backtest): gap-through-a-level fills at the bar open behind OPENCLAW_BT_GAP_FILL (Stream A task 3)

_bar_exit gains keyword-only open_; simulate_trade and advance_open_book pass
bar.get('open') (None when the frame has no open column => legacy level fill).
config_json records gap_fill and financials_pit. Unset flag is byte-identical,
locked by a frozen-values test. Spec A2.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: A2 — `scripts/measure_gap_fill_impact.py`

**Files:**
- Create: `scripts/measure_gap_fill_impact.py`
- Test: `tests/scripts/test_measure_gap_fill_impact.py` (new)

**Interfaces:**
- Consumes: `strategy_backtest_trades` joined to `strategy_backtest_runs` on `run_id` where `primary_window = TRUE` — columns verified against the INSERT at `src/backtest/unified_backtest.py:1567-1573`: `run_id, trade_seq, strategy_id, ticker, direction, entry_date, entry_price, exit_date, exit_price, exit_reason, pnl_pct, holding_days, entry_regime, signal_stop, signal_target`. `data/master/prices.parquet` via `pq.read_table(columns=['ticker','date','open'], read_dictionary=['ticker','date'], filters=[('ticker','in',…)])` — the same read shape as `unified_backtest.load_prices_panels` (`unified_backtest.py:318-326`), where `date` is a STRING dictionary column.
- Produces:
  - `reprice_stop_exit(direction: str, entry_price: float, stop: float, bar_open: float) -> Optional[float]` — the gapped gross pnl_pct, or `None` when the bar did not gap through the stop.
  - CLI: `python3 scripts/measure_gap_fill_impact.py [--limit N] [--strategy SID] [--min-trades N] [--env-file PATH]`, printing one line per strategy.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_measure_gap_fill_impact.py
"""The re-pricing arithmetic of scripts/measure_gap_fill_impact.py. Pure
function only — the script's DB and parquet reads are never exercised here."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        'mgfi', ROOT / 'scripts' / 'measure_gap_fill_impact.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_long_gap_below_stop_is_repriced_at_the_open():
    m = _mod()
    assert m.reprice_stop_exit('long', 100.0, 98.0, 95.0) == pytest.approx(-0.05)


def test_long_open_above_stop_is_not_a_gap():
    m = _mod()
    assert m.reprice_stop_exit('long', 100.0, 98.0, 99.0) is None
    assert m.reprice_stop_exit('long', 100.0, 98.0, 98.0) is None


def test_short_gap_above_stop_is_repriced_at_the_open():
    m = _mod()
    assert m.reprice_stop_exit('short', 100.0, 102.0, 106.0) == pytest.approx(-0.06)


def test_short_open_below_stop_is_not_a_gap():
    m = _mod()
    assert m.reprice_stop_exit('short', 100.0, 102.0, 101.0) is None


def test_non_finite_and_zero_entry_are_rejected():
    m = _mod()
    assert m.reprice_stop_exit('long', 0.0, 98.0, 95.0) is None
    assert m.reprice_stop_exit('long', 100.0, float('nan'), 95.0) is None
    assert m.reprice_stop_exit('long', 100.0, 98.0, float('nan')) is None
    assert m.reprice_stop_exit('sideways', 100.0, 98.0, 95.0) is None
```

- [ ] **Step 2: Run it and see the expected failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/scripts/test_measure_gap_fill_impact.py -q
```
Expected: `FileNotFoundError: [Errno 2] No such file or directory: '.../scripts/measure_gap_fill_impact.py'` — 5 errors.

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""measure_gap_fill_impact.py — read-only sizing of the A2 gap-fill change.

For every STORED primary backtest run it takes the trades whose exit_reason is
'stop', looks up the exit bar's OPEN in prices.parquet, and re-prices the ones
whose open was already beyond the signal stop. Prints, per strategy, the mean
pnl_pct delta over the gapped trades, the same mean spread over all that
strategy's stop exits, and the counts.

READ-ONLY: no INSERT, no UPDATE, no env flip, no file written.

Comparability (state this whenever you quote the number): the stored
strategy_backtest_trades.pnl_pct is NET of the adverse per-fill slippage that
simulate_trade applies to both legs, while this script re-prices at the RAW
level -> RAW open. What it reports is therefore the GROSS level-vs-open delta;
the true net delta differs by the (unchanged) slippage fraction applied to a
slightly different exit level, which is second order. It is a scoping estimate,
never a backtest result.

Memory (2-core / 8 GB, no swap): prices are read with a pyarrow ticker filter
and a three-column projection, never the whole master. Keep --limit modest;
the default 40 strategies keeps the resident set well under 1 GB.

Usage:
  python3 scripts/measure_gap_fill_impact.py
  python3 scripts/measure_gap_fill_impact.py --strategy S_beta_spy
  python3 scripts/measure_gap_fill_impact.py --limit 80 --min-trades 50
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'


def reprice_stop_exit(direction, entry_price, stop, bar_open):
    """Gross pnl_pct of a stop exit filled at `bar_open`, or None when the bar
    did not gap through `stop` (or an input is unusable).

    long : a gap is open <  stop  -> fill at open, pnl = (open - entry)/entry
    short: a gap is open >  stop  -> fill at open, pnl = (entry - open)/entry
    """
    try:
        entry_price = float(entry_price)
        stop = float(stop)
        bar_open = float(bar_open)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(entry_price) and math.isfinite(stop) and math.isfinite(bar_open)):
        return None
    if entry_price <= 0.0:
        return None
    d = str(direction or '').lower()
    if d == 'long':
        if bar_open >= stop:
            return None
        return (bar_open - entry_price) / entry_price
    if d == 'short':
        if bar_open <= stop:
            return None
        return (entry_price - bar_open) / entry_price
    return None


def _postgres_uri(env_file: str):
    uri = os.environ.get('POSTGRES_URI')
    if uri:
        return uri
    try:
        m = re.search(r'^POSTGRES_URI=(.*)$', open(env_file).read(), re.M)
    except OSError:
        return None
    return m.group(1).strip().strip('"') if m else None


def _fetch_stop_trades(uri, strategy, limit):
    import psycopg2
    sql = """
        SELECT t.strategy_id, t.ticker, t.direction, t.entry_price,
               t.exit_date, t.signal_stop, t.pnl_pct
          FROM strategy_backtest_trades t
          JOIN strategy_backtest_runs r ON r.run_id = t.run_id
         WHERE r.primary_window = TRUE
           AND t.exit_reason = 'stop'
           AND t.signal_stop IS NOT NULL
           AND t.entry_price IS NOT NULL
           AND t.pnl_pct IS NOT NULL
    """
    params = []
    if strategy:
        sql += ' AND t.strategy_id = %s'
        params.append(strategy)
    with psycopg2.connect(uri) as c, c.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not strategy and limit:
        keep = sorted({r[0] for r in rows})[:limit]
        keep_set = set(keep)
        rows = [r for r in rows if r[0] in keep_set]
    return rows


def _open_map(tickers):
    """{(ticker, 'YYYY-MM-DD'): open} for the requested tickers only."""
    import pyarrow.parquet as pq
    if not tickers:
        return {}
    tbl = pq.read_table(str(PRICES_PARQUET), columns=['ticker', 'date', 'open'],
                        read_dictionary=['ticker', 'date'],
                        filters=[('ticker', 'in', sorted(tickers))])
    df = tbl.to_pandas()
    del tbl
    # `date` is a STRING dictionary column on disk (that is why the dictionary
    # read works); the DB hands back datetime.date, so both sides key on ISO.
    df['ticker'] = df['ticker'].astype(str)
    df['date'] = df['date'].astype(str).str.slice(0, 10)
    return dict(zip(zip(df['ticker'], df['date']), df['open'].astype(float)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=40,
                    help='max distinct strategies to load (alphabetical); 0 = all')
    ap.add_argument('--strategy', default=None)
    ap.add_argument('--min-trades', type=int, default=20,
                    help='skip strategies with fewer stop exits than this')
    ap.add_argument('--env-file', default=str(ROOT / '.env'))
    a = ap.parse_args()

    uri = _postgres_uri(a.env_file)
    if not uri:
        print('POSTGRES_URI not found', file=sys.stderr)
        return 2
    rows = _fetch_stop_trades(uri, a.strategy, a.limit)
    if not rows:
        print('no stored stop exits matched')
        return 0
    om = _open_map({r[1] for r in rows})

    per: dict = {}
    unmatched = 0
    for sid, ticker, direction, entry_price, exit_date, stop, pnl in rows:
        slot = per.setdefault(sid, {'stops': 0, 'gapped': 0, 'deltas': []})
        slot['stops'] += 1
        key = (str(ticker), exit_date.isoformat() if hasattr(exit_date, 'isoformat') else str(exit_date)[:10])
        bar_open = om.get(key)
        if bar_open is None:
            unmatched += 1
            continue
        gapped = reprice_stop_exit(direction, entry_price, stop, bar_open)
        if gapped is None:
            continue
        slot['gapped'] += 1
        slot['deltas'].append(gapped - float(pnl))

    print(f'{"strategy":<46} {"stops":>6} {"gapped":>7} {"gap%":>6} '
          f'{"mean_d_gapped%":>15} {"mean_d_all%":>12}')
    ranked = sorted(per.items(), key=lambda kv: (sum(kv[1]['deltas']) / kv[1]['stops']) if kv[1]['stops'] else 0.0)
    for sid, s in ranked:
        if s['stops'] < a.min_trades:
            continue
        d_gapped = (sum(s['deltas']) / len(s['deltas']) * 100.0) if s['deltas'] else 0.0
        d_all = (sum(s['deltas']) / s['stops'] * 100.0) if s['stops'] else 0.0
        print(f'{sid:<46} {s["stops"]:>6} {s["gapped"]:>7} '
              f'{(100.0 * s["gapped"] / s["stops"]):>5.1f}% {d_gapped:>+15.3f} {d_all:>+12.3f}')
    tot_stops = sum(s['stops'] for s in per.values())
    tot_gapped = sum(s['gapped'] for s in per.values())
    tot_delta = sum(sum(s['deltas']) for s in per.values())
    print(f'\nTOTAL strategies={len(per)} stops={tot_stops} gapped={tot_gapped} '
          f'({(100.0 * tot_gapped / tot_stops if tot_stops else 0.0):.1f}%) '
          f'mean_delta_all={(100.0 * tot_delta / tot_stops if tot_stops else 0.0):+.3f}% '
          f'unmatched_bars={unmatched}')
    print('NOTE: gross level-vs-open delta; stored pnl_pct is net of adverse '
          'fills, so this is a scoping estimate, not a backtest result.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run and PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/scripts/test_measure_gap_fill_impact.py -q
```
Expected: `5 passed`.

- [ ] **Step 5: Confirm the CLI parses without touching the DB or the master**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 scripts/measure_gap_fill_impact.py --help | head -5
```
Expected: the argparse usage line listing `--limit`, `--strategy`, `--min-trades`, `--env-file`. Do NOT run the script for real while the fleet backtest is running — that is Task 8's job.

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add scripts/measure_gap_fill_impact.py tests/scripts/test_measure_gap_fill_impact.py && git commit -q -m "feat(scripts): measure_gap_fill_impact — read-only per-strategy gap-fill delta (Stream A task 4)

Re-prices stored primary-run stop exits whose exit-bar open was beyond the
signal stop, using a ticker-filtered three-column prices.parquet read.
Spec A2 measurement script.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: A3 — exit-reason census + cost drag in `config_json`

**Files:**
- Modify: `src/backtest/unified_backtest.py` — add `import statistics` to the stdlib import block (currently 35-48, alphabetically between `import os` at 41 and `import subprocess` at 42; note the module currently imports `statistics` locally at line 1676); add two pure functions after `_bar_exit`; add two keys to the `json.dumps({...})` literal.
- Test: `tests/backtest/test_exit_census.py` (new)

**Interfaces:**
- Consumes: the trade dicts built at `unified_backtest.py:1045-1059` and `open_book._close` (keys `ticker, direction, entry_date, entry_price, exit_date, exit_price, exit_reason, holding_days, pnl_pct, entry_regime, signal_stop, signal_target, daily_marks`); the cost resolution `cost_bps_by_ticker.get(ticker, slippage_bps)` used at `unified_backtest.py:1017-1018`; `_slippage_bps` and `_sim_kwargs['cost_bps_by_ticker']` already in scope at the persist block.
- Produces:
  - `exit_reason_census(trades: list) -> dict` → `{reason: {'n': int, 'mean_pnl_pct': float|None, 'median_hold_days': float|None}}`
  - `cost_drag_bps(trades: list, *, cost_bps_by_ticker: Optional[dict] = None, flat_bps: float = 0.0) -> Optional[float]`
  - `config_json['exit_reasons']`, `config_json['cost_drag_bps']`

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_exit_census.py
"""A3 (spec 2026-09-12 §1): every run records what its exits actually were and
what the modelled cost took out. Pure functions — no DB, no parquet."""
from __future__ import annotations

import pytest

import backtest.unified_backtest as ub

THREE = [
    {'ticker': 'AAA', 'exit_reason': 'stop',   'pnl_pct': -0.020, 'holding_days': 3},
    {'ticker': 'BBB', 'exit_reason': 'target', 'pnl_pct': +0.050, 'holding_days': 9},
    {'ticker': 'AAA', 'exit_reason': 'stop',   'pnl_pct': -0.040, 'holding_days': 5},
]


class TestCensus:
    def test_three_trade_census(self):
        out = ub.exit_reason_census(THREE)
        assert set(out) == {'stop', 'target'}
        assert out['stop'] == {'n': 2, 'mean_pnl_pct': pytest.approx(-0.030),
                               'median_hold_days': pytest.approx(4.0)}
        assert out['target'] == {'n': 1, 'mean_pnl_pct': pytest.approx(0.050),
                                 'median_hold_days': pytest.approx(9.0)}

    def test_hook_reasons_keep_their_prefix(self):
        out = ub.exit_reason_census(
            THREE + [{'ticker': 'CCC', 'exit_reason': 'strategy_exit:pair_decohered',
                      'pnl_pct': 0.01, 'holding_days': 2}])
        assert out['strategy_exit:pair_decohered']['n'] == 1

    def test_non_finite_pnl_counts_in_n_but_not_in_the_mean(self):
        out = ub.exit_reason_census(
            THREE + [{'ticker': 'DDD', 'exit_reason': 'stop',
                      'pnl_pct': float('nan'), 'holding_days': 1}])
        assert out['stop']['n'] == 3
        assert out['stop']['mean_pnl_pct'] == pytest.approx(-0.030)

    def test_missing_reason_is_unknown_and_empty_is_empty(self):
        assert ub.exit_reason_census([{'ticker': 'AAA', 'pnl_pct': 0.0, 'holding_days': 1}]) \
            == {'unknown': {'n': 1, 'mean_pnl_pct': 0.0, 'median_hold_days': 1.0}}
        assert ub.exit_reason_census([]) == {}
        assert ub.exit_reason_census(None) == {}


class TestCostDrag:
    def test_flat_bps_over_three_trades(self):
        # cost_i = 2 * 10 / 1e4 = 0.002 each => sum 0.006
        # gross_i = pnl_i + 0.002 => -0.018, +0.052, -0.038 => sum|.| = 0.108
        got = ub.cost_drag_bps(THREE, flat_bps=10.0)
        assert got == pytest.approx(1e4 * 0.006 / 0.108, rel=1e-9)

    def test_per_ticker_map_overrides_the_flat_fallback(self):
        got = ub.cost_drag_bps(THREE, cost_bps_by_ticker={'AAA': 30.0}, flat_bps=10.0)
        # AAA 0.006 + AAA 0.006 + BBB 0.002 = 0.014
        # gross: -0.014, +0.052, -0.034 => 0.100
        assert got == pytest.approx(1e4 * 0.014 / 0.100, rel=1e-9)

    def test_zero_cost_is_zero_not_none(self):
        assert ub.cost_drag_bps(THREE, flat_bps=0.0) == pytest.approx(0.0)

    def test_no_finite_trades_is_none(self):
        assert ub.cost_drag_bps([], flat_bps=10.0) is None
        assert ub.cost_drag_bps(None, flat_bps=10.0) is None
        assert ub.cost_drag_bps([{'ticker': 'AAA', 'pnl_pct': float('nan')}], flat_bps=10.0) is None

    def test_zero_gross_denominator_is_none_not_a_zero_division(self):
        # pnl exactly cancels the cost => gross 0 for every trade
        trades = [{'ticker': 'AAA', 'pnl_pct': -0.002, 'holding_days': 1}]
        assert ub.cost_drag_bps(trades, flat_bps=10.0) is None
```

- [ ] **Step 2: Run them and see the expected failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_exit_census.py -q
```
Expected: `AttributeError: module 'backtest.unified_backtest' has no attribute 'exit_reason_census'` — 9 failed.

- [ ] **Step 3: Add `import statistics`**

In `src/backtest/unified_backtest.py`, insert between `import os` (currently 41) and `import subprocess` (currently 42):

```python
import statistics
```

(The function-local `import statistics` at line 1676 stays; it is harmless and out of scope.)

- [ ] **Step 4: Add the two pure functions**

Insert immediately after `_bar_exit`'s closing `return None, None` and before `def simulate_trade(` in `src/backtest/unified_backtest.py`:

```python
def exit_reason_census(trades) -> dict:
    """{exit_reason: {n, mean_pnl_pct, median_hold_days}} over `trades`
    (spec 2026-09-12 §A3). Never changes a Sharpe — provenance only.

    Hook exits keep their 'strategy_exit:<reason>' prefix on purpose: the
    prefix is exactly how "65 % of exits were pair_decohered" becomes readable
    from a stored run instead of a rolled journal.

    Non-finite pnl_pct / holding_days are dropped from the aggregates the same
    way aggregate_metrics and tail_stats drop them (2026-06-15 BRK-B: one
    corrupt price bar must not NaN a whole stat) — the trade still counts in n.
    A reason with no finite observation reports None, never a fabricated 0.0.
    """
    buckets: dict = {}
    for t in trades or []:
        reason = str(t.get('exit_reason') or 'unknown')
        slot = buckets.setdefault(reason, {'n': 0, 'pnl': [], 'hold': []})
        slot['n'] += 1
        p = t.get('pnl_pct')
        try:
            if p is not None and math.isfinite(float(p)):
                slot['pnl'].append(float(p))
        except (TypeError, ValueError):
            pass
        h = t.get('holding_days')
        try:
            if h is not None and math.isfinite(float(h)):
                slot['hold'].append(float(h))
        except (TypeError, ValueError):
            pass
    out: dict = {}
    for reason, slot in buckets.items():
        out[reason] = {
            'n': slot['n'],
            'mean_pnl_pct': (sum(slot['pnl']) / len(slot['pnl'])) if slot['pnl'] else None,
            'median_hold_days': statistics.median(slot['hold']) if slot['hold'] else None,
        }
    return out


def cost_drag_bps(trades, *, cost_bps_by_ticker: Optional[dict] = None,
                  flat_bps: float = 0.0) -> Optional[float]:
    """Modelled round-trip cost as basis points of gross P&L (spec §A3):

        1e4 * Σ cost_i / Σ |gross_i|

    cost_i = 2 * bps_i / 1e4 — one adverse entry fill plus one adverse exit
    fill, with bps_i resolved exactly as _per_bar_simulate resolves it
    (cost_bps_by_ticker.get(ticker, flat_bps)). gross_i = pnl_pct_i + cost_i,
    a first-order un-netting: pnl_pct compounds and the two cost legs do not
    net out exactly, which is immaterial at ≤ 30 bps and is why this is
    provenance and never a gate input.

    Returns None (never 0.0, never ZeroDivisionError) when no trade carries a
    finite pnl_pct or when Σ|gross| is 0.
    """
    num = 0.0
    den = 0.0
    seen = False
    for t in trades or []:
        p = t.get('pnl_pct')
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(p):
            continue
        bps = float(flat_bps)
        if cost_bps_by_ticker:
            bps = float(cost_bps_by_ticker.get(t.get('ticker'), flat_bps))
        cost = 2.0 * bps / 1e4
        num += cost
        den += abs(p + cost)
        seen = True
    if not seen or den <= 0.0:
        return None
    return 1e4 * num / den
```

- [ ] **Step 5: Wire them into `config_json`**

In the `json.dumps({...})` literal, insert immediately after the `'financials_pit': ...` line added in Task 3:

```python
                # Exit-reason census + modelled cost drag (2026-09-12 §A3).
                # Provenance only — never a Sharpe, never a gate input. Hook
                # exits appear as 'strategy_exit:<reason>'.
                'exit_reasons': exit_reason_census(trades),
                'cost_drag_bps': cost_drag_bps(
                    trades,
                    cost_bps_by_ticker=_sim_kwargs.get('cost_bps_by_ticker'),
                    flat_bps=_slippage_bps),
```

Both `trades` and `_slippage_bps` are already bound in that scope (`trades = sim['trades']` at line ~1394; `_slippage_bps` at line 1319).

- [ ] **Step 6: Run and PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_exit_census.py tests/backtest/test_gap_fill.py -q
```
Expected: `22 passed`.

- [ ] **Step 7: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/backtest/unified_backtest.py tests/backtest/test_exit_census.py && git commit -q -m "feat(backtest): exit_reasons census + cost_drag_bps in the run's config_json (Stream A task 5)

Two pure functions (NaN-guarded, None on no evidence) persisted alongside
target_mode/cost_model/gap_fill. Provenance only, never a gate input. Spec A3.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Hygiene — stale registry row, archived→candidate revival, two false manifest reasons

**Files:**
- Modify: `src/strategies/registry.py` — delete the `S_fomc_presell_spy_long` entry, currently line 148 inside `_IMPL_MAP`, exact text:
  `    'S_fomc_presell_spy_long': ('strategies.implementations.S_fomc_presell_spy_long', 'FomcPresellSpyLong'),`
  (`sed -i /…/d` exits 0 on no match, so Step 5's `assert` — not the sed — is what proves the row is gone.)
- Modify: `src/strategies/lifecycle.py` — add `(StrategyState.ARCHIVED, StrategyState.CANDIDATE)` to `VALID_TRANSITIONS` (currently 65-85).
- Modify: `src/strategies/manifest.json` — the two `history[].reason` strings currently at lines 1962 and 2030 (entries `S_TR04_zarattini_intraday_spy` at 1891 and `S_TR06_baltussen_eod_reversal` at 1968), plus each entry's `state` / `state_since` / appended history event.
- Test: `tests/strategies/test_lifecycle_revival.py` (new)

**Interfaces:**
- Consumes: `strategies.lifecycle.LifecycleStateMachine.from_manifest(path) -> LifecycleStateMachine`; `.transition(strategy_id, to_state: StrategyState, actor: str, reason: str, metadata: dict|None) -> StrategyRecord`; `.save_manifest(path)`; `StrategyState.ARCHIVED` / `.CANDIDATE`.
- Produces: `VALID_TRANSITIONS[(StrategyState.ARCHIVED, StrategyState.CANDIDATE)]` — the revival edge.

Verified facts this task rests on:
- `ls src/strategies/implementations | grep -i fomc` returns nothing and `find . -name '*fomc*' -not -path './.git/*'` returns nothing — the source file AND its `.pyc` are already gone; only the registry row survives.
- `str04_zarattini_intraday_spy.py` and `str06_baltussen_eod_reversal.py` both exist, so `manifest_canonical_file_consistency` stays green after the revival.
- `data/master/prices_30m.parquet` exists and was refreshed 2026-09-11 — the shelving reason is false.
- BUT the real blocker is different: `str04` reads `market_data['spy_30m_bars']` (line 71) and `str06` reads `market_data['intraday_30m_bars']` (line 64) — a `market_data` kwarg the backtest never passes (it passes `aux_data`). Neither reads `aux_data['prices_30m']`. The corrected reason must say this, or the research lane re-shelves them next quarter for the same wrong reason.
- `_ACTIVE_KEEP_STATES` (lifecycle.py:695) already contains `CANDIDATE`, and `system_checks/checks/strategies.py:110-113` skips candidates in the `_IMPL_MAP` coverage check, so neither strategy needs a registry row to sit at candidate.
- **Manifest writing is not a free-hand JSON edit.** `manifest.json` is written by `src/strategies/_manifest_lock.py::write_atomic` — `json.dumps(payload, indent=2)` with **no trailing newline** — under a cross-process lock (`with_manifest_lock`) that exists because JS writers (`saturday_brain.js`, the finisher, approvals) mutate the same file. A bare `read_text()` / `write_text()` from this task would race them and would add a spurious trailing newline. Every write below therefore goes through `with_manifest_lock` or `LifecycleStateMachine.save_manifest` (which wraps it and additionally merges with disk, so a concurrent writer's new strategy is not lost). Round-trip stability is verified in Step 6.
- **`LifecycleStateMachine.to_dict()` (lifecycle.py:819-857) emits a FIXED key set** — `state`, `state_since`, `metadata`, `history`, optional `eligible_regimes`, `instrument_class` — so any other top-level entry key is silently dropped on `save_manifest`. Today that costs nothing (verified: 0 of the manifest's entries carry a non-schema top-level key and 0 are quarantined), but it means the `backtest_quarantine` flag set in Step 7 would be wiped by the next unrelated `save_manifest` (e.g. `auto_demote_negative_sharpe`). The flag must still live at the top level because `refresh_backtests_resumable.js:119` reads `e.backtest_quarantine` there, so Step 7 also mirrors the fact into `metadata` (which does round-trip) and Task 9 records the `to_dict` gap as OWED. Do not "fix" it by moving the flag into `metadata` — the driver would stop seeing it.
- `_sync_registry_demotion` (lifecycle.py:913) is invoked only from `auto_demote_negative_sharpe` (line 969), never from `transition` — the revival touches no registry row.
- `scripts/refresh_backtests_resumable.js` `RANK = { live: 0, candidate: 1, staging: 2 }` (line 105) means candidates DO consume nightly fleet slots, and `backtest_quarantine` (line 119) removes a strategy from that queue WITHOUT changing its lifecycle state. Because a 0-trade run carries no gating information either way, the revival sets `backtest_quarantine` naming the real wiring gap.

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_lifecycle_revival.py
"""archived -> candidate: reviving a strategy whose blocking data gap closed
(spec 2026-09-12 §A4 hygiene). Operates on a tmp_path manifest copy only."""
from __future__ import annotations

import json

import pytest

from strategies.lifecycle import LifecycleStateMachine, LifecycleError, StrategyState, VALID_TRANSITIONS


def _manifest(tmp_path):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {
        'S_revive_me': {
            'state': 'archived',
            'state_since': '2026-04-30T23:37:01.649Z',
            'metadata': {'canonical_file': 'str04_zarattini_intraday_spy.py',
                         'class': 'ZarattiniIntradaySpy'},
            'history': [],
            'instrument_class': 'equity',
        },
    }}))
    return p


def test_archived_to_candidate_is_a_valid_transition():
    assert (StrategyState.ARCHIVED, StrategyState.CANDIDATE) in VALID_TRANSITIONS


def test_revival_moves_state_and_appends_history(tmp_path, monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)   # _persist_lifecycle_event no-ops
    p = _manifest(tmp_path)
    lsm = LifecycleStateMachine.from_manifest(str(p))
    rec = lsm.transition('S_revive_me', StrategyState.CANDIDATE,
                         actor='manual:operator', reason='data gap closed')
    assert rec.state is StrategyState.CANDIDATE
    assert rec.history[-1].from_state == 'archived'
    assert rec.history[-1].to_state == 'candidate'
    lsm.save_manifest(str(p))
    assert json.loads(p.read_text())['strategies']['S_revive_me']['state'] == 'candidate'


def test_archived_to_live_is_still_refused(tmp_path, monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    lsm = LifecycleStateMachine.from_manifest(str(_manifest(tmp_path)))
    with pytest.raises(LifecycleError):
        lsm.transition('S_revive_me', StrategyState.LIVE, actor='test')
```

- [ ] **Step 2: Run it and see the expected failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_lifecycle_revival.py -q
```
Expected: `test_archived_to_candidate_is_a_valid_transition` fails on the `assert ... in VALID_TRANSITIONS`; `test_revival_moves_state_and_appends_history` fails with `LifecycleError: No valid path from 'archived' to 'candidate'`. `test_archived_to_live_is_still_refused` passes. `1 passed, 2 failed`.

- [ ] **Step 3: Add the revival edge**

In `src/strategies/lifecycle.py`, insert immediately before the closing `}` of `VALID_TRANSITIONS` (after the `(StrategyState.DEPRECATED, StrategyState.ARCHIVED)` line, currently 84):

```python
    # Revival (2026-09-12, spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md
    # §A4 hygiene). ARCHIVED is normally terminal, but a strategy shelved
    # because a data source was believed dead has to be able to come back when
    # the source is alive again — otherwise the only route is a hand-edited
    # manifest that bypasses this state machine entirely. It lands at CANDIDATE
    # (never LIVE): every promotion guard downstream is unchanged.
    (StrategyState.ARCHIVED,   StrategyState.CANDIDATE):   "revive: the data gap that caused archival is closed",
```

- [ ] **Step 4: Run and PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/strategies/test_lifecycle_revival.py -q
```
Expected: `3 passed`.

- [ ] **Step 5: Delete the stale registry row and confirm nothing else references it**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && \
  sed -i "/'S_fomc_presell_spy_long': ('strategies.implementations.S_fomc_presell_spy_long', 'FomcPresellSpyLong'),/d" src/strategies/registry.py && \
  grep -rn "S_fomc_presell_spy_long" --include=*.py --include=*.json --include=*.js . | grep -v '^./.git/' ; \
  find . -name '*fomc*' -not -path './.git/*' ; \
  python3 -c "import sys; sys.path.insert(0,'src'); from strategies.registry import _IMPL_MAP; print('rows', len(_IMPL_MAP)); assert 'S_fomc_presell_spy_long' not in _IMPL_MAP; print('OK')"
```
Expected: both `grep` and `find` print nothing (the `.pyc` was already absent — this step VERIFIES that, it does not delete anything), then `rows <N>` and `OK`.

- [ ] **Step 6: Correct the two false manifest reasons**

The two history events are dated 2026-04-30 and record what the operator believed then, so the original sentence is preserved and the correction is prefixed — the manifest must not assert a falsehood, and it must not silently rewrite what an operator said.

First prove the file is byte-stable under the canonical writer, so this edit produces a two-line diff and not a whole-file reformat:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -c "
import json, pathlib
s = pathlib.Path('src/strategies/manifest.json').read_text()
print('byte-stable under write_atomic:', json.dumps(json.loads(s), indent=2) == s)"
```
Expected: `byte-stable under write_atomic: True`. If it prints `False`, STOP — a concurrent writer has changed the format and this step would reformat the whole file; report instead of proceeding.

Then correct the two reasons through the cross-process lock (JS writers mutate this file too):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 - <<'PY'
import sys
sys.path.insert(0, 'src')
from strategies._manifest_lock import with_manifest_lock

MP = 'src/strategies/manifest.json'
OLD = ("Requires prices_30m which is no longer collected daily; "
       "shelved for future revival when intraday tier is enabled")
NEW = ("[CORRECTED 2026-09-13] Original reason (2026-04-30): 'Requires prices_30m "
       "which is no longer collected daily' — FALSE: data/master/prices_30m.parquet "
       "is a live master (refreshed 2026-09-11). The real blocker is different: this "
       "strategy reads intraday bars from the market_data kwarg, which the backtest "
       "never passes, and reads no aux_data['prices_30m']. That wiring is owed before "
       "any backtest of it can produce trades.")
counted = {'n': 0}


def _fix(disk):
    for sid in ('S_TR04_zarattini_intraday_spy', 'S_TR06_baltussen_eod_reversal'):
        for ev in disk['strategies'][sid].get('history', []):
            if ev.get('reason') == OLD:
                ev['reason'] = NEW
                counted['n'] += 1
    return disk


with_manifest_lock(MP, _fix, actor='qd-stream-a:reason-correction')
assert counted['n'] == 2, f"expected 2 corrections, made {counted['n']}"
print('corrected', counted['n'])
PY
grep -c "no longer collected daily; shelved" src/strategies/manifest.json
git -C /root/openclaw/.claude/worktrees/qd-adoptions diff --stat src/strategies/manifest.json
```
Expected: `corrected 2`, then `0` from the grep (`grep -c` on no match exits 1 and prints `0` — that is the pass condition; the phrase now survives only inside the quoted `[CORRECTED …]` text, which does not match the trailing `; shelved`), then a `diff --stat` showing **2 insertions, 2 deletions**. Any larger diff means the file was reformatted — `git checkout src/strategies/manifest.json` and report.

- [ ] **Step 7: Re-queue both strategies as candidates through the state machine**

Two writes, both lock-protected. The first goes through `save_manifest` (state machine semantics + merge-with-disk); the second sets the top-level `backtest_quarantine` flag through `with_manifest_lock`, because `to_dict()` would drop it. The same fact is mirrored into `metadata`, which does round-trip, so the truth survives a later `save_manifest` even if the flag does not.

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && POSTGRES_URI= python3 - <<'PY'
import json, pathlib, sys
sys.path.insert(0, 'src')
from strategies.lifecycle import LifecycleStateMachine, StrategyState
from strategies._manifest_lock import with_manifest_lock

MP = 'src/strategies/manifest.json'
SIDS = ('S_TR04_zarattini_intraday_spy', 'S_TR06_baltussen_eod_reversal')
QUAR = ("revived 2026-09-13 but reads market_data['*_30m_bars'], which the backtest "
        "never passes — 0-trade runs until the intraday aux wiring lands")
REASON = ("Revived 2026-09-13 (spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md "
          "A4 hygiene): the stated blocker — 'prices_30m no longer collected' — is false; "
          "the master is live and refreshed 2026-09-11. Queued as a candidate so the "
          "research lane can re-gate it. Backtest-quarantined until the intraday bars are "
          "wired: the strategy reads market_data['*_30m_bars'], a kwarg the backtest never "
          "passes, so a fleet slot today buys a 0-trade run and no information.")

lsm = LifecycleStateMachine.from_manifest(MP)
for sid in SIDS:
    lsm.transition(sid, StrategyState.CANDIDATE, actor='manual:operator', reason=REASON,
                   metadata={'revival_2026_09_13': {
                       'spec': 'docs/specs/2026-09-12-quantdinger-adoptions-spec.md',
                       'quarantine_reason': QUAR,
                       'owed': 'wire intraday 30m bars into aux_data'}})
lsm.save_manifest(MP)


def _quarantine(disk):
    for sid in SIDS:
        disk['strategies'][sid]['backtest_quarantine'] = {'reason': QUAR, 'since': '2026-09-13'}
    return disk


with_manifest_lock(MP, _quarantine, actor='qd-stream-a:revival-quarantine')
m = json.loads(pathlib.Path(MP).read_text())
for sid in SIDS:
    e = m['strategies'][sid]
    print(sid, e['state'], e['state_since'],
          'quarantined' if e.get('backtest_quarantine') else 'NOT-QUARANTINED',
          'meta-ok' if e['metadata'].get('revival_2026_09_13') else 'META-MISSING')
PY
```
Expected two lines, each ending `quarantined meta-ok`: `S_TR04_zarattini_intraday_spy candidate 2026-09-13T…+00:00 quarantined meta-ok` and the same for `S_TR06_baltussen_eod_reversal`.

Then confirm `save_manifest` did not disturb anything else (it rewrites `updated_at` and normalises every entry's key order, which is why this check is a count, not a stat):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -c "
import json, subprocess
head = json.loads(subprocess.run(['git','show','HEAD:src/strategies/manifest.json'],
                                 capture_output=True, text=True).stdout)['strategies']
now = json.load(open('src/strategies/manifest.json'))['strategies']
assert set(head) == set(now), 'strategy set changed'
moved = [s for s in head if head[s]['state'] != now[s]['state']]
print('states changed:', moved)
print('quarantine flags now:', sorted(s for s in now if now[s].get('backtest_quarantine')))
"

- [ ] **Step 8: Verify the manifest still parses everywhere that reads it**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && \
  node -e "const m=require('./src/strategies/manifest.json'); const R={live:0,candidate:1,staging:2}; const e=Object.entries(m.strategies).filter(([,v])=>v.state in R); const q=e.filter(([,v])=>v.backtest_quarantine); console.log('fleet-eligible',e.length,'quarantined',q.length, q.map(([s])=>s).join(','))" && \
  python3 -m pytest tests/strategies/test_lifecycle_revival.py -q
```
Expected: a `fleet-eligible <N> quarantined <M> …,S_TR04_zarattini_intraday_spy,S_TR06_baltussen_eod_reversal` line and `3 passed`.

- [ ] **Step 9: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/strategies/registry.py src/strategies/lifecycle.py src/strategies/manifest.json tests/strategies/test_lifecycle_revival.py && git commit -q -m "chore(strategies): drop the dead S_fomc registry row, add archived->candidate revival, correct two false prices_30m reasons (Stream A task 6)

S_fomc_presell_spy_long's implementation file and .pyc are already gone; only
the _IMPL_MAP row survived. The two 'prices_30m is no longer collected' reasons
were false (master refreshed 2026-09-11) but the real blocker is that both
strategies read market_data['*_30m_bars'], which the backtest never passes —
recorded verbatim in the corrected reason and in a backtest_quarantine flag, so
the revival to candidate costs no nightly fleet slot. Spec A4 hygiene.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: A4 — the gated live-flip gate and script

**Files:**
- Create: `scripts/pit_gap_flip_gate.py`
- Create: `scripts/pit_gap_flip_after_fleet.sh`
- Test: `tests/scripts/test_pit_gap_flip_gate.py` (new)

**Interfaces:**
- Consumes: `strategy_backtest_runs (strategy_id, primary_window, config_json, run_at)`; `src/strategies/manifest.json` `strategies[*].state`; the `--rows-json` test seam (a JSON list of `[strategy_id, gap_fill, financials_pit, run_at_iso]`) so the gate's verdict logic is testable without a DB.
- Produces:
  - `pit_gap_flip_gate.verdict(live: list[str], rows: list, max_lagging: int) -> tuple[bool, list[str]]`
  - CLI printing `OK` or `NOT_YET` on line 1 then the per-gate detail, matching `scripts/target_mode_flip_gate.py`'s contract so `pit_gap_flip_after_fleet.sh` can reuse `target_mode_flip_after_fleet.sh`'s parsing verbatim.

**No Sharpe gate.** `target_mode_flip_gate.py` has a G2 median-ΔSharpe / positive-count leg; this gate deliberately does not. Ruling R1 states the operator ACCEPTS that PIT "may lower the fundamentals sleeve's Sharpes and demote strategies" — a Sharpe gate would block a flip already approved on exactly those terms, and there is no clean baseline population to compare against (the prior rows are a mix of `flat` and `atr_r` depending on how Tuesday's target flip resolved). Do not add one back.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_pit_gap_flip_gate.py
"""G1 uniformity verdict for the PIT+gap live flip. No DB: the gate's decision
logic is exercised through its pure `verdict` function."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        'pgfg', ROOT / 'scripts' / 'pit_gap_flip_gate.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


LIVE = ['S_a', 'S_b', 'S_c']


def test_all_live_rows_on_the_new_config_is_ok():
    m = _mod()
    rows = [['S_a', 'open', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    ok, detail = m.verdict(LIVE, rows, max_lagging=0)
    assert ok is True
    assert any('on_new=3' in line for line in detail)


def test_a_row_missing_gap_fill_lags():
    m = _mod()
    rows = [['S_a', 'level', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    ok, detail = m.verdict(LIVE, rows, max_lagging=0)
    assert ok is False
    assert any('S_a' in line for line in detail)


def test_a_row_missing_financials_pit_lags():
    m = _mod()
    rows = [['S_a', 'open', False, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    assert m.verdict(LIVE, rows, max_lagging=0)[0] is False


def test_a_live_strategy_with_no_row_at_all_lags():
    m = _mod()
    rows = [['S_a', 'open', True, '2026-09-20T01:00:00']]
    ok, detail = m.verdict(LIVE, rows, max_lagging=0)
    assert ok is False
    assert any('S_b' in line and 'S_c' in line for line in detail)


def test_max_lagging_tolerance_admits_a_stuck_strategy():
    m = _mod()
    rows = [['S_a', 'level', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00']]
    assert m.verdict(LIVE, rows, max_lagging=1)[0] is True


def test_non_live_rows_are_ignored():
    m = _mod()
    rows = [['S_a', 'open', True, '2026-09-20T01:00:00'],
            ['S_b', 'open', True, '2026-09-20T02:00:00'],
            ['S_c', 'open', True, '2026-09-20T03:00:00'],
            ['S_candidate', 'level', False, '2026-09-20T04:00:00']]
    assert m.verdict(LIVE, rows, max_lagging=0)[0] is True
```

- [ ] **Step 2: Run it and see the expected failure**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/scripts/test_pit_gap_flip_gate.py -q
```
Expected: `FileNotFoundError: ... scripts/pit_gap_flip_gate.py` — 6 errors.

- [ ] **Step 3: Write the gate**

```python
#!/usr/bin/env python3
"""pit_gap_flip_gate.py — read-only gate check for the OPENCLAW_FINANCIALS_PIT=1
+ OPENCLAW_BT_GAP_FILL=open live flip (spec 2026-09-12 §A4).

Prints 'OK' or 'NOT_YET' on the first line, then the per-gate detail — the same
contract scripts/target_mode_flip_after_fleet.sh parses.

  G1 fleet uniformity: every manifest state=live strategy's LATEST primary
     backtest row carries BOTH config_json.gap_fill='open' and
     config_json.financials_pit=true (at most --max-lagging exceptions, default
     3 — a strategy whose backtest OOMs every night must not hold the flip
     hostage; exceptions are listed).

There is deliberately NO Sharpe gate here, unlike scripts/target_mode_flip_gate.py.
Operator ruling R1 (2026-09-12) accepts up front that point-in-time fundamentals
"may lower the fundamentals sleeve's Sharpes and demote strategies", so a
median-ΔSharpe leg would block a flip that was approved on exactly those terms;
and there is no clean baseline population to compare against, because the prior
rows are a mix of target_mode 'flat' and 'atr_r'. Do not add one back.

Usage: python3 scripts/pit_gap_flip_gate.py [--max-lagging N] [--env-file PATH]
                                            [--manifest PATH] [--rows-json PATH]
--rows-json is the test seam: a JSON list of
[strategy_id, gap_fill, financials_pit, run_at_iso] used INSTEAD of the DB.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


def verdict(live, rows, max_lagging: int):
    """(ok, detail_lines). `rows` is newest-first-agnostic: the newest run_at
    per strategy wins. A live strategy with no row at all lags."""
    latest: dict = {}
    for sid, gap_fill, fin_pit, run_at in rows:
        if sid not in live:
            continue
        prev = latest.get(sid)
        if prev is None or str(run_at) > str(prev[2]):
            latest[sid] = (gap_fill, bool(fin_pit), run_at)
    lagging = []
    for sid in live:
        row = latest.get(sid)
        if row is None or row[0] != 'open' or row[1] is not True:
            lagging.append(sid)
    ok = len(lagging) <= max_lagging
    detail = [f"  G1 fleet: live={len(live)} on_new={len(live) - len(lagging)} "
              f"lagging={len(lagging)} (max {max_lagging}) -> {'OK' if ok else 'NOT_YET'}"]
    if lagging:
        detail.append('    lagging: ' + ', '.join(lagging[:12]) + (' …' if len(lagging) > 12 else ''))
    detail.append('  G2 sharpe: intentionally absent — ruling R1 accepts PIT-driven '
                  'Sharpe reductions and demotions')
    return ok, detail


def _rows_from_db(uri):
    import psycopg2
    with psycopg2.connect(uri) as c, c.cursor() as cur:
        cur.execute("""SELECT strategy_id,
                              COALESCE(config_json->>'gap_fill', 'level'),
                              COALESCE((config_json->>'financials_pit')::boolean, false),
                              run_at
                         FROM strategy_backtest_runs
                        WHERE primary_window = true
                        ORDER BY strategy_id, run_at DESC""")
        return [[r[0], r[1], bool(r[2]), r[3].isoformat() if hasattr(r[3], 'isoformat') else str(r[3])]
                for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-lagging', type=int, default=3)
    ap.add_argument('--env-file', default='/root/openclaw/.env')
    ap.add_argument('--manifest', default='/root/openclaw/src/strategies/manifest.json')
    ap.add_argument('--rows-json', default=None)
    a = ap.parse_args()
    live = sorted(k for k, v in json.load(open(a.manifest))['strategies'].items()
                  if v.get('state') == 'live')
    if a.rows_json:
        rows = json.load(open(a.rows_json))
    else:
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            m = re.search(r'^POSTGRES_URI=(.*)$', open(a.env_file).read(), re.M)
            uri = m.group(1).strip().strip('"') if m else None
        if not uri:
            print('NOT_YET'); print('  POSTGRES_URI not found'); return 0
        rows = _rows_from_db(uri)
    ok, detail = verdict(live, rows, a.max_lagging)
    print('OK' if ok else 'NOT_YET')
    print('\n'.join(detail))
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run and PASS**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/scripts/test_pit_gap_flip_gate.py -q
```
Expected: `6 passed`.

- [ ] **Step 5: Write the flip script**

```bash
#!/bin/bash
# pit_gap_flip_after_fleet.sh — guarded one-shot flip of OPENCLAW_FINANCIALS_PIT=1
# and OPENCLAW_BT_GAP_FILL=open (spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md
# §A1/§A2, epoch §A4). Modelled line-for-line on scripts/target_mode_flip_after_fleet.sh.
#
# Flips BOTH flags in .env, removes the fleet unit's temporary pit-gap drop-in
# (so .env is the single source of truth again) and restarts the user-scope
# johnbot ONLY IF all gates hold:
#   G1 fleet: every manifest state=live strategy's LATEST primary backtest row
#      carries config_json.gap_fill='open' AND config_json.financials_pit=true
#      (<= --max-lagging exceptions) — scripts/pit_gap_flip_gate.py.
#   G3 never inside the weekday 13:00–20:15 UTC compute window.
# There is no Sharpe gate on purpose: ruling R1 accepts PIT-driven Sharpe
# reductions and demotions up front.
# Otherwise it posts why and leaves everything alone (exit 0 — "not yet" is not
# a unit failure). Once applied it stops its own timer.
#
# Usage:
#   scripts/pit_gap_flip_after_fleet.sh            # check only, prints the verdict
#   scripts/pit_gap_flip_after_fleet.sh --apply    # check, then flip + restart on success
#   --max-lagging N (default 3)  --env-file PATH (default /root/openclaw/.env)
#   --no-restart  --no-post  --timer-unit NAME (default openclaw-pit-gap-flip.timer)
set -uo pipefail
cd /root/openclaw || exit 2

APPLY=0; MAXLAG=3; ENVF=/root/openclaw/.env; RESTART=1; POST=1
TIMER_UNIT=openclaw-pit-gap-flip.timer
DROPIN=/etc/systemd/system/openclaw-fleet-overnight-resume.service.d/pit-gap.conf
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1;; --max-lagging) MAXLAG="$2"; shift;;
    --env-file) ENVF="$2"; shift;;
    --no-restart) RESTART=0;; --no-post) POST=0;; --timer-unit) TIMER_UNIT="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
LOG=/root/openclaw/logs/pit_gap_flip.log
ts() { date -u +%FT%TZ; }
say() { echo "[pit-gap-flip $(ts)] $*" | tee -a "$LOG"; }
PG_URI="$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2- | tr -d '"')"

post_discord() {  # $1 = text; best-effort, never fails the script
  [ "$POST" = 1 ] || return 0
  POSTGRES_URI="$PG_URI" FLIP_TEXT="$1" python3 - <<'PY' 2>>"$LOG" || true
import json, os, urllib.request, psycopg2
text = os.environ['FLIP_TEXT'][:1900]
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute("SELECT webhook_urls->>'botjohn-log' FROM agent_registry WHERE webhook_urls->>'botjohn-log' IS NOT NULL LIMIT 1")
    row = cur.fetchone()
if row and row[0]:
    req = urllib.request.Request(row[0], data=json.dumps({'content': text}).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'fundjohn-pit-gap-flip/1.0'})
    urllib.request.urlopen(req, timeout=8).read()
PY
}

# --- already applied? ---------------------------------------------------------
if grep -qE '^OPENCLAW_BT_GAP_FILL=open' "$ENVF" && grep -qE '^OPENCLAW_FINANCIALS_PIT=1' "$ENVF"; then
  say "already applied (both flags set in $ENVF) — nothing to do"
  systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
  exit 0
fi

# --- G3 compute-window guard (weekdays 13:00–20:15 UTC) ----------------------
dow=$(date -u +%u); hm=$(date -u +%H%M)
if [ "$dow" -le 5 ] && [ "$hm" -ge 1300 ] && [ "$hm" -le 2015 ] && [ "$APPLY" = 1 ]; then
  say "refusing to flip inside the weekday compute window (UTC $hm)"; exit 0
fi

# --- G1 (read-only) -----------------------------------------------------------
VERDICT="$(POSTGRES_URI="$PG_URI" python3 scripts/pit_gap_flip_gate.py --max-lagging "$MAXLAG" 2>&1)"
STATUS="$(echo "$VERDICT" | head -1)"
say "verdict: $STATUS"; echo "$VERDICT" | tail -n +2 | tee -a "$LOG"

if [ "$STATUS" != "OK" ]; then
  post_discord "[pit-gap-flip] NOT applied — gate not met yet:
$(echo "$VERDICT" | tail -n +2 | head -12)"
  exit 0
fi
[ "$APPLY" = 1 ] || { say "check-only: gate MET; run with --apply to flip"; exit 0; }

# --- apply --------------------------------------------------------------------
cp -p "$ENVF" "$ENVF.bak.pit-gap-flip.$(date -u +%Y%m%dT%H%M%SZ)"
if grep -qE '^OPENCLAW_FINANCIALS_PIT=' "$ENVF"; then sed -i -E 's|^OPENCLAW_FINANCIALS_PIT=.*|OPENCLAW_FINANCIALS_PIT=1|' "$ENVF"
else printf '\nOPENCLAW_FINANCIALS_PIT=1\n' >> "$ENVF"; fi
if grep -qE '^OPENCLAW_BT_GAP_FILL=' "$ENVF"; then sed -i -E 's|^OPENCLAW_BT_GAP_FILL=.*|OPENCLAW_BT_GAP_FILL=open|' "$ENVF"
else printf 'OPENCLAW_BT_GAP_FILL=open\n' >> "$ENVF"; fi
say "flags set: $(grep -E '^OPENCLAW_(FINANCIALS_PIT|BT_GAP_FILL)=' "$ENVF" | tr '\n' ' ')"
if [ -f "$DROPIN" ]; then
  rm -f "$DROPIN" && systemctl daemon-reload && say "removed fleet drop-in $DROPIN (.env is now the single source)"
fi

RESULT="flags flipped"
if [ "$RESTART" = 1 ]; then
  if XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service; then
    sleep 5; ST=$(XDG_RUNTIME_DIR=/run/user/0 systemctl --user is-active johnbot.service)
    RESULT="$RESULT; johnbot restarted ($ST)"
  else
    RESULT="$RESULT; johnbot restart FAILED — restart manually: XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot"
  fi
fi
say "$RESULT"
systemctl disable --now "$TIMER_UNIT" 2>/dev/null || systemctl stop "$TIMER_UNIT" 2>/dev/null || true
post_discord "[pit-gap-flip] APPLIED — OPENCLAW_FINANCIALS_PIT=1 + OPENCLAW_BT_GAP_FILL=open ($RESULT). Gate:
$(echo "$VERDICT" | tail -n +2 | head -8)
Next: live aux['financials'] is unchanged (engine.py already sees filed data only); the backtest stops reading unfiled quarters and stops protecting stops against gaps. OWED: canonical sequence (weights rebuild -> floor recheck -> activation). Kill switch: OPENCLAW_FINANCIALS_PIT=0 + OPENCLAW_BT_GAP_FILL=level + user-scope johnbot restart."
exit 0
```

- [ ] **Step 6: Make it executable and syntax-check both**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && chmod +x scripts/pit_gap_flip_after_fleet.sh scripts/pit_gap_flip_gate.py && \
  bash -n scripts/pit_gap_flip_after_fleet.sh && echo "bash OK" && \
  python3 -c "import ast,sys; ast.parse(open('scripts/pit_gap_flip_gate.py').read()); print('py OK')" && \
  python3 scripts/pit_gap_flip_gate.py --help | head -3
```
Expected: `bash OK`, `py OK`, then the argparse usage line listing `--max-lagging`, `--env-file`, `--manifest`, `--rows-json`.

- [ ] **Step 7: Commit**

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add scripts/pit_gap_flip_gate.py scripts/pit_gap_flip_after_fleet.sh tests/scripts/test_pit_gap_flip_gate.py && git commit -q -m "feat(scripts): gated PIT+gap live flip (gate + apply script) (Stream A task 7)

G1 uniformity on config_json.gap_fill='open' AND config_json.financials_pit,
plus the 13:00-20:15 UTC compute-window guard. Deliberately NO Sharpe gate —
ruling R1 accepts PIT-driven Sharpe reductions and demotions. Spec A4.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: A4 — the epoch (OPERATOR / ORCHESTRATOR-RUN)

> **This task runs ONLY after the atr_r target-mode flip resolves on Tue 2026-09-15 21:45 UTC** — either `openclaw-target-mode-flip.timer` fires and `scripts/target_mode_flip_after_fleet.sh --apply` applies (`.env` gains `OPENCLAW_TARGET_MODE=atr_r`, the `target-atr-r.conf` drop-in is removed, the timer disables itself), or the operator records a rejection. Ruling R1: **never stack epochs.** Do not start any step below before that is settled, and do not start it while `openclaw-fleet-overnight-resume.service` or a `fleet-target-epoch-*` unit is active.
>
> Every command runs on production at `/root/openclaw` (main), not in the worktree. Merge Stream A to main first.

**Files:**
- Create: `docs/systemd/openclaw-fleet-overnight-resume.service.d/pit-gap.conf` (the canonical snapshot beside the installed copy; `docs/systemd/README.md` requires every installed unit and drop-in to have one)
- Modify: nothing else. `/etc/systemd/system/...` and `data/.refresh_backtests.done` are production state, not repo files.

**Interfaces:**
- Consumes: Tasks 1-7 merged to `main`; `scripts/fleet_weekend_window.sh --deadline <UTC YYYY-MM-DDTHH:MM> [--wait-unit U]`; `scripts/refresh_backtests_resumable.js --resume` (checkpoint `data/.refresh_backtests.done`, verified at `scripts/refresh_backtests_resumable.js:55`); `scripts/pit_gap_flip_after_fleet.sh --apply` (Task 7); `node src/agent/curators/weekly_live_sharpe.js`; `python3 scripts/run_universe_shrink.py --force`; `python3 -m backtest.activation_assigner --all --trigger <tag>` (CLI verified at `src/backtest/activation_assigner.py:492-508`).

- [ ] **Step 0: Stream A must already be on main (BLOCKING precondition)**

Every command from Step 2 onward runs the Task 1-7 code out of the production tree. The fleet unit and the flip timer both use `WorkingDirectory=/root/openclaw`, so an unmerged branch means the epoch runs the OLD engine and the drop-in is a lie.

```bash
cd /root/openclaw && git log --oneline -1 && \
  git log --oneline main | grep -c "Stream A task" && \
  ls scripts/pit_gap_flip_gate.py scripts/pit_gap_flip_after_fleet.sh scripts/measure_gap_fill_impact.py && \
  grep -c "OPENCLAW_BT_GAP_FILL" src/backtest/unified_backtest.py && \
  grep -c "OPENCLAW_FINANCIALS_PIT" src/strategies/aux_data_loader.py && \
  git status --short src/ scripts/
```
Expected: the current `main` HEAD, `7` Stream A task commits, all three scripts present, `≥2` and `≥1` flag hits respectively, and an EMPTY `git status` for `src/` and `scripts/` (never leave main half-edited across a timer boundary — spec §0). If any of that is untrue, STOP and merge first.

- [ ] **Step 1: Confirm the atr_r epoch has resolved (BLOCKING precondition)**

```bash
cd /root/openclaw && \
  grep -E '^OPENCLAW_TARGET_MODE=' /root/openclaw/.env || echo '(no OPENCLAW_TARGET_MODE line)' ; \
  ls /etc/systemd/system/openclaw-fleet-overnight-resume.service.d/ ; \
  systemctl list-timers 'openclaw-target-mode-flip.timer' --all --no-pager ; \
  systemctl is-active openclaw-fleet-overnight-resume.service ; \
  systemctl list-units 'fleet-*' --all --no-pager | head ; \
  pgrep -a -x node | grep refresh_backtests_resumable || echo '(no fleet driver running)'
```
Proceed ONLY if: `.env` reads `OPENCLAW_TARGET_MODE=atr_r` and `target-atr-r.conf` is gone (flip applied), **or** the operator has recorded that the flip was rejected; AND the fleet service is `inactive` with no `refresh_backtests_resumable` node process. If any of that is untrue, STOP and report.

- [ ] **Step 2: Size the change before spending 53 h of compute**

```bash
cd /root/openclaw && nice -n 19 python3 scripts/measure_gap_fill_impact.py --limit 60 --min-trades 30 2>&1 | tail -40
```
Expected: a per-strategy table plus the TOTAL line. Record the total gapped fraction and `mean_delta_all` in the changelog entry (Task 9). If gapped is < 0.5 % of stop exits fleet-wide, say so in the entry — the epoch still runs (the gate must be truthful regardless of magnitude), but the expected Sharpe movement is then attributable to A1, not A2.

- [ ] **Step 3: Rotate the fleet checkpoint**

```bash
cd /root/openclaw && D=$(date -u +%Y%m%d) && \
  wc -l data/.refresh_backtests.done && \
  mv data/.refresh_backtests.done data/.refresh_backtests.done.pre-pit-gap-$D && \
  { [ -f data/.refresh_backtests.failed ] && mv data/.refresh_backtests.failed data/.refresh_backtests.failed.pre-pit-gap-$D || true; } && \
  ls -la data/.refresh_backtests.* && \
  node -e "const m=require('/root/openclaw/src/strategies/manifest.json');const R={live:0,candidate:1,staging:2};const e=Object.entries(m.strategies).filter(([,v])=>v.state in R && !v.backtest_quarantine);console.log('fleet size',e.length)"
```
Expected: the pre-rotation `wc -l` (the previous epoch's done count), then a listing showing `.pre-pit-gap-<date>` files and NO `.refresh_backtests.done` (the driver recreates it), then `fleet size <N>` — the number of strategies the epoch will re-derive.

- [ ] **Step 4: Install the drop-in (alongside `oom-continue.conf` / `rf-macro.conf`)**

```bash
sudo tee /etc/systemd/system/openclaw-fleet-overnight-resume.service.d/pit-gap.conf >/dev/null <<'CONF'
[Service]
# PIT-fundamentals + gap-fill epoch (2026-09-12, spec
# docs/specs/2026-09-12-quantdinger-adoptions-spec.md §A1/§A2, epoch §A4).
# The fleet re-backtest runs with fundamentals visible only from their filing
# date and with stops that do not protect against overnight gaps, so every
# canonical row carries config_json.financials_pit=true and
# config_json.gap_fill='open' BEFORE the live flags flip. EnvironmentFile (.env)
# wins over Environment=, so once .env sets these two these lines are inert —
# scripts/pit_gap_flip_after_fleet.sh removes this file at flip time so a later
# .env rollback cannot leave the fleet silently on the new config.
Environment=OPENCLAW_FINANCIALS_PIT=1
Environment=OPENCLAW_BT_GAP_FILL=open
CONF
sudo systemctl daemon-reload
systemctl show openclaw-fleet-overnight-resume.service -p Environment | tr ' ' '\n' | grep -E 'FINANCIALS_PIT|GAP_FILL|TARGET_MODE|RF_SOURCE'
```
Expected: `OPENCLAW_FINANCIALS_PIT=1` and `OPENCLAW_BT_GAP_FILL=open` in the effective environment.

Then snapshot it into the repo:

```bash
cd /root/openclaw && mkdir -p docs/systemd/openclaw-fleet-overnight-resume.service.d && \
  cp /etc/systemd/system/openclaw-fleet-overnight-resume.service.d/pit-gap.conf \
     docs/systemd/openclaw-fleet-overnight-resume.service.d/pit-gap.conf && \
  ls docs/systemd/openclaw-fleet-overnight-resume.service.d/
```
Expected: `oom-continue.conf  pit-gap.conf  rf-macro.conf  target-atr-r.conf`.

- [ ] **Step 5: Arm the weekend window for Sat 2026-09-19**

Sat 08:05 UTC → no NEW strategy spawned after Sun 10:30 UTC; `RuntimeMaxSec=102300` s puts the hard wall at Sun 12:30 UTC (deadline + one per-strategy timeout), matching the `fleet-rf-epoch-20260906` pattern.

```bash
sudo systemd-run --on-calendar="2026-09-19 08:05:00 UTC" --unit=fleet-pit-gap-epoch-20260919 \
  -p Nice=19 -p OOMPolicy=continue -p RuntimeMaxSec=102300 \
  -p EnvironmentFile=/root/openclaw/.env -p WorkingDirectory=/root/openclaw \
  --setenv=NUMEXPR_MAX_THREADS=1 --setenv=NUMEXPR_NUM_THREADS=1 \
  --setenv=OPENCLAW_FINANCIALS_PIT=1 --setenv=OPENCLAW_BT_GAP_FILL=open \
  /bin/bash /root/openclaw/scripts/fleet_weekend_window.sh --deadline 2026-09-20T10:30
systemctl list-timers 'fleet-pit-gap-epoch-20260919*' --all --no-pager
```
Expected: a timer row for `fleet-pit-gap-epoch-20260919.timer` NEXT at `2026-09-19 08:05 UTC`. A transient timer does NOT survive a reboot — if the box reboots before Saturday, re-run this command.

- [ ] **Step 6: Arm the gated live flip (persistent unit — the 2026-09-06 lesson)**

21:55 UTC keeps it clear of the rf flip (21:40), the options-surface flip (21:50) and the target-mode flip (21:45).

```bash
sudo tee /etc/systemd/system/openclaw-pit-gap-flip.service >/dev/null <<'UNIT'
[Unit]
Description=Guarded flip of OPENCLAW_FINANCIALS_PIT=1 + OPENCLAW_BT_GAP_FILL=open after the fleet epoch
After=network.target postgresql.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
TimeoutStartSec=1800
ExecStart=/bin/bash /root/openclaw/scripts/pit_gap_flip_after_fleet.sh --apply
StandardOutput=journal
StandardError=journal
UNIT
sudo tee /etc/systemd/system/openclaw-pit-gap-flip.timer >/dev/null <<'UNIT'
[Unit]
Description=Tue-Fri 21:55 UTC — try the PIT+gap live flip until it applies

[Timer]
OnCalendar=Tue..Fri *-*-* 21:55:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now openclaw-pit-gap-flip.timer && \
  systemctl list-timers openclaw-pit-gap-flip.timer --all --no-pager && \
  cd /root/openclaw && cp /etc/systemd/system/openclaw-pit-gap-flip.service /etc/systemd/system/openclaw-pit-gap-flip.timer docs/systemd/
```
Expected: the timer listed as active with its next elapse, and both unit files copied into `docs/systemd/`. Dry-run the gate once now (it will say NOT_YET until the epoch is uniform):

```bash
cd /root/openclaw && python3 scripts/pit_gap_flip_gate.py --max-lagging 3
```
Expected first line: `NOT_YET`, then `G1 fleet: live=<N> on_new=0 lagging=<N> (max 3) -> NOT_YET`.

- [ ] **Step 7: After the flip applies — the canonical sequence (OWED)**

Run only once `logs/pit_gap_flip.log` shows `APPLIED` and outside 13:00–20:15 UTC:

```bash
cd /root/openclaw && \
  node src/agent/curators/weekly_live_sharpe.js 2>&1 | tail -20 && \
  python3 scripts/run_universe_shrink.py --force 2>&1 | tail -20 && \
  python3 -m backtest.activation_assigner --all --trigger pit_gap_epoch 2>&1 | tail -20
```
Expected in order: a weights-rebuild summary line with the new row count; the shrink run reporting recomputed conviction floors (`--force` because a recommendation already exists for most strategies and the run silently skips them otherwise); then the assigner's activated / deactivated / newly-dormant counts. Record all three in the changelog. Ruling R1 accepts demotions here — do not intervene to keep a strategy live.

- [ ] **Step 8: Commit the snapshot files**

```bash
cd /root/openclaw && git add docs/systemd/openclaw-fleet-overnight-resume.service.d/pit-gap.conf docs/systemd/openclaw-pit-gap-flip.service docs/systemd/openclaw-pit-gap-flip.timer && git commit -q -m "ops(systemd): PIT+gap fleet epoch drop-in and gated flip timer snapshots (Stream A task 8)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" && git push origin main
```

---

### Task 9: Changelog

**Files:**
- Modify: `docs/archive/changelog.md` — one dated entry at the top of the `## Recent Changes` list (newest first; the list currently starts with the 2026-09-08 20:40 UTC entry).

**Interfaces:**
- Consumes: the commits from Tasks 1-8 and the numbers recorded in Task 8 Steps 2, 3 and 7.

- [ ] **Step 1: Write the entry**

Insert immediately after the `## Recent Changes` heading and its blank line, replacing every `…` with the value actually observed:

```markdown
- **2026-09-13: Stream A — the gates now measure something true (spec `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` §1, plan `docs/superpowers/plans/2026-09-12-qd-stream-a-gates.md`).** Four changes, three behind flags whose unset value is byte-identical to the previous engine. **A1 fundamentals point-in-time (`OPENCLAW_FINANCIALS_PIT`)** — `financials.parquet`'s `date` is the FMP statement PERIOD END, not a filing date (`src/pipeline/backfillers/fmp.py::build_financial_rows` stores none), so `aux_data_loader._financials_slice` was letting every backtest bar read numbers that were not public for weeks: pure look-ahead on `S10_quality_value`, `S_bankruptcy_risk_anomaly`, `S_ast_asset_growth_effect`, `S_ast_roa_effect_within_stocks`, `S_accrual_anomaly`. A module-cached `available_at` per (ticker, period-end) — the first `earnings.parquet` report date strictly after the period end and within 120 d, else period end + 60 d, built by one `merge_asof` — now gates visibility. The LIVE path is untouched (`engine.py::load_aux_data` already sees only filed data); a parity test locks that the backtest slice's key set equals the live dict's on one shared synthetic frame, and that `available_at` never leaks into it. Ruling R1 accepts that this may lower the fundamentals sleeve's Sharpes and demote strategies. **A2 gap-through-a-level fills at the open (`OPENCLAW_BT_GAP_FILL=open`)** — `unified_backtest._bar_exit` returned the stop LEVEL on any low touch, so a bar that opened below the stop was credited a fill the market never offered; `quick_backtest.py:477-480` had had the honest rule (and an oracle test) since the beginning. `_bar_exit` gains a keyword-only `open_`; `simulate_trade` and `open_book.advance_open_book` pass `bar.get('open')` (None on frames with no `open` column ⇒ legacy level fill). Double-touch priority is unchanged. Measured before the epoch with the new read-only `scripts/measure_gap_fill_impact.py` (ticker-filtered three-column `prices.parquet` read, well under 1 GB): … % of stored primary-run stop exits gapped, mean Δ over all stop exits …  % (gross level-vs-open; stored `pnl_pct` is net of adverse fills). **A3 exit census + cost drag** — every run's `strategy_backtest_runs.config_json` now carries `exit_reasons {reason: {n, mean_pnl_pct, median_hold_days}}` (hook exits keep their `strategy_exit:` prefix — this is how "65 % of exits are `pair_decohered`" becomes readable from a stored run) and `cost_drag_bps` = 1e4 · Σ modelled round-trip cost / Σ |gross pnl|, both NaN-guarded and `None` on no evidence. Provenance only; never a Sharpe, never a gate input. `config_json` also gains `gap_fill` and `financials_pit`. **A4 epoch** — checkpoint rotated `data/.refresh_backtests.done` → `.pre-pit-gap-…` (… done entries), drop-in `pit-gap.conf` installed on `openclaw-fleet-overnight-resume.service` beside `oom-continue`/`rf-macro`, weekend transient unit `fleet-pit-gap-epoch-20260919` (Sat 09-19 08:05 UTC → deadline Sun 10:30 UTC, `RuntimeMaxSec=102300`) via `scripts/fleet_weekend_window.sh`, and the live flip automated on the persistent `openclaw-pit-gap-flip.timer` (Tue–Fri 21:55 UTC → `scripts/pit_gap_flip_after_fleet.sh --apply`). The flip gate is **G1 uniformity only** — every live strategy's latest primary row carrying both `gap_fill='open'` and `financials_pit` — plus the 13:00–20:15 UTC compute-window guard; **deliberately no Sharpe gate**, because R1 pre-accepts the Sharpe reductions a Sharpe gate would block. Started only after the atr_r target flip resolved on Tue 09-15 (R1: never stack epochs). OWED after the flip and run: weights rebuild → floor recheck (`run_universe_shrink --force`) → activation (…). **Hygiene:** the `S_fomc_presell_spy_long` row in `registry.py::_IMPL_MAP` was dropped — its implementation file and `.pyc` were already gone, only the registry row survived. The two manifest history reasons claiming `S_TR04_zarattini_intraday_spy` and `S_TR06_baltussen_eod_reversal` "require prices_30m which is no longer collected daily" were FALSE (`data/master/prices_30m.parquet` refreshed 2026-09-11) — but so is the implied fix: **both strategies read intraday bars from a `market_data` kwarg the backtest never passes** (`market_data['spy_30m_bars']` / `market_data['intraday_30m_bars']`), and neither reads `aux_data['prices_30m']`. Both reasons now say exactly that, both strategies were revived `archived → candidate` through a new `VALID_TRANSITIONS` edge (`lifecycle.py`; ARCHIVED is otherwise still terminal and archived→live is still refused), and both carry `backtest_quarantine` naming the wiring gap so the revival costs no nightly fleet slot for a 0-trade run. **Owed to the operator (two items, both found while doing this):** (1) wire 30-minute bars into `aux_data` so those two strategies can actually be gated; (2) `LifecycleStateMachine.to_dict()` (`src/strategies/lifecycle.py:819-857`) emits a FIXED entry key set, so **any** `save_manifest` call silently drops a top-level `backtest_quarantine` (or any other non-schema key) — harmless today because nothing else carried one, but it means the two flags set here will be wiped by the next unrelated `save_manifest` (e.g. `auto_demote_negative_sharpe`) and the strategies will quietly rejoin the nightly fleet as 0-trade runs. The flag has to be top-level because `refresh_backtests_resumable.js:119` reads it there; the same fact is mirrored into `metadata.revival_2026_09_13` (which does round-trip) as the durable record. Kill switches: `OPENCLAW_FINANCIALS_PIT=0`, `OPENCLAW_BT_GAP_FILL=level`, user-scope johnbot restart.
```

- [ ] **Step 2: Verify the entry is first and the file still renders**

```bash
cd /root/openclaw && grep -n "^## Recent Changes" -A 3 docs/archive/changelog.md | head -6 && grep -c "2026-09-13: Stream A" docs/archive/changelog.md
```
Expected: the `## Recent Changes` heading followed by the new 2026-09-13 entry, and `1`.

- [ ] **Step 3: Confirm no placeholder survived**

```bash
cd /root/openclaw && grep -n '…' docs/archive/changelog.md | head
```
Expected: no line from the 2026-09-13 entry. Every `…` above must have been replaced with a measured number before this commit.

- [ ] **Step 4: Commit**

```bash
cd /root/openclaw && git add docs/archive/changelog.md && git commit -q -m "docs(changelog): Stream A — fundamentals PIT, gap fill, exit census, epoch, hygiene (task 9)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" && git push origin main
```

---

## Self-review (done at authoring time)

**Spec coverage**

| Spec item | Task(s) |
|---|---|
| A1 `available_at` rule (earnings ≤ 120 d, else +60 d) + `_financials_slice` under `OPENCLAW_FINANCIALS_PIT=1`, unset byte-identical | Task 1 |
| A1 synthetic tests (03-31 quarter, earnings 05-05: invisible 04-15, visible 05-05; no earnings row → visible 05-30) | Task 1 Step 1 (`test_pit_hides_unfiled_quarter`, `test_pit_reveals_on_the_report_date`, `test_pit_fallback_sixty_days_when_no_earnings_row`) |
| A1 parity with `engine.py`'s live `aux['financials']` shape | Task 2 |
| A2 `_bar_exit(..., open_=…)` gap rule, long/short/target mirrored, double-touch unchanged, unset/`level` identical | Task 3 |
| A2 both call sites pass the bar open | Task 3 Step 4 |
| A2 `gap_fill` in the `config_json` literal | Task 3 Step 5 |
| A2 frozen-values test that the flag unset reproduces today's numbers | Task 3 Step 1 (`test_flag_unset_reproduces_todays_frozen_values`) |
| A2 `scripts/measure_gap_fill_impact.py` (read-only, `--limit`, < 1 GB, per-strategy Δmean + count) | Task 4 |
| A3 `exit_reasons` + `cost_drag_bps` in `config_json`, 3-trade synthetic test | Task 5 |
| A4 checkpoint rotation → `.pre-pit-gap-<date>` | Task 8 Step 3 |
| A4 drop-in `pit-gap.conf` beside the existing drop-ins | Task 8 Step 4 |
| A4 weekend transient unit via `fleet_weekend_window.sh` for Sat 09-19 | Task 8 Step 5 |
| A4 gated live-flip script modelled on `target_mode_flip_after_fleet.sh` (`.env` + user-scope johnbot restart) | Tasks 7, 8 Step 6 |
| A4 "only after the atr_r flip resolves Tue 09-15 21:45Z" | Task 8 header + Step 1 |
| A4 owed sequence: weights → floors → activation, `run_universe_shrink --force` | Task 8 Step 7 |
| Hygiene: stale `S_fomc_presell_spy_long` registry row + `.pyc` | Task 6 Step 5 (verify-absent, no-op — the `.pyc` does not exist) |
| Hygiene: two stale manifest reasons corrected + re-queued as candidates | Task 6 Steps 6-7 |
| Changelog | Task 9 |

**Manifest-write safety (verified at authoring time, 2026-09-13):** `manifest.json` is byte-stable under its canonical writer (`_manifest_lock.write_atomic` = `json.dumps(indent=2)`, no trailing newline) — checked, `True` — so Task 6's edits produce a two-line diff, not a reformat; Step 6 re-checks it and aborts if a concurrent writer changed the format. Both writes go through the cross-process lock because JS writers mutate the same file. `LifecycleStateMachine.to_dict()` drops non-schema top-level keys; 0 entries currently carry one and 0 are quarantined, so `save_manifest` strips nothing today, but the gap is recorded as OWED in the changelog and the quarantine fact is mirrored into `metadata` for durability.

**Placeholder scan:** no "TBD", no "add error handling", no "similar to Task N", no "write tests". Every test file is given in full; every code insertion is given in full, including the two blocks that repeat the `_o = bar.get('open')` idiom in different modules (Task 3 Step 4) rather than cross-referencing. The only `…` characters in this plan are inside the Task 9 changelog draft, where Step 3 makes replacing them a checked gate.

**Signature consistency:** `_bar_exit(direction, high, low, stop_loss, target_1, dt_priority, *, open_=None)` — defined Task 3 Step 3, called Task 3 Step 4 (twice), asserted Task 3 Step 1. `_financials_slice(date_str) -> dict` unchanged — Tasks 1, 2. `_financials_with_availability() -> pd.DataFrame` and `_financials_pit_enabled() -> bool` — defined and used Task 1, cache reset in Tasks 1 and 2 fixtures. `exit_reason_census(trades) -> dict` and `cost_drag_bps(trades, *, cost_bps_by_ticker=None, flat_bps=0.0) -> Optional[float]` — defined Task 5 Step 4, called Task 5 Step 5, asserted Task 5 Step 1. `reprice_stop_exit(direction, entry_price, stop, bar_open) -> Optional[float]` — Task 4. `verdict(live, rows, max_lagging) -> (bool, list[str])` — Task 7, consumed by `pit_gap_flip_after_fleet.sh` through the `OK`/`NOT_YET` first line only. Env vars used: `OPENCLAW_FINANCIALS_PIT`, `OPENCLAW_BT_GAP_FILL` (new); `OPENCLAW_BT_DOUBLE_TOUCH`, `OPENCLAW_BT_FILL_MODEL`, `OPENCLAW_BACKTEST_SLIPPAGE`, `OPENCLAW_TARGET_MODE`, `POSTGRES_URI` (existing). `config_json` keys added: `gap_fill`, `financials_pit`, `exit_reasons`, `cost_drag_bps` — written in Tasks 3 and 5, read by Task 7's gate. Manifest fields touched: `strategies[sid].state`, `.state_since`, `.history[]`, `.backtest_quarantine` — the last one is the flag `refresh_backtests_resumable.js:119` already reads.
