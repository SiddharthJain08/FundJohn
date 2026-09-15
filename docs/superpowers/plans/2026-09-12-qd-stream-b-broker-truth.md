# Stream B — broker truth (stop fills, broker_fills ledger, slippage digest, ownership) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Make the live ledger agree with the broker on three axes the system currently gets wrong:

- **B1** — a stop that fills at the broker leaves `signal_pnl` saying `open`. `engine.update_pnl`
  only infers `close_reason='stop_loss'` at EOD from a parquet close crossing the stop
  (`src/execution/engine.py:2051-2058`), so a broker stop fill inside the day is invisible to the
  stop-out cooldown (`regime_blended_sizer._load_recent_stopouts`, `:2297-2317`) and the same name
  is re-bought on the next cycle. Wire the already-existing exit-fill classifier into
  `open_reconcile.drop_signal_close`, and fix the 200-row closed-order read
  (`afterhours_tp.py:619-620`) that silently drops fills past row 200.
- **B2** — nothing in the DB records what the broker actually filled, when. Add a `broker_fills`
  fact table fed by the reconcile step's paginated FILL activity poll, `alpaca_submissions.filled_at`,
  an exit-leg slippage column on `signal_pnl`, and a `fill_slippage:` line in the daily
  #trade-reports digest with a verdict against our own per-ticker half-spread cost model.
- **B3** — no one checks that the shares the broker holds are the shares our signals claim. Add a
  per-ticker ownership ledger computed nightly in the `reconcile` step, an opt-in sizer block on
  opens/adds for un-owned tickers, and a `position_ownership_clean` system check.

## Architecture

```
                       ┌─ openclaw-afterhours-stop-monitor.timer (every 10 min, 04:00-20:00 ET Mon-Fri)
                       │     afterhours_tp.py --monitor
                       │       run_exit_fill_reporter()
B1                     │         stop_reattach.fetch_recent_closed_orders()   [NEW, replaces --limit 200]
                       │         classify_exit_fills()                        [unchanged]
                       │         → Discord post (unchanged)
                       └─────────→ open_reconcile.drop_signal_close(reason='stop_loss', closed_at=…)  [NEW]
                                     → signal_pnl.close_reason='stop_loss'
                                     → _load_recent_stopouts sees it → cooldown fires

                       ┌─ pipeline `reconcile` step (alpaca_reconcile.py --date <run_date>)
                       │     fetch_fills_for_date()            [unchanged, paginated]
B2                     │     collapse_fills()                  [+ filled_at]
                       │     _apply_fill()                     [+ filled_at column write]
                       │     fetch_recent_closed_orders(symbols=…) → order_meta   [NEW]
                       │     ingest_broker_fills()              [NEW, ON CONFLICT DO NOTHING]
                       │     backfill_exit_slippage()           [NEW → signal_pnl.exit_slippage_bps]
                       └─ pipeline `report` step (send_report.py)
                             fill_slippage.fill_slippage_line() [NEW] → "fill_slippage: …" line

                       ┌─ pipeline `reconcile` step (alpaca_reconcile.py, after phantom cleanup)
B3                     │     position_ownership.run_ownership_pass()  [NEW]
                       │       → position_ownership rows + transition-only log lines
                       ├─ pipeline `trade` step (regime_blended_sizer_live.py)
                       │     _load_ownership_blocklist() → _apply_entry_hygiene_gate(ownership_blocked=…)
                       │       → `_shed` semantics: opens dropped, adds capped at held, exits untouched
                       └─ `python3 -m system_checks --check position_ownership_clean`
```

Design invariants carried through every task:

- Pure functions do the arithmetic and the classification; SQL and the Alpaca CLI live in thin
  loaders. Every test drives the pure function or injects a fake cursor / fake `_cli`.
- All DB writes are INSERT/UPDATE only. `broker_fills` and `position_ownership` are append-only.
- New behaviour that can *change orders* ships behind `OPENCLAW_OWNERSHIP_BLOCK` (unset = today's
  behaviour byte-for-byte). B1 is a pure bug fix (spec item 3, operator-approved without a flag).

## Tech Stack

- Python 3 (`python3`), stdlib + `psycopg2` + `requests`; no new packages.
- PostgreSQL via `psycopg2`; migrations are plain `.sql` files under `src/database/migrations/`,
  applied in filename sort order by `migrate()` in `src/database/postgres.js:42-55`, which is
  called once from `src/channels/discord/bot.js:1663` on johnbot start. A new migration therefore
  lands on the next `johnbot.service` restart, and only after this branch merges to main.
- Alpaca Go CLI at `/root/go/bin/alpaca` (`ALPACA_CLI_BIN`), always through
  `stop_reattach._run_cli` (`:191-227`; retry/backoff/`--quiet`/`--timeout`) or
  `alpaca_reconcile`'s own `subprocess.run`.
- pytest; `unittest.mock` / `monkeypatch` for CLI + DB fakes.

## Spec

`/root/openclaw/.claude/worktrees/qd-adoptions/docs/specs/2026-09-12-quantdinger-adoptions-spec.md`
— section 0 (non-negotiables, lines 26-49) and section 2 (Stream B: B1 `:122-143`, B2 `:144-166`,
B3 `:167-181`).

## Global Constraints

Spec §0, verbatim, one per line:

- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history.
- Backtest side is AUTHORITATIVE (08-07 ruling). Any live/backtest disagreement is fixed on the live side unless the backtest is provably look-ahead — items 1 and 2 are exactly that case.
- Every new behaviour ships behind an env flag whose unset value is byte-identical to today's behaviour, unless the item is a pure bug fix that the operator has explicitly approved (items 3, 10, 11, 16, and the circuit-breaker regime change).
- 2-core / 8 GB / no swap: never load whole `prices.parquet` or `options_eod.parquet`; slice by date/ticker; no always-on threads; no new packages.
- Production = working tree on `main`; timer-spawned scripts pick up the tree on their next run. Work on a worktree branch; merge to main only when the whole stream is green; never leave main half-edited across a timer boundary.
- Tests on this box reach the REAL DB (`.env` loads at import) — stub gates in fixtures; never run the full suite while the fleet runs; never include `test_regime_stratified_backtest`.
- Every file:line cited below was grep-verified on 2026-09-11 against main `d9dbdf06`; re-verify before editing (lines drift).
- Log to `docs/archive/changelog.md` (newest first) per stream, not to CLAUDE.md.

Test-running rules for this plan:

- Run ONLY the task's own test file plus the test files of the module you touched. Never the whole suite.
- Example: `cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_b_stop_fill_close.py -q`
- Every command in this plan runs from the worktree: `cd /root/openclaw/.claude/worktrees/qd-adoptions && …`. Never `cd /root/openclaw`.
- Tests reach the REAL Postgres (`src/execution/*` loads `.env` at import). Stub every DB and CLI touchpoint in a fixture: fake cursors, `monkeypatch.setattr(mod, '_cli', …)`, `monkeypatch.setattr(mod.subprocess, 'run', …)`. No test in this plan may open a psycopg2 connection.
- Never call the real `alpaca` CLI in a test. Never invoke `python3 -m system_checks` against the live broker in a test — call the check function directly with its CLI/DB stubbed.
- A fleet backtest is running: never run `tests/backtest/test_regime_stratified_backtest.py`, and never run a pytest invocation that imports `src/backtest/unified_backtest.py`'s parquet loaders.

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `src/database/migrations/155_broker_fills.sql` | `broker_fills` fact table + `alpaca_submissions.filled_at` + `signal_pnl.exit_slippage_bps` |
| `src/database/migrations/156_position_ownership.sql` | `position_ownership` append-only per-ticker ownership ledger |
| `src/execution/fill_slippage.py` | Entry/exit slippage stats + verdict bands + the `fill_slippage:` digest line |
| `src/execution/position_ownership.py` | Pure ownership computation, persistence, transition logging, sizer blocklist loader |
| `tests/execution/test_closed_order_fetch.py` | `fetch_recent_closed_orders` paging/scoping/dedupe |
| `tests/execution/test_b_stop_fill_close.py` | B1 stop fill → `drop_signal_close`, idempotency, no-open-signal case, first-run guard |
| `tests/database/test_stream_b_migration_shape.py` | Static DDL-shape assertions for migrations 155 + 156 (no DB connection) |
| `tests/execution/test_broker_fills_ingest.py` | `collapse_fills` filled_at, `order_meta` build, `ingest_broker_fills` dedupe |
| `tests/execution/test_exit_slippage.py` | Exit-level selection + signed-adverse bp math |
| `tests/execution/test_fill_slippage_digest.py` | Digest line formatting, verdict bands, n=0 → "n/a" |
| `tests/execution/test_position_ownership.py` | Ownership statuses, tolerances, transition-only logging |
| `tests/execution/test_ownership_sizer_block.py` | Sizer blocks opens/adds, allows reduces, flag-gated |
| `tests/system_checks/test_position_ownership_clean.py` | The new system check's PASS/WARN/FAIL mapping |

**Modified**

| Path | Change |
|---|---|
| `src/execution/stop_reattach.py` | + `fetch_recent_closed_orders()` (newest-first, `--symbols`-scoped, deduped) |
| `src/execution/afterhours_tp.py` | `run_exit_fill_reporter` uses the new fetcher; closes matching open signals on `stop`/`ah_exit` fills |
| `src/execution/open_reconcile.py` | `drop_signal_close` gains keyword-only `closed_at=None` |
| `src/execution/alpaca_reconcile.py` | `collapse_fills`/`_apply_fill` carry `filled_at`; `ingest_broker_fills`; `backfill_exit_slippage`; ownership pass in `main()` |
| `src/execution/send_report.py` | `fill_slippage:` line appended to the #trade-reports digest |
| `src/execution/regime_blended_sizer.py` | `_apply_entry_hygiene_gate(..., ownership_blocked=None)` + `_load_ownership_blocklist()` |
| `src/system_checks/checks/broker.py` | + `position_ownership_clean` check |
| `docs/archive/changelog.md` | Stream B entry, newest first |

---

### Task 1: Bounded newest-first closed-order fetch

**Safety:** `stop_reattach.py` is imported by `openclaw-stop-reattach*` timers AND by
`afterhours_tp.py --monitor` (`openclaw-afterhours-stop-monitor.timer`, every 10 min
04:00–20:00 ET Mon–Fri). This task only ADDS a function — no existing call site changes, so the
timers behave identically until Task 2 wires it in. Everything here is inert until this branch
merges to main.

**Files:**
- Modify: `src/execution/stop_reattach.py` — insert after `_fetch_open_orders` (currently ends at line 182, immediately before `def _is_rate_limited` at line 185)
- Test: `tests/execution/test_closed_order_fetch.py` (Create)

**Interfaces:**
- Consumes: `stop_reattach._run_cli(args, timeout=15) -> tuple[bool, object, dict | None]` (line 191); `stop_reattach.log(msg: str) -> None` (line 87)
- Produces: `stop_reattach.fetch_recent_closed_orders(symbols=None, *, include_unscoped: bool = True, page: int = 500, chunk: int = 100, timeout: int = 45) -> tuple[bool, list]`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_closed_order_fetch.py`:

```python
"""fetch_recent_closed_orders — the 200-row closed-order read replacement.

The spec asked for the --after-order-id keyset loop stop_reattach._fetch_open_orders
uses. That cursor is ASCENDING and inception-anchored: on --status open the set is
bounded by the live book, but on --status closed it starts at the oldest order the
account ever had. A 10-minute timer would spend its whole page budget on ancient
history and never reach today's fills. The CLI exposes no time bound, so the correct
shape is the DEFAULT newest-first window widened to 500 rows, plus --symbols-scoped
reads that spend a fresh window on the names we actually care about.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import stop_reattach as sr  # noqa: E402


def _recorder(pages, calls):
    """Fake _run_cli: returns pages.pop(0) per call, recording argv."""
    def _cli(args, timeout=15):
        calls.append(list(args))
        if not pages:
            return True, [], None
        nxt = pages.pop(0)
        if nxt is None:
            return False, None, {'error': 'boom'}
        return True, nxt, None
    return _cli


def test_unscoped_read_is_newest_first_not_keyset(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([[{'id': 'o1'}]], calls))
    ok, orders = sr.fetch_recent_closed_orders()
    assert ok is True and [o['id'] for o in orders] == ['o1']
    argv = calls[0]
    assert argv[:2] == ['order', 'list']
    assert '--status' in argv and argv[argv.index('--status') + 1] == 'closed'
    assert '--nested' in argv
    assert argv[argv.index('--limit') + 1] == '500'
    assert '--direction' not in argv, 'asc on closed orders walks from account inception'
    assert '--after-order-id' not in argv, 'keyset cursor is ascending-only'


def test_symbol_scoped_reads_are_chunked(monkeypatch):
    calls = []
    syms = [f'T{i:03d}' for i in range(250)]
    monkeypatch.setattr(sr, '_run_cli', _recorder([[], [], [], []], calls))
    ok, _ = sr.fetch_recent_closed_orders(syms, include_unscoped=False, chunk=100)
    assert ok is True
    assert len(calls) == 3
    groups = [c[c.index('--symbols') + 1].split(',') for c in calls]
    assert [len(g) for g in groups] == [100, 100, 50]
    assert sorted(sum(groups, [])) == sorted(syms)


def test_dedupes_by_order_id_across_reads(monkeypatch):
    calls = []
    pages = [[{'id': 'dup'}, {'id': 'a'}], [{'id': 'dup'}, {'id': 'b'}]]
    monkeypatch.setattr(sr, '_run_cli', _recorder(pages, calls))
    ok, orders = sr.fetch_recent_closed_orders(['AAA'])
    assert ok is True
    assert [o['id'] for o in orders] == ['dup', 'a', 'b']


def test_partial_failure_still_returns_ok_with_coverage(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([[{'id': 'a'}], None], calls))
    ok, orders = sr.fetch_recent_closed_orders(['AAA'])
    assert ok is True and [o['id'] for o in orders] == ['a']


def test_ok_false_only_when_every_read_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([None, None], calls))
    ok, orders = sr.fetch_recent_closed_orders(['AAA'])
    assert ok is False and orders == []


def test_no_symbols_and_no_unscoped_is_a_clean_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, '_run_cli', _recorder([], calls))
    ok, orders = sr.fetch_recent_closed_orders(None, include_unscoped=False)
    assert ok is False and orders == [] and calls == []
```

- [ ] **Step 2** — Run it and confirm the expected failure (`AttributeError: module 'execution.stop_reattach' has no attribute 'fetch_recent_closed_orders'`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_closed_order_fetch.py -q
```

- [ ] **Step 3** — Implement. Insert into `src/execution/stop_reattach.py` immediately after `_fetch_open_orders` (after line 182, before `def _is_rate_limited`):

```python
_CLOSED_ORDERS_PAGE = 500
_CLOSED_SYMBOL_CHUNK = 100


def fetch_recent_closed_orders(symbols=None, *, include_unscoped: bool = True,
                               page: int = _CLOSED_ORDERS_PAGE,
                               chunk: int = _CLOSED_SYMBOL_CHUNK,
                               timeout: int = 45) -> tuple[bool, list]:
    """Newest-first CLOSED orders with legs, deduped by order id.

    Why NOT the --after-order-id keyset loop _fetch_open_orders uses: that cursor
    is ASCENDING and inception-anchored. On --status open the set is bounded by
    the live book, but on --status closed it starts at the oldest order the
    account ever had and walks forward — the 10-minute exit-fill reporter would
    burn its whole page budget on ancient history and never reach today's fills.
    The CLI exposes no time bound (`order list` takes only --status, --symbols,
    --limit, --direction, --after-order-id, --nested), so the right shape is the
    DEFAULT newest-first window, widened from the old 200 to `page` rows, plus a
    --symbols-scoped read that spends a whole fresh window on just the names we
    care about — the same server-side-filter trick latest_broker_bracket applies
    at :312-315 once the account's history outgrew the unfiltered window.

    ok=False only when EVERY read failed (no coverage at all). A partial failure
    returns ok=True with what was gathered plus a loud warning: partial coverage
    beats none, and the worst case equals the old single-read behavior.
    """
    out: list = []
    seen: set = set()
    any_ok = False

    def _absorb(payload) -> None:
        for o in (payload or []):
            oid = (o or {}).get('id') or (o or {}).get('order_id')
            if oid is not None:
                if oid in seen:
                    continue
                seen.add(oid)
            out.append(o)

    if include_unscoped:
        ok, payload, err = _run_cli(
            ['order', 'list', '--status', 'closed', '--nested', '--limit', str(page)],
            timeout=timeout)
        if ok and isinstance(payload, list):
            any_ok = True
            _absorb(payload)
            if len(payload) >= page:
                log(f'⚠ closed-order window full ({page} rows) — coverage may be '
                    f'truncated; the symbol-scoped reads cover the names that matter')
        else:
            log(f'⚠ closed-order read failed: {(err or {}).get("error", "unknown")}')

    syms = sorted({str(s).strip().upper() for s in (symbols or []) if s})
    for i in range(0, len(syms), chunk):
        group = syms[i:i + chunk]
        ok, payload, err = _run_cli(
            ['order', 'list', '--status', 'closed', '--nested',
             '--limit', str(page), '--symbols', ','.join(group)],
            timeout=timeout)
        if ok and isinstance(payload, list):
            any_ok = True
            _absorb(payload)
        else:
            log(f'⚠ closed-order read for {len(group)} symbol(s) failed: '
                f'{(err or {}).get("error", "unknown")}')
    return any_ok, out
```

- [ ] **Step 4** — Run the task's test plus the touched module's own tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_closed_order_fetch.py tests/execution/test_reattach_from_broker.py tests/execution/test_alpaca_cli_contract.py -q
```

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/execution/stop_reattach.py tests/execution/test_closed_order_fetch.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(broker): bounded newest-first closed-order fetch with --symbols scoping

Stream B prerequisite: afterhours_tp's exit-fill reporter reads closed orders
with --limit 200 and silently drops every fill past row 200. The keyset loop
_fetch_open_orders uses is ascending and inception-anchored, so it is the wrong
shape for --status closed on a 10-minute timer. fetch_recent_closed_orders keeps
the default newest-first window (500 rows) and adds a --symbols-scoped read so a
whole fresh window lands on the names we still hold signals for.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: B1 — broker stop/ah-exit fills close the ledger and arm the cooldown

**Safety:** this changes `run_exit_fill_reporter`, which runs FIRST on every
`openclaw-afterhours-stop-monitor.timer` tick (every 10 min, 04:00–20:00 ET Mon–Fri) — BEFORE
`run_stop_monitor`, the ext-hours stop emulation. Two hard rules follow, both enforced in code
below: (1) the close pass is wrapped so a DB failure logs and returns 0 rather than propagating —
if it raised, the stop emulation would never run that tick and positions would sit unprotected
outside RTH; (2) the `first_run` seed path (no state file yet) must NOT close anything — a
deleted/missing `logs/exit_fills_reported.json` would otherwise mass-close every historical
fill's signals in one tick, the same class as the 2026-05-22 empty-signals blowout. This is spec
item 3, an operator-approved pure bug fix, so it ships WITHOUT a new flag. Inert until merged to main.

**Files:**
- Modify: `src/execution/open_reconcile.py` — `drop_signal_close` signature at lines 68-74, docstring 75-97, `run_date` at line 165, INSERT params tuple at lines 191-201 (both text-anchored in Step 3)
- Modify: `src/execution/afterhours_tp.py` — new helpers above `run_exit_fill_reporter` (line 614), and the body of `run_exit_fill_reporter` (lines 614-655)
- Test: `tests/execution/test_b_stop_fill_close.py` (Create)

**Interfaces:**
- Consumes: `open_reconcile._held_signal_rows(cur, ticker: str) -> list[tuple[str, str]]` (line 528); `stop_reattach.fetch_recent_closed_orders` (Task 1); `stop_reattach._post_alert(msg, channel='data-alerts')` (line 628); `afterhours_tp.classify_exit_fills(orders) -> list` (line 562) which emits `{id, symbol, side, qty, price, level, kind, filled_at}`
- Produces:
  - `open_reconcile.drop_signal_close(cur, signal_id, ticker, closed_price, reason='signal_dropped', *, closed_at=None) -> None`
  - `open_reconcile._coerce_close_date(value, fallback) -> datetime.date`
  - `afterhours_tp._db_conn() -> psycopg2 connection`
  - `afterhours_tp._open_signal_tickers(*, conn_factory=None) -> list[str]`
  - `afterhours_tp._close_signals_for_fill(fill: dict, *, conn_factory=None) -> int`
  - `afterhours_tp._CLOSING_FILL_KINDS = ('stop', 'ah_exit')`
  - `afterhours_tp.run_exit_fill_reporter(dry_run) -> dict` now returns `{'fills_seen', 'reported', 'signals_closed'}`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_b_stop_fill_close.py`:

```python
"""B1 (spec item 3): a broker stop fill must close the ledger, not just post.

engine.update_pnl only infers close_reason='stop_loss' at EOD from a parquet
close crossing the stop, so an intraday broker stop fill left signal_pnl 'open'
and the stop-out cooldown (_load_recent_stopouts) never saw it — the same name
was re-bought on the next cycle. run_exit_fill_reporter now closes every HELD
ledger row on that ticker/side via drop_signal_close(reason='stop_loss').
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import afterhours_tp as ah  # noqa: E402
from execution import open_reconcile as orc  # noqa: E402

_SIGNAL_ROW = ('sig-1', 'S_x', 'ws-1', date(2026, 9, 10), 'LONG',
               100.0, 100.0, date(2026, 9, 11))


class _Cursor:
    """Records every execute; replays one SELECT row for drop_signal_close."""
    def __init__(self, row=_SIGNAL_ROW, held=()):
        self.row = row
        self.held = list(held)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def inserts(self):
        return [c for c in self.calls if c[0].startswith('INSERT INTO signal_pnl')]


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.commits += 1
        return False


# ── drop_signal_close(closed_at=…) ──────────────────────────────────────────

def _closed_at_param(cur):
    """signal_pnl INSERT params: index 3 = pnl_date, index 9 = closed_at."""
    return cur.inserts()[0][1][9]


def _pnl_date_param(cur):
    return cur.inserts()[0][1][3]


def test_closed_at_defaults_to_today():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss')
    assert _closed_at_param(cur) == date.today()


def test_closed_at_accepts_broker_iso_string_with_z():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at='2026-09-11T19:31:02.412Z')
    assert _closed_at_param(cur) == date(2026, 9, 11)


def test_closed_at_accepts_a_datetime():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at=datetime(2026, 9, 11, 19, 31, tzinfo=timezone.utc))
    assert _closed_at_param(cur) == date(2026, 9, 11)


def test_closed_at_garbage_falls_back_to_today():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at='not-a-timestamp')
    assert _closed_at_param(cur) == date.today()


def test_pnl_date_stays_today_so_the_conflict_key_is_unchanged():
    cur = _Cursor()
    orc.drop_signal_close(cur, 'sig-1', 'AAA', 10.0, reason='stop_loss',
                          closed_at='2026-01-02T10:00:00Z')
    assert _pnl_date_param(cur) == date.today()


# ── _close_signals_for_fill ─────────────────────────────────────────────────

def _fill(kind='stop', side='sell', symbol='AAA', price=9.5, oid='f1'):
    return {'id': oid, 'symbol': symbol, 'side': side, 'qty': 10.0,
            'price': price, 'level': 10.0, 'kind': kind,
            'filled_at': '2026-09-11T19:31:02Z'}


def test_stop_fill_closes_matching_open_signals(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows',
                        lambda c, t: [('sig-1', 'LONG'), ('sig-2', 'LONG')])
    n = ah._close_signals_for_fill(_fill(), conn_factory=lambda: _Conn(cur))
    assert n == 2
    reasons = [c[1][10] for c in cur.inserts()]
    prices = [c[1][4] for c in cur.inserts()]
    assert reasons == ['stop_loss', 'stop_loss']
    assert prices == [9.5, 9.5]


def test_sell_exit_does_not_close_short_rows(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows', lambda c, t: [('sig-9', 'SHORT')])
    assert ah._close_signals_for_fill(_fill(), conn_factory=lambda: _Conn(cur)) == 0
    assert cur.inserts() == []


def test_buy_exit_closes_short_rows(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows', lambda c, t: [('sig-9', 'SHORT')])
    assert ah._close_signals_for_fill(_fill(side='buy'),
                                      conn_factory=lambda: _Conn(cur)) == 1


def test_fill_on_ticker_with_no_open_signal_only_logs(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(orc, '_held_signal_rows', lambda c, t: [])
    assert ah._close_signals_for_fill(_fill(), conn_factory=lambda: _Conn(cur)) == 0
    assert cur.inserts() == []


def test_db_failure_returns_zero_and_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError('postgres down')
    assert ah._close_signals_for_fill(_fill(), conn_factory=_boom) == 0


# ── run_exit_fill_reporter wiring ───────────────────────────────────────────

def _wire(monkeypatch, tmp_path, orders, closed):
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(tmp_path / 'seen.json'))
    import execution.stop_reattach as sr
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders',
                        lambda *a, **k: (True, orders))
    monkeypatch.setattr(sr, '_post_alert', lambda msg, channel=None: None)
    monkeypatch.setattr(ah, '_open_signal_tickers', lambda **k: ['AAA'])
    monkeypatch.setattr(ah, '_close_signals_for_fill',
                        lambda f, **k: closed.append(f) or 1)


_STOP_ORDER = [{'id': 'o1', 'symbol': 'AAA', 'side': 'sell', 'status': 'filled',
                'type': 'stop', 'stop_price': '10.00', 'filled_qty': '10',
                'filled_avg_price': '9.50', 'filled_at': '2026-09-11T19:31:02Z'}]


def test_first_run_seeds_without_closing_anything(monkeypatch, tmp_path):
    closed = []
    _wire(monkeypatch, tmp_path, _STOP_ORDER, closed)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['fills_seen'] == 1
    assert stats['reported'] == 0
    assert stats['signals_closed'] == 0
    assert closed == []


def test_second_run_closes_then_third_run_is_a_noop(monkeypatch, tmp_path):
    closed = []
    _wire(monkeypatch, tmp_path, _STOP_ORDER, closed)
    ah.run_exit_fill_reporter(dry_run=False)          # seed
    (tmp_path / 'seen.json').write_text(json.dumps({'seen': []}))  # state exists, empty
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 1 and len(closed) == 1
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['signals_closed'] == 0 and len(closed) == 1


def test_take_profit_fill_is_reported_but_never_closed_as_stop_loss(monkeypatch, tmp_path):
    closed = []
    tp = [{'id': 'o2', 'symbol': 'AAA', 'side': 'sell', 'status': 'filled',
           'type': 'limit', 'order_class': 'oco', 'limit_price': '12.00',
           'filled_qty': '10', 'filled_avg_price': '12.05',
           'filled_at': '2026-09-11T19:31:02Z'}]
    _wire(monkeypatch, tmp_path, tp, closed)
    (tmp_path / 'seen.json').write_text(json.dumps({'seen': []}))
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 1 and stats['signals_closed'] == 0 and closed == []


def test_reporter_scopes_the_fetch_to_open_signal_tickers(monkeypatch, tmp_path):
    seen_args = {}
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(tmp_path / 'seen.json'))
    import execution.stop_reattach as sr

    def _fetch(symbols=None, **k):
        seen_args['symbols'] = symbols
        return True, []
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', _fetch)
    monkeypatch.setattr(sr, '_post_alert', lambda msg, channel=None: None)
    monkeypatch.setattr(ah, '_open_signal_tickers', lambda **k: ['AAA', 'BBB'])
    ah.run_exit_fill_reporter(dry_run=False)
    assert seen_args['symbols'] == ['AAA', 'BBB']
```

- [ ] **Step 2** — Run it and confirm the expected failure (`TypeError: drop_signal_close() got an unexpected keyword argument 'closed_at'` on the first tests, `AttributeError: ... '_close_signals_for_fill'` on the rest):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_b_stop_fill_close.py -q
```

- [ ] **Step 3** — Add `closed_at` to `drop_signal_close`. In `src/execution/open_reconcile.py`, insert this helper immediately before `def drop_signal_close` (line 68):

```python
def _coerce_close_date(value, fallback):
    """Broker fill timestamps arrive as ISO strings ('2026-09-11T19:31:02.4Z'),
    datetimes, or dates. signal_pnl.closed_at is a DATE column (012:64), so
    return a `date`. Anything unparseable falls back rather than raising — this
    runs inside an exit path that must never abort."""
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).strip().replace('Z', '+00:00')).date()
    except (TypeError, ValueError):
        logger.warning('drop_signal_close: unparseable closed_at %r — using %s',
                       value, fallback)
        return fallback
```

Change the signature (lines 68-74) to:

```python
def drop_signal_close(
    cur,
    signal_id: str,
    ticker: str,
    closed_price: float,
    reason: str = 'signal_dropped',
    *,
    closed_at=None,
) -> None:
```

Append to the Args block of the docstring, after the `reason:` line:

```
        closed_at:    when the close actually happened (broker fill timestamp;
                      ISO string / datetime / date). Defaults to today. Only
                      signal_pnl.closed_at moves — pnl_date stays today so the
                      ON CONFLICT (signal_id, pnl_date) key, and therefore
                      same-day idempotency, is unchanged.
```

Replace line 165 (`run_date = date.today()`; the `if target_dt …` header on 166 stays) so the block reads:

```python
    run_date = date.today()
    close_date = _coerce_close_date(closed_at, run_date)
    if target_dt is not None and isinstance(target_dt, date):
```

In the INSERT params tuple, replace the `closed_at` positional — the line currently reading
`run_date,          # closed_at is DATE in signal_pnl schema` — with:

```python
            close_date,        # closed_at is DATE in signal_pnl schema
```

- [ ] **Step 4** — Add the B1 helpers to `src/execution/afterhours_tp.py`, immediately before `def run_exit_fill_reporter` (line 614):

```python
# Fills that mean "the broker closed this position at its stop". A take-profit
# or ah_take_profit fill is a WIN and must never be written as 'stop_loss' —
# that would arm the stop-out cooldown against a name that worked.
_CLOSING_FILL_KINDS = ('stop', 'ah_exit')


def _db_conn():
    import psycopg2
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def _open_signal_tickers(*, conn_factory=None) -> list:
    """Tickers with at least one HELD ledger row — the scope that matters for
    the close pass. A stop fill REMOVES the position from the broker, so the
    position list cannot name it; the still-open signal rows can. Empty list on
    any DB failure: the unscoped newest-first window still backs the Discord post."""
    conn_factory = conn_factory or _db_conn
    try:
        conn = conn_factory()
    except Exception as e:  # noqa: BLE001
        log(f'fill-reporter: DB connect failed ({e}) — unscoped window only')
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ticker FROM execution_signals "
                "WHERE status = 'open' "
                "AND (lifecycle_state IS NULL OR lifecycle_state = 'FILLED') "
                "AND ticker IS NOT NULL")
            return [r[0] for r in (cur.fetchall() or []) if r and r[0]]
    except Exception as e:  # noqa: BLE001
        log(f'fill-reporter: open-signal ticker lookup failed ({e}) — unscoped window only')
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _close_signals_for_fill(fill, *, conn_factory=None) -> int:
    """Close every HELD ledger row on this fill's ticker whose direction matches
    the position the fill exited, with close_reason='stop_loss' at the fill price
    and closed_at = the broker's fill timestamp.

    The broker nets a position per TICKER while the ledger carries one row per
    signal, so all matching rows close — the same reasoning open_reconcile
    ._held_signal_rows documents at :534-537. Exit side maps to direction: a
    'sell' exit closed a LONG, a 'buy' exit closed a SHORT.

    Returns rows closed, and returns 0 rather than raising on ANY failure: this
    runs first on each --monitor tick, before run_stop_monitor, and a DB blip
    must not cost the book its ext-hours stop emulation for that tick."""
    from execution.open_reconcile import _held_signal_rows, drop_signal_close
    conn_factory = conn_factory or _db_conn
    sym = fill.get('symbol') or ''
    want = 'LONG' if (fill.get('side') or '').lower() == 'sell' else 'SHORT'
    try:
        conn = conn_factory()
    except Exception as e:  # noqa: BLE001
        log(f'  ⚠ {sym}: DB connect failed ({e}) — signal close skipped')
        return 0
    n = 0
    try:
        with conn, conn.cursor() as cur:
            for sig_id, direction in _held_signal_rows(cur, sym):
                if (direction or '').upper() != want:
                    continue
                drop_signal_close(cur, sig_id, sym, float(fill['price']),
                                  reason='stop_loss', closed_at=fill.get('filled_at'))
                n += 1
        if n:
            log(f"  ↳ {sym}: closed {n} {want} signal(s) stop_loss @ {float(fill['price']):.2f}")
        else:
            log(f'  ↳ {sym}: no open {want} signal to close (reported only)')
        return n
    except Exception as e:  # noqa: BLE001
        log(f'  ⚠ {sym}: signal close failed ({e}) — reported only')
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 5** — Rewrite `run_exit_fill_reporter` (lines 614-655) to:

```python
def run_exit_fill_reporter(dry_run: bool) -> dict:
    """Report newly-filled exit orders to #trade-reports AND close the ledger
    rows a broker stop / after-hours exit actually closed (B1, spec item 3).

    The FIRST run seeds the seen-set silently so history doesn't flood the
    channel — and, load-bearing, closes NOTHING: a missing or deleted state file
    would otherwise mass-close every historical fill's signals in a single tick
    (the 2026-05-22 empty-signals blowout class). Idempotency has two layers:
    the seen-set file, and drop_signal_close flipping execution_signals to
    'closed' so _held_signal_rows stops returning the row even if the state file
    is lost."""
    from execution.stop_reattach import _post_alert, fetch_recent_closed_orders
    stats = {'fills_seen': 0, 'reported': 0, 'signals_closed': 0}
    ok, orders = fetch_recent_closed_orders(symbols=_open_signal_tickers())
    if not ok:
        log('fill-reporter: order list failed — skipping')
        return stats
    fills = classify_exit_fills(orders)
    stats['fills_seen'] = len(fills)
    state_p = _fills_state_path()
    first_run = not state_p.exists()
    try:
        seen_list = list(json.loads(state_p.read_text()).get('seen', []))
    except (OSError, ValueError):
        seen_list = []
    seen = set(seen_list)
    new = [f for f in fills if f['id'] and f['id'] not in seen]
    for f in new:
        seen.add(f['id'])
        seen_list.append(f['id'])
        if first_run:
            continue                     # seed silently; close NOTHING
        lvl = f" (level {f['level']:.2f})" if f['level'] else ''
        msg = (f"{_EXIT_FILL_LABELS[f['kind']]} {f['symbol']}: "
               f"{f['side'].upper()} {f['qty']:g} @ {f['price']:.2f}{lvl} — "
               f"${f['qty'] * f['price']:,.0f}")
        log(f'  {msg}')
        stats['reported'] += 1
        if not dry_run:
            _post_alert(msg, channel='trade-reports')
            if f['kind'] in _CLOSING_FILL_KINDS:
                stats['signals_closed'] += _close_signals_for_fill(f)
    if not dry_run:
        try:
            state_p.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_p.with_suffix('.tmp')
            tmp.write_text(json.dumps({'seen': seen_list[-800:]}))
            os.replace(tmp, state_p)
        except OSError as e:
            log(f'⚠ fill-reporter state write failed: {e}')
    return stats
```

- [ ] **Step 6** — Run the task's test plus both touched modules' tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_b_stop_fill_close.py tests/execution/test_afterhours_tp.py tests/execution/test_reconcile_broker_closes.py tests/execution/test_sp6_run_reconcile.py tests/execution/test_closed_order_fetch.py -q
```

- [ ] **Step 7** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/execution/open_reconcile.py src/execution/afterhours_tp.py tests/execution/test_b_stop_fill_close.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "fix(ledger): broker stop fills close signal_pnl and arm the stop-out cooldown

B1 / spec item 3. classify_exit_fills already tagged filled stop and ah_exit
legs; run_exit_fill_reporter only posted them to Discord, so signal_pnl stayed
'open' and _load_recent_stopouts never saw the stop — the name was re-bought on
the next cycle. Each classified stop/ah_exit fill now closes every HELD ledger
row on that ticker/side via drop_signal_close(reason='stop_loss') at the fill
price, with the broker's fill timestamp as closed_at (new keyword-only arg;
pnl_date stays today so the conflict key is unchanged). The first-run seed path
closes nothing, and every close is wrapped so a DB blip can never cost the tick
its ext-hours stop emulation. The 200-row closed-order read is replaced by
fetch_recent_closed_orders scoped to the open-signal tickers.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: B2 migration 155 — `broker_fills`, `alpaca_submissions.filled_at`, `signal_pnl.exit_slippage_bps`

**Safety:** migrations are applied in filename sort order by `migrate()`
(`src/database/postgres.js:42-55`), called once from `src/channels/discord/bot.js:1663` on
johnbot start. `ls src/database/migrations | tail -3` shows `152_beta_budget.sql`,
`153_account_epoch.sql`, `154_bench_corr_removal.sql` — 155 is the next free number. Nothing runs
this migration until johnbot restarts AND this branch is merged to main. Every statement is
`IF NOT EXISTS`, so a re-run is a no-op; no DROP, no DELETE, no TRUNCATE (append-only invariant).

**Deliberate deviation:** the spec says "Tests: migration applies".
`tests/database/test_sp7_migrations.py` does that by opening a real psycopg2 connection — this box
is running a live fleet backtest and this plan's constraints forbid any test that reaches the DB.
The substitute is a STATIC DDL-shape test that also cross-checks the declared columns against the
constant the ingest code writes, so code/DDL drift fails the test.

**Files:**
- Create: `src/database/migrations/155_broker_fills.sql`
- Test: `tests/database/test_stream_b_migration_shape.py` (Create)

**Interfaces:**
- Consumes: nothing (pure DDL)
- Produces: table `broker_fills(activity_id TEXT PRIMARY KEY, order_id TEXT, parent_order_id TEXT, client_order_id TEXT, ticker TEXT, side TEXT, order_type TEXT, order_class TEXT, qty NUMERIC, price NUMERIC, filled_at TIMESTAMPTZ, ingested_at TIMESTAMPTZ DEFAULT now())`; column `alpaca_submissions.filled_at TIMESTAMPTZ`; column `signal_pnl.exit_slippage_bps NUMERIC`

- [ ] **Step 1** — Write the failing test file `tests/database/test_stream_b_migration_shape.py`:

```python
"""Stream B migrations 155/156 — static DDL shape (NO database connection).

tests/database/test_sp7_migrations.py proves a migration applies by connecting
to the REAL Postgres. This box runs a live fleet backtest and the Stream B plan
forbids any test that reaches the DB, so Stream B asserts the DDL TEXT instead:
every column the ingest / ownership code writes must be declared, every statement
must be idempotent, and nothing may violate the append-only invariant.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG = ROOT / 'src' / 'database' / 'migrations'

BROKER_FILLS_COLUMNS = (
    'activity_id', 'order_id', 'parent_order_id', 'client_order_id',
    'ticker', 'side', 'order_type', 'order_class', 'qty', 'price',
    'filled_at', 'ingested_at',
)


def _sql(name: str) -> str:
    return (MIG / name).read_text()


def _create_body(sql: str, table: str) -> str:
    start = sql.index(f'CREATE TABLE IF NOT EXISTS {table}')
    return sql[start:sql.index(');', start)]


def test_155_declares_every_broker_fills_column():
    body = _create_body(_sql('155_broker_fills.sql'), 'broker_fills')
    for col in BROKER_FILLS_COLUMNS:
        assert re.search(rf'^\s*{col}\s+\w', body, re.M), f'broker_fills.{col} not declared'


def test_155_activity_id_is_the_primary_key():
    assert re.search(r'activity_id\s+TEXT\s+PRIMARY KEY', _sql('155_broker_fills.sql'))


def test_155_indexes_the_exit_leg_join_keys():
    sql = _sql('155_broker_fills.sql')
    assert 'broker_fills_parent_idx' in sql
    assert 'broker_fills_ticker_idx' in sql


def test_155_adds_filled_at_and_exit_slippage():
    sql = _sql('155_broker_fills.sql')
    assert 'ALTER TABLE alpaca_submissions ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ' in sql
    assert 'ALTER TABLE signal_pnl ADD COLUMN IF NOT EXISTS exit_slippage_bps NUMERIC' in sql


def test_155_is_idempotent_and_append_only():
    sql = _sql('155_broker_fills.sql')
    assert 'CREATE TABLE IF NOT EXISTS' in sql
    upper = sql.upper()
    for stmt in ('DROP ', 'DELETE ', 'TRUNCATE '):
        assert stmt not in upper, f'{stmt.strip()} violates the append-only invariant'


def test_155_is_the_next_free_number():
    existing = sorted(p.name for p in MIG.glob('*.sql'))
    assert '155_broker_fills.sql' in existing
    assert not any(n.startswith('155_') and n != '155_broker_fills.sql' for n in existing)
```

- [ ] **Step 2** — Run it and confirm the expected failure (`FileNotFoundError: … 155_broker_fills.sql`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/database/test_stream_b_migration_shape.py -q
```

- [ ] **Step 3** — Create `src/database/migrations/155_broker_fills.sql`.

> **Re-check the migration number first.** A concurrent session is planning Stream E
> (`docs/superpowers/plans/2026-09-12-qd-stream-e-reliability.md` was untracked in the worktree
> when this plan was written) and E3/E5 may claim numbers. Run
> `cd /root/openclaw/.claude/worktrees/qd-adoptions && ls src/database/migrations | tail -3`
> before creating the file; if 155/156 are taken, shift BOTH Stream B migrations to the next free
> pair and update the filenames, the `_sql(...)` calls in
> `tests/database/test_stream_b_migration_shape.py`, and the citations in Tasks 4-9 and the
> changelog entry together.

Contents:

```sql
-- 155: broker-fill fact table + fill timestamps + exit-leg slippage.
-- Stream B item 14 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:144-166).
--
-- broker_fills is APPEND-ONLY: one row per Alpaca FILL activity, keyed by the
-- activity id the broker assigns. alpaca_reconcile ingests with
-- ON CONFLICT DO NOTHING, so re-running the reconcile step (or the --sweep-stale
-- pass) never duplicates and never rewrites a row.
--
-- The four order-shape columns (parent_order_id, client_order_id, order_type,
-- order_class) are NOT carried by Alpaca activity records, and the REST order
-- model has no parent pointer at all — a leg's parent is only knowable by
-- walking `legs` on a --nested order read. alpaca_reconcile enriches from that
-- read and leaves these NULL when the order fell outside the window.
CREATE TABLE IF NOT EXISTS broker_fills (
  activity_id      TEXT PRIMARY KEY,
  order_id         TEXT,
  parent_order_id  TEXT,
  client_order_id  TEXT,
  ticker           TEXT,
  side             TEXT,
  order_type       TEXT,
  order_class      TEXT,
  qty              NUMERIC,
  price            NUMERIC,
  filled_at        TIMESTAMPTZ,
  ingested_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS broker_fills_filled_at_idx
  ON broker_fills (filled_at DESC);
CREATE INDEX IF NOT EXISTS broker_fills_order_idx
  ON broker_fills (order_id);
CREATE INDEX IF NOT EXISTS broker_fills_parent_idx
  ON broker_fills (parent_order_id) WHERE parent_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS broker_fills_ticker_idx
  ON broker_fills (ticker, filled_at DESC);

-- Broker fill timestamp on the submission ledger. submitted_at already exists
-- (043_alpaca_submissions.sql:25) and reconciled_at (064) records when WE
-- looked, not when the broker filled. latency = filled_at - submitted_at.
ALTER TABLE alpaca_submissions ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ;

-- Exit-leg realized slippage vs the signal's own stop/target level, signed
-- adverse-positive. Nullable: only exit legs we can attribute get a value.
-- execution_signals.fill_slippage_bps (migration 145) is the ENTRY twin.
ALTER TABLE signal_pnl ADD COLUMN IF NOT EXISTS exit_slippage_bps NUMERIC;
```

- [ ] **Step 4** — Run the shape test; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/database/test_stream_b_migration_shape.py -q
```

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/database/migrations/155_broker_fills.sql tests/database/test_stream_b_migration_shape.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(db): migration 155 — broker_fills, alpaca_submissions.filled_at, signal_pnl.exit_slippage_bps

Stream B item 14 schema. broker_fills is append-only, keyed by the broker's own
activity id so ingest dedupes with ON CONFLICT DO NOTHING. Shape test is static
(reads the DDL text, cross-checks the ingest column list) because this box's
pytest reaches the real Postgres and a fleet backtest is running.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: B2 ingest — `broker_fills` from the FILL activity poll + `filled_at` on submissions

**Safety:** `alpaca_reconcile.py` is the pipeline's `reconcile` step
(`pipeline_orchestrator.STEPS`, line 68: `('reconcile', 'alpaca_reconcile')`), spawned as
`python3 src/execution/alpaca_reconcile.py --date <run_date>` by `_resolve_script` (line 509)
after `alpaca` and before `stop_reattach`/`report` in the daily 10:00 ET cycle, and again by
`scripts/redeploy_pipeline.py` on an intraday regime redeploy. The ingest is wrapped in a
SAVEPOINT and a try/except so it can never poison the submission-reconcile transaction, and it
never blocks the step. Writes only INSERT ... ON CONFLICT DO NOTHING. Inert until merged to main
AND johnbot restarts to apply migration 155 (before that the INSERT fails, the savepoint rolls
back, and the step logs and continues).

**Files:**
- Modify: `src/execution/alpaca_reconcile.py` — `collapse_fills` (lines 96-139), `fetch_order_status` (lines 142-183), `_apply_fill` (lines 260-278), `reconcile` (lines 297-401); new module-level helpers after `_mark_rejected` (line 294)
- Test: `tests/execution/test_broker_fills_ingest.py` (Create)

**Interfaces:**
- Consumes: `alpaca_reconcile.fetch_fills_for_date(run_date, *, page_size=100, max_pages=50) -> list` (line 55, already paginated); `stop_reattach.fetch_recent_closed_orders` (Task 1)
- Produces:
  - `alpaca_reconcile._BROKER_FILL_COLUMNS: tuple[str, ...]`
  - `alpaca_reconcile.build_order_meta(orders) -> dict[str, dict]`
  - `alpaca_reconcile.ingest_broker_fills(cur, fills, order_meta=None, *, dry_run=False) -> int`
  - `collapse_fills(...)` per-order dicts gain `'filled_at'`; `fetch_order_status(...)` gains `'filled_at'`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_broker_fills_ingest.py`:

```python
"""B2 ingest: every Alpaca FILL activity lands in broker_fills exactly once.

Alpaca activity records carry activity_id/order_id/symbol/side/qty/price/
transaction_time and NOTHING else — no parent_order_id, no client_order_id, no
order type or class. Those four come from a --nested closed-order read, walked
the same way classify_exit_fills walks legs.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_reconcile as ar  # noqa: E402

MIG = ROOT / 'src' / 'database' / 'migrations' / '155_broker_fills.sql'


class _Cursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchall(self):
        return []

    def close(self):
        pass

    def inserts(self):
        return [c for c in self.calls if c[0].startswith('INSERT INTO broker_fills')]


_ACTIVITIES = [
    {'id': 'act-1', 'order_id': 'o-entry', 'symbol': 'AAA', 'side': 'buy',
     'qty': '60', 'price': '10.00', 'transaction_time': '2026-09-11T13:31:00Z',
     'order_status': 'partial_fill'},
    {'id': 'act-2', 'order_id': 'o-entry', 'symbol': 'AAA', 'side': 'buy',
     'qty': '40', 'price': '10.10', 'transaction_time': '2026-09-11T13:32:00Z',
     'order_status': 'filled'},
    {'id': 'act-3', 'order_id': 'o-stopleg', 'symbol': 'AAA', 'side': 'sell',
     'qty': '100', 'price': '9.40', 'transaction_time': '2026-09-11T19:31:00Z',
     'order_status': 'filled'},
]

_NESTED_ORDERS = [
    {'id': 'o-entry', 'symbol': 'AAA', 'client_order_id': 'oc_AAA_1',
     'type': 'market', 'order_class': 'bracket',
     'legs': [
         {'id': 'o-stopleg', 'symbol': 'AAA', 'client_order_id': 'oc_AAA_1_sl',
          'type': 'stop', 'stop_price': '9.50'},
         {'id': 'o-tpleg', 'symbol': 'AAA', 'client_order_id': 'oc_AAA_1_tp',
          'type': 'limit', 'limit_price': '12.00'},
     ]},
]


# ── collapse_fills / fetch_order_status carry the fill timestamp ────────────

def test_collapse_fills_carries_the_latest_transaction_time():
    out = ar.collapse_fills(_ACTIVITIES)
    assert out['o-entry']['filled_at'] == '2026-09-11T13:32:00Z'
    assert out['o-entry']['status'] == 'filled'
    assert abs(out['o-entry']['qty'] - 100.0) < 1e-9


def test_apply_fill_writes_filled_at_without_clobbering(monkeypatch):
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'filled', 'qty': 100.0, 'avg_price': 10.04,
                    'filled_at': '2026-09-11T13:32:00Z'}, dry_run=False)
    sql, params = cur.calls[0]
    assert 'filled_at=COALESCE(%s::timestamptz, filled_at)' in sql
    assert '2026-09-11T13:32:00Z' in params


def test_apply_fill_tolerates_a_record_without_a_timestamp():
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'partial', 'qty': 1.0, 'avg_price': 2.0}, dry_run=False)
    assert cur.calls[0][1][3] is None


# ── build_order_meta ───────────────────────────────────────────────────────

def test_build_order_meta_assigns_leg_parents():
    meta = ar.build_order_meta(_NESTED_ORDERS)
    assert meta['o-stopleg']['parent_order_id'] == 'o-entry'
    assert meta['o-stopleg']['order_type'] == 'stop'
    assert meta['o-stopleg']['client_order_id'] == 'oc_AAA_1_sl'
    assert meta['o-stopleg']['order_class'] == 'bracket'


def test_build_order_meta_top_level_order_has_no_parent():
    meta = ar.build_order_meta(_NESTED_ORDERS)
    assert meta['o-entry']['parent_order_id'] is None
    assert meta['o-entry']['order_type'] == 'market'


def test_build_order_meta_ignores_garbage_rows():
    assert ar.build_order_meta([None, 'nope', {'no_id': 1}]) == {}


# ── ingest_broker_fills ────────────────────────────────────────────────────

def test_ingest_writes_one_row_per_activity_with_on_conflict_do_nothing():
    cur = _Cursor()
    n = ar.ingest_broker_fills(cur, _ACTIVITIES, ar.build_order_meta(_NESTED_ORDERS))
    assert n == 3 and len(cur.inserts()) == 3
    sql, params = cur.inserts()[2]
    assert 'ON CONFLICT (activity_id) DO NOTHING' in sql
    assert params[0] == 'act-3'
    assert params[1] == 'o-stopleg'
    assert params[2] == 'o-entry'          # parent_order_id from the nested walk
    assert params[4] == 'AAA' and params[5] == 'sell'
    assert params[6] == 'stop'
    assert abs(params[8] - 100.0) < 1e-9 and abs(params[9] - 9.40) < 1e-9
    assert params[10] == '2026-09-11T19:31:00Z'


def test_ingest_leaves_order_shape_null_when_meta_is_missing():
    cur = _Cursor()
    ar.ingest_broker_fills(cur, _ACTIVITIES, {})
    assert cur.inserts()[0][1][2] is None
    assert cur.inserts()[0][1][6] is None


def test_ingest_skips_rows_without_an_activity_id():
    cur = _Cursor()
    assert ar.ingest_broker_fills(cur, [{'order_id': 'x', 'qty': '1', 'price': '1'}], {}) == 0
    assert cur.inserts() == []


def test_ingest_skips_rows_with_unparseable_numbers():
    cur = _Cursor()
    assert ar.ingest_broker_fills(cur, [{'id': 'a', 'qty': 'NaNsense', 'price': None}], {}) == 0


def test_ingest_dry_run_writes_nothing():
    cur = _Cursor()
    assert ar.ingest_broker_fills(cur, _ACTIVITIES, {}, dry_run=True) == 3
    assert cur.inserts() == []


def test_ingest_column_list_matches_migration_155():
    body = MIG.read_text()
    body = body[body.index('CREATE TABLE IF NOT EXISTS broker_fills'):]
    body = body[:body.index(');')]
    for col in ar._BROKER_FILL_COLUMNS:
        assert re.search(rf'^\s*{col}\s+\w', body, re.M), f'{col} not in migration 155'
    assert 'ingested_at' not in ar._BROKER_FILL_COLUMNS, 'ingested_at is a DB default'
```

- [ ] **Step 2** — Run it and confirm the expected failure (`KeyError: 'filled_at'` then `AttributeError: … 'build_order_meta'`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_broker_fills_ingest.py -q
```

- [ ] **Step 3** — Carry the fill timestamp. In `src/execution/alpaca_reconcile.py`:

In `collapse_fills`, change the final assembly loop (lines 126-139) so each output dict carries
the latest transaction time:

```python
    out = {}
    for oid, rec in by_oid.items():
        cq = rec['cum_qty']
        avg_price = (rec['notional'] / cq) if cq > 0 else 0.0
        status = 'filled' if rec['order_status'] == 'filled' else 'partial'
        out[oid] = {
            'qty':       cq,
            'avg_price': avg_price,
            'status':    status,
            # B2: the broker's own fill timestamp — latency vs submitted_at, and
            # the ordering key for the broker_fills ledger.
            'filled_at': rec['last_seen'] or None,
        }
    return out
```

In `fetch_order_status`, add the order object's `filled_at` to both terminal-fill returns
(lines 176-180) and leave the rejected branch without one:

```python
    if status == 'filled':
        return {'qty': filled_qty, 'avg_price': avg_price, 'status': 'filled',
                'filled_at': o.get('filled_at')}
    if status == 'partially_filled':
        return {'qty': filled_qty, 'avg_price': avg_price, 'status': 'partial',
                'filled_at': o.get('filled_at')}
```

In `_apply_fill`, persist it without ever overwriting a known timestamp with NULL:

```python
    cur.execute("""
        UPDATE alpaca_submissions
        SET broker_status=%s,
            filled_qty=%s,
            filled_avg_price=%s,
            filled_at=COALESCE(%s::timestamptz, filled_at),
            reconciled_at=NOW()
        WHERE id=%s
    """, (rec['status'], rec['qty'], rec['avg_price'], rec.get('filled_at'), sub_id))
```

- [ ] **Step 4** — Add the ledger helpers to `src/execution/alpaca_reconcile.py`, immediately after `_mark_rejected` (line 294, before `def reconcile`):

```python
# ── B2: broker_fills fact table (spec item 14) ──────────────────────────────
# Column order of the INSERT below; ingested_at is a DB default and is NOT here.
_BROKER_FILL_COLUMNS = (
    'activity_id', 'order_id', 'parent_order_id', 'client_order_id',
    'ticker', 'side', 'order_type', 'order_class', 'qty', 'price', 'filled_at',
)

_BROKER_FILL_INSERT = (
    'INSERT INTO broker_fills '
    '(activity_id, order_id, parent_order_id, client_order_id, ticker, '
    ' side, order_type, order_class, qty, price, filled_at) '
    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::timestamptz) '
    'ON CONFLICT (activity_id) DO NOTHING'
)


def build_order_meta(orders) -> dict:
    """{order_id: {parent_order_id, client_order_id, order_type, order_class}}
    from a --nested order list.

    Alpaca FILL ACTIVITY records carry none of these four fields, and the REST
    order model has no parent pointer at all — a leg's parent is only knowable
    by walking `legs` (the same walk classify_exit_fills and
    alpaca_replace_stop.find_stop_loss_leg do). A leg inherits its enclosing
    order's class when it declares none; a top-level order's parent is None."""
    meta: dict = {}
    for top in (orders or []):
        stack = [(top, None)]
        while stack:
            o, parent = stack.pop()
            if not isinstance(o, dict):
                continue
            for leg in (o.get('legs') or []):
                stack.append((leg, o))
            oid = o.get('id') or o.get('order_id')
            if not oid:
                continue
            meta[oid] = {
                'parent_order_id': (parent or {}).get('id'),
                'client_order_id': o.get('client_order_id'),
                'order_type': (o.get('type') or o.get('order_type') or None),
                'order_class': (o.get('order_class')
                                or (parent or {}).get('order_class') or None),
            }
    return meta


def ingest_broker_fills(cur, fills, order_meta=None, *, dry_run: bool = False) -> int:
    """Append every FILL activity to broker_fills (migration 155).

    Keyed by the broker's own activity id with ON CONFLICT DO NOTHING, so
    re-running the reconcile step — or the --sweep-stale pass — never duplicates
    and never rewrites a row (append-only invariant). Returns rows offered.

    The counted NULL-parent log line is deliberate: the exit-leg slippage join
    in backfill_exit_slippage keys on parent_order_id, so a closed-order window
    that stopped covering our orders would otherwise show up only as a silent
    `exit n=0` in the daily digest."""
    order_meta = order_meta or {}
    n = 0
    n_no_parent = 0
    for f in (fills or []):
        aid = f.get('id')
        if not aid:
            continue
        try:
            qty = float(f.get('qty') or 0)
            price = float(f.get('price') or 0)
        except (TypeError, ValueError):
            continue
        oid = f.get('order_id')
        m = order_meta.get(oid) or {}
        if not m.get('parent_order_id'):
            n_no_parent += 1
        n += 1
        if dry_run:
            continue
        cur.execute(_BROKER_FILL_INSERT, (
            aid, oid, m.get('parent_order_id'), m.get('client_order_id'),
            f.get('symbol'), (f.get('side') or '').lower() or None,
            m.get('order_type'), m.get('order_class'),
            qty, price, f.get('transaction_time'),
        ))
    log(f'broker_fills: {n} fill activity row(s) offered '
        f'({n_no_parent} without a parent_order_id)'
        f'{" (DRY-RUN)" if dry_run else ""}')
    return n
```

- [ ] **Step 5** — Wire the ingest into `reconcile()`. In `src/execution/alpaca_reconcile.py`, insert this block immediately before the `if not dry_run:` / `conn.commit()` pair at the end of `reconcile` (currently lines 396-397):

```python
    # ── B2: append the raw fill activities to the broker_fills ledger ───────
    # The enrichment read is symbol-scoped to today's fill symbols: the four
    # order-shape columns are not on activity records, and a --nested closed
    # order list is the only place a leg's parent is visible. Savepoint-isolated
    # so a missing migration or a broker hiccup can never poison the submission
    # reconcile above — that is this step's critical path.
    try:
        cur.execute('SAVEPOINT sp_broker_fills')
        from execution.stop_reattach import fetch_recent_closed_orders
        _syms = sorted({f.get('symbol') for f in fills if f.get('symbol')})
        _ok_meta, _orders = fetch_recent_closed_orders(_syms, include_unscoped=False)
        ingest_broker_fills(cur, fills, build_order_meta(_orders) if _ok_meta else {},
                            dry_run=dry_run)
        cur.execute('RELEASE SAVEPOINT sp_broker_fills')
    except Exception as exc:  # noqa: BLE001
        log(f'broker_fills ingest skipped ({type(exc).__name__}: {exc})')
        try:
            cur.execute('ROLLBACK TO SAVEPOINT sp_broker_fills')
            cur.execute('RELEASE SAVEPOINT sp_broker_fills')
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 6** — Run the task's test plus the touched module's tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_broker_fills_ingest.py tests/execution/test_alpaca_reconcile.py tests/execution/test_alpaca_reconcile_sweep.py tests/database/test_stream_b_migration_shape.py -q
```

- [ ] **Step 7** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/execution/alpaca_reconcile.py tests/execution/test_broker_fills_ingest.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(reconcile): ingest broker FILL activities into broker_fills + record filled_at

Stream B item 14. The reconcile step already paged every FILL activity for the
cycle and then threw the raw rows away after collapsing them per order. They now
land in broker_fills keyed by the broker's activity id (ON CONFLICT DO NOTHING),
enriched with parent_order_id / client_order_id / type / class from a
symbol-scoped --nested closed-order read, because activity records carry none of
those and the REST order model has no parent pointer. alpaca_submissions.filled_at
is written from the same activities with COALESCE so a later pass never clobbers
a known timestamp with NULL. Savepoint-isolated; the submission reconcile is the
step's critical path and cannot be poisoned by this.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: B2 exit-leg join — `signal_pnl.exit_slippage_bps`

**Safety:** runs inside the same `reconcile` step as Task 4, in the same SAVEPOINT-isolated block,
after the ingest. UPDATE-only, and only on rows where `exit_slippage_bps IS NULL`, so it is
idempotent and never rewrites a value. Inert until merged to main and migration 155 applied.

**Files:**
- Modify: `src/execution/alpaca_reconcile.py` — new functions after `ingest_broker_fills` (added in Task 4); call site inside the Task 4 SAVEPOINT block in `reconcile`
- Test: `tests/execution/test_exit_slippage.py` (Create)

**Interfaces:**
- Consumes: `broker_fills` (migration 155); `alpaca_submissions.alpaca_order_id`, `.run_date`, `.ticker`, `.strategy_id`; `execution_signals.target_date`, `.ticker`, `.strategy_id`, `.direction`, `.stop_loss`, `.target_1`; `signal_pnl.signal_id`, `.pnl_date`, `.exit_slippage_bps`
- Produces:
  - `alpaca_reconcile.exit_level_kind(order_type, client_order_id) -> str` (`'stop'` | `'target'`)
  - `alpaca_reconcile.exit_slippage_bps(direction, level, price) -> float | None`
  - `alpaca_reconcile.plan_exit_slippage(rows) -> list[tuple[str, object, float]]`
  - `alpaca_reconcile.backfill_exit_slippage(cur, run_date, *, lookback_days=5, dry_run=False) -> int`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_exit_slippage.py`:

```python
"""B2 exit-leg slippage: what the exit actually cost vs the level it aimed at.

Sign convention matches execution_signals.fill_slippage_bps (migration 145):
dir_sign * (level - price) / level * 1e4, dir_sign = +1 LONG / -1 SHORT, so a
POSITIVE number always means "worse than the level we wanted".
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_reconcile as ar  # noqa: E402


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def updates(self):
        return [c for c in self.calls if c[0].startswith('UPDATE signal_pnl')]


# ── which level was this exit aiming at ────────────────────────────────────

def test_ahsx_coid_scores_against_the_stop_not_the_target():
    """ahsx_ exits are marketable LIMITS emulating a stop (classify_exit_fills
    tags them 'ah_exit'); typing alone would score them against target_1."""
    assert ar.exit_level_kind('limit', 'ahsx_AAA_1') == 'stop'


def test_ahtp_coid_scores_against_the_target():
    assert ar.exit_level_kind('limit', 'ahtp_AAA_1') == 'target'


def test_stop_and_stop_limit_order_types_score_against_the_stop():
    assert ar.exit_level_kind('stop', None) == 'stop'
    assert ar.exit_level_kind('STOP_LIMIT', '') == 'stop'


def test_a_plain_limit_leg_scores_against_the_target():
    assert ar.exit_level_kind('limit', 'oc_AAA_1_tp') == 'target'


# ── signed adverse-positive bp ─────────────────────────────────────────────

def test_long_exit_below_its_level_is_positive_adverse():
    assert abs(ar.exit_slippage_bps('LONG', 10.0, 9.95) - 50.0) < 1e-6


def test_long_exit_above_its_level_is_favourable_negative():
    assert abs(ar.exit_slippage_bps('LONG', 10.0, 10.05) + 50.0) < 1e-6


def test_short_exit_above_its_level_is_positive_adverse():
    assert abs(ar.exit_slippage_bps('SHORT', 10.0, 10.05) - 50.0) < 1e-6


def test_short_exit_below_its_level_is_favourable_negative():
    assert abs(ar.exit_slippage_bps('SHORT', 10.0, 9.95) + 50.0) < 1e-6


def test_missing_or_nonpositive_inputs_return_none():
    assert ar.exit_slippage_bps('LONG', None, 10.0) is None
    assert ar.exit_slippage_bps('LONG', 0.0, 10.0) is None
    assert ar.exit_slippage_bps('LONG', 10.0, None) is None
    assert ar.exit_slippage_bps('LONG', 10.0, 'nope') is None


# ── plan_exit_slippage ─────────────────────────────────────────────────────

def _row(otype, coid, price, direction='LONG', stop=10.0, tgt=12.0,
         sig='sig-1', pnl=date(2026, 9, 11), aid='act-3'):
    return (aid, otype, coid, price, sig, direction, stop, tgt, pnl)


def test_plan_uses_the_stop_for_a_stop_leg():
    plan = ar.plan_exit_slippage([_row('stop', 'oc_1_sl', 9.95)])
    assert plan == [('sig-1', date(2026, 9, 11), 50.0)]


def test_plan_uses_the_target_for_a_tp_leg():
    plan = ar.plan_exit_slippage([_row('limit', 'oc_1_tp', 11.94)])
    assert plan[0][0] == 'sig-1'
    assert abs(plan[0][2] - 50.0) < 1e-6


def test_plan_uses_the_stop_for_an_ahsx_limit():
    plan = ar.plan_exit_slippage([_row('limit', 'ahsx_AAA_1', 9.95)])
    assert abs(plan[0][2] - 50.0) < 1e-6


def test_plan_skips_rows_with_no_usable_level():
    assert ar.plan_exit_slippage([_row('stop', 'oc_1_sl', 9.95, stop=None)]) == []


# ── backfill_exit_slippage ─────────────────────────────────────────────────

def test_backfill_updates_only_null_rows_and_releases_its_savepoint():
    cur = _Cursor([_row('stop', 'oc_1_sl', 9.95)])
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 1
    sql, params = cur.updates()[0]
    assert 'exit_slippage_bps IS NULL' in sql
    assert params == (50.0, 'sig-1', date(2026, 9, 11))
    assert any(c[0] == 'RELEASE SAVEPOINT sp_exit_slip' for c in cur.calls)


def test_backfill_dry_run_plans_but_writes_nothing():
    cur = _Cursor([_row('stop', 'oc_1_sl', 9.95)])
    assert ar.backfill_exit_slippage(cur, '2026-09-11', dry_run=True) == 1
    assert cur.updates() == []


def test_backfill_rolls_back_and_returns_zero_on_failure():
    class _Boom(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.strip().upper().startswith('SELECT'):
                raise RuntimeError('relation broker_fills does not exist')
    cur = _Boom()
    assert ar.backfill_exit_slippage(cur, '2026-09-11') == 0
    assert any(c[0] == 'ROLLBACK TO SAVEPOINT sp_exit_slip' for c in cur.calls)
```

- [ ] **Step 2** — Run it and confirm the expected failure (`AttributeError: … 'exit_level_kind'`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_exit_slippage.py -q
```

- [ ] **Step 3** — Implement. Add to `src/execution/alpaca_reconcile.py` immediately after `ingest_broker_fills`:

```python
_EXIT_CANDIDATE_SQL = """
    SELECT bf.activity_id, bf.order_type, bf.client_order_id, bf.price,
           es.id, es.direction, es.stop_loss, es.target_1, sp.pnl_date
      FROM broker_fills bf
      JOIN alpaca_submissions s ON s.alpaca_order_id = bf.parent_order_id
      JOIN execution_signals es ON es.target_date = s.run_date
                               AND es.ticker = s.ticker
                               AND es.strategy_id = s.strategy_id
      JOIN LATERAL (
            SELECT pnl_date, exit_slippage_bps
              FROM signal_pnl
             WHERE signal_id = es.id
             ORDER BY pnl_date DESC
             LIMIT 1
           ) sp ON TRUE
     WHERE bf.parent_order_id IS NOT NULL
       AND bf.filled_at >= %s::date - %s
       AND sp.exit_slippage_bps IS NULL
"""


def exit_level_kind(order_type, client_order_id) -> str:
    """'stop' or 'target' — which bracket level this exit fill was aiming at.

    client_order_id WINS over order_type: the after-hours monitor's ahsx_ exits
    are marketable LIMITS that emulate a stop (afterhours_tp.classify_exit_fills
    tags them 'ah_exit'), so typing alone would score them against target_1 and
    report a 20 % "slippage" on every emulated stop."""
    coid = str(client_order_id or '')
    if coid.startswith('ahsx_'):
        return 'stop'
    if coid.startswith('ahtp_'):
        return 'target'
    if str(order_type or '').lower() in ('stop', 'stop_limit'):
        return 'stop'
    return 'target'


def exit_slippage_bps(direction, level, price):
    """Signed adverse-positive slippage of an exit fill vs its intended level.

    LONG exits (sell): filling BELOW the level is adverse -> +bp.
    SHORT exits (buy): filling ABOVE the level is adverse -> +bp.
    Both collapse to dir_sign * (level - price) / level * 10000 with
    dir_sign = +1 for LONG, -1 for SHORT — the same convention
    execution_signals.fill_slippage_bps uses for entries (migration 145,
    parity_mark.backfill_broker_fill_truth:317-322).

    None when the level or the price is missing or non-positive."""
    try:
        lvl = float(level)
        px = float(price)
    except (TypeError, ValueError):
        return None
    if lvl <= 0 or px <= 0:
        return None
    sign = 1.0 if str(direction or '').upper() in ('LONG', 'BUY', 'BUY_VOL') else -1.0
    return sign * (lvl - px) / lvl * 10000.0


def plan_exit_slippage(rows) -> list:
    """Pure: candidate rows from _EXIT_CANDIDATE_SQL -> [(signal_id, pnl_date, bps)].
    Row shape: (activity_id, order_type, client_order_id, price, signal_id,
    direction, stop_loss, target_1, pnl_date)."""
    out = []
    for r in (rows or []):
        (_aid, otype, coid, price, sig_id, direction, stop_loss, target_1, pnl_date) = r
        level = stop_loss if exit_level_kind(otype, coid) == 'stop' else target_1
        bps = exit_slippage_bps(direction, level, price)
        if bps is None:
            continue
        out.append((sig_id, pnl_date, round(bps, 4)))
    return out


def backfill_exit_slippage(cur, run_date, *, lookback_days: int = 5,
                           dry_run: bool = False) -> int:
    """Attribute broker exit-leg fills to signals and persist exit_slippage_bps
    on each signal's LATEST signal_pnl row (migration 155).

    Attribution: broker_fills.parent_order_id = alpaca_submissions.alpaca_order_id
    identifies the submission whose bracket produced this exit leg, and the
    submission maps to its signal by (run_date -> target_date, ticker,
    strategy_id) — the same key parity_mark.backfill_broker_fill_truth:323-332
    uses for the entry twin. Idempotent: only rows still NULL are written.
    Savepoint-isolated; returns rows planned (0 on any failure).

    dry_run reads and reports but issues no UPDATE — reconcile()'s docstring
    promises dry-run "exits cleanly without touching the DB", and
    PIPELINE_DRY_RUN=1 appends --dry-run to every pipeline step
    (pipeline_orchestrator._resolve_script:491-496), so that path is reachable."""
    cur.execute('SAVEPOINT sp_exit_slip')
    try:
        cur.execute(_EXIT_CANDIDATE_SQL, (run_date, int(lookback_days)))
        plan = plan_exit_slippage(cur.fetchall() or [])
        if not dry_run:
            for sig_id, pnl_date, bps in plan:
                cur.execute(
                    'UPDATE signal_pnl SET exit_slippage_bps = %s '
                    'WHERE signal_id = %s AND pnl_date = %s AND exit_slippage_bps IS NULL',
                    (bps, sig_id, pnl_date))
        cur.execute('RELEASE SAVEPOINT sp_exit_slip')
        if plan:
            log(f'exit slippage: {len(plan)} exit-leg bp value(s)'
                f'{" (DRY-RUN, not written)" if dry_run else " persisted"}')
        return len(plan)
    except Exception as exc:  # noqa: BLE001
        log(f'exit slippage backfill failed ({type(exc).__name__}: {exc}) — skipped')
        try:
            cur.execute('ROLLBACK TO SAVEPOINT sp_exit_slip')
            cur.execute('RELEASE SAVEPOINT sp_exit_slip')
        except Exception:  # noqa: BLE001
            pass
        return 0
```

- [ ] **Step 4** — Call it from `reconcile()`. Inside the Task 4 SAVEPOINT block, add the
backfill on the line after the `ingest_broker_fills(...)` call and before
`cur.execute('RELEASE SAVEPOINT sp_broker_fills')`:

```python
        backfill_exit_slippage(cur, run_date, dry_run=dry_run)
```

- [ ] **Step 5** — Run the task's test plus the touched module's tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_exit_slippage.py tests/execution/test_broker_fills_ingest.py tests/execution/test_alpaca_reconcile.py tests/execution/test_alpaca_reconcile_sweep.py -q
```

- [ ] **Step 6** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/execution/alpaca_reconcile.py tests/execution/test_exit_slippage.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(reconcile): exit-leg slippage vs the signal's own stop/target level

Stream B item 14. A broker_fills row whose parent_order_id matches a submission's
alpaca_order_id IS that signal's exit; score it against stop_loss or target_1 and
persist signed adverse-positive bp on the signal's latest signal_pnl row. Level
selection keys on client_order_id BEFORE order_type: ahsx_ exits are marketable
limits emulating a stop, so typing alone would score every emulated stop against
the take-profit. Idempotent (NULL rows only) and savepoint-isolated.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: B2 digest — the `fill_slippage:` line with verdict bands from our own half-spread model

**Safety:** `send_report.py` is the pipeline's `report` step (`pipeline_orchestrator.STEPS` line
69), which posts the daily #trade-reports digest. The new line is appended exactly the way the
`bench_realized` line is (`send_report.py:659-667`): inside its own `try/except`, report-only,
fail-open — an exception prints a skip note and the digest posts without the line. Gates nothing,
routes no orders. Inert until merged to main; the line renders `n/a` until migration 155 has been
applied and a reconcile step has populated the columns.

**Files:**
- Create: `src/execution/fill_slippage.py`
- Modify: `src/execution/send_report.py` — insert after the `bench_realized` block (lines 659-667), before the `if dry_run or (not wh_signals and not wh_reports):` at line 669
- Test: `tests/execution/test_fill_slippage_digest.py` (Create)

**Interfaces:**
- Consumes: `backtest.unified_backtest.load_ticker_cost_bps() -> dict | None` (line 101, reads `data/derived/ticker_cost_bps.json`, kill switch `OPENCLAW_BT_SPREAD_COSTS=0`); `execution_signals.fill_slippage_bps` + `.broker_fill_price` (migration 145); `signal_pnl.exit_slippage_bps` + `.closed_price` (migration 155); `alpaca_submissions.filled_qty`, `.filled_at`, `.submitted_at`
- Produces:
  - `fill_slippage.verdict(median_bps, modelled_bps) -> str`
  - `fill_slippage.modelled_median_bps(tickers, cost_bps=None) -> float | None`
  - `fill_slippage.summarize(entry_rows, exit_rows, cost_bps=None) -> dict`
  - `fill_slippage.format_line(st: dict, run_date) -> str`
  - `fill_slippage.load_entry_rows(conn, run_date) -> list[dict]`
  - `fill_slippage.load_exit_rows(conn, run_date) -> list[dict]`
  - `fill_slippage.fill_slippage_line(run_date, *, conn=None, cost_bps=None) -> str | None`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_fill_slippage_digest.py`:

```python
"""B2 digest: the fill_slippage: line, and its verdict against OUR OWN model.

The verdict compares realized median entry bp to the per-ticker one-way
half-spread in data/derived/ticker_cost_bps.json (unified_backtest
.load_ticker_cost_bps) — never a foreign asset class's bands. n=0 renders 'n/a'
rather than a zero that reads as "no slippage today".
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import fill_slippage as fs  # noqa: E402

COSTS = {'AAA': 4.0, 'BBB': 6.0, 'CCC': 100.0}

ENTRY = [
    {'ticker': 'AAA', 'bps': 4.0, 'notional_usd': 10_000.0, 'latency_s': 2.0},
    {'ticker': 'AAA', 'bps': 6.0, 'notional_usd': 20_000.0, 'latency_s': 4.0},
    {'ticker': 'BBB', 'bps': 20.0, 'notional_usd': 5_000.0, 'latency_s': 6.0},
]
EXIT = [
    {'ticker': 'AAA', 'bps': 10.0, 'notional_usd': 30_000.0},
    {'ticker': 'BBB', 'bps': -2.0, 'notional_usd': 5_000.0},
]


# ── verdict bands ──────────────────────────────────────────────────────────

def test_verdict_ok_at_or_below_1_5x_modelled():
    assert fs.verdict(7.5, 5.0) == 'OK'
    assert fs.verdict(5.0, 5.0) == 'OK'


def test_verdict_warn_between_1_5x_and_3x():
    assert fs.verdict(7.6, 5.0) == 'WARN'
    assert fs.verdict(15.0, 5.0) == 'WARN'


def test_verdict_fail_above_3x():
    assert fs.verdict(15.1, 5.0) == 'FAIL'


def test_verdict_na_without_a_model_or_a_median():
    assert fs.verdict(None, 5.0) == 'n/a'
    assert fs.verdict(7.5, None) == 'n/a'
    assert fs.verdict(7.5, 0.0) == 'n/a'


# ── modelled median over the tickers we actually traded ────────────────────

def test_modelled_median_uses_only_traded_tickers():
    assert fs.modelled_median_bps(['AAA', 'BBB'], cost_bps=COSTS) == 5.0


def test_modelled_median_none_when_the_artifact_covers_nothing():
    assert fs.modelled_median_bps(['ZZZ'], cost_bps=COSTS) is None
    assert fs.modelled_median_bps(['AAA'], cost_bps={}) is None


# ── summarize ──────────────────────────────────────────────────────────────

def test_summarize_computes_n_mean_median_p90():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    assert st['entry_n'] == 3
    assert abs(st['entry_mean'] - 10.0) < 1e-9
    assert abs(st['entry_median'] - 6.0) < 1e-9
    assert abs(st['entry_p90'] - 20.0) < 1e-9
    assert st['exit_n'] == 2
    assert abs(st['exit_mean'] - 4.0) < 1e-9


def test_summarize_latency_median_is_over_entry_rows():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    assert abs(st['latency_median_s'] - 4.0) < 1e-9


def test_summarize_cost_usd_is_the_signed_bp_weighted_sum():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    # entry: 4bp*10k + 6bp*20k + 20bp*5k = 4 + 12 + 10 = 26
    # exit:  10bp*30k + (-2bp)*5k      = 30 - 1        = 29
    assert abs(st['cost_usd'] - 55.0) < 1e-6


def test_summarize_verdict_uses_the_modelled_median():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    # One modelled value PER ENTRY ROW (AAA twice, BBB once), so the model is
    # weighted the same way the realized median is: median(4, 4, 6) = 4.0.
    assert abs(st['modelled_median'] - 4.0) < 1e-9
    assert st['verdict'] == 'OK'                 # 6.0 / 4.0 = 1.5 <= OK_MULT


def test_summarize_verdict_fails_when_realized_dwarfs_the_model():
    entry = [{'ticker': 'AAA', 'bps': 40.0, 'notional_usd': 1_000.0, 'latency_s': 1.0}]
    st = fs.summarize(entry, [], cost_bps=COSTS)
    assert st['verdict'] == 'FAIL'               # 40 / 4 = 10x


def test_zero_rows_render_na_everywhere():
    st = fs.summarize([], [], cost_bps=COSTS)
    line = fs.format_line(st, '2026-09-15')
    assert line.startswith('fill_slippage: ')
    assert 'entry n=0 mean=n/a median=n/a p90=n/a' in line
    assert 'exit n=0 mean=n/a' in line
    assert 'latency_med=n/a' in line
    assert 'cost=$0' in line
    assert 'verdict=n/a' in line
    assert 'asof=2026-09-15' in line


def test_format_line_renders_the_populated_case():
    st = fs.summarize(ENTRY, EXIT, cost_bps=COSTS)
    line = fs.format_line(st, '2026-09-15')
    assert line.startswith('fill_slippage: entry n=3 ')
    assert 'median=6.0bp' in line
    assert 'p90=20.0bp' in line
    assert 'exit n=2 mean=4.0bp' in line
    assert 'latency_med=4.0s' in line
    assert 'modelled_med=4.0bp' in line
    assert 'verdict=OK' in line


def test_fill_slippage_line_returns_none_on_any_failure(monkeypatch):
    monkeypatch.setattr(fs, 'load_entry_rows',
                        lambda conn, rd: (_ for _ in ()).throw(RuntimeError('db down')))
    assert fs.fill_slippage_line('2026-09-15', conn=object()) is None


def test_fill_slippage_line_uses_the_injected_connection(monkeypatch):
    monkeypatch.setattr(fs, 'load_entry_rows', lambda conn, rd: list(ENTRY))
    monkeypatch.setattr(fs, 'load_exit_rows', lambda conn, rd: list(EXIT))
    line = fs.fill_slippage_line('2026-09-15', conn=object(), cost_bps=COSTS)
    assert line.startswith('fill_slippage: entry n=3 ')
```

- [ ] **Step 2** — Run it and confirm the expected failure (`ModuleNotFoundError: No module named 'execution.fill_slippage'`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_fill_slippage_digest.py -q
```

- [ ] **Step 3** — Create `src/execution/fill_slippage.py`:

```python
"""fill_slippage.py — daily realized-slippage line for #trade-reports.

Stream B item 14 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:144-166).
Report-only: gates nothing, routes nothing, and never raises out of
fill_slippage_line (returns None on any failure, logged) — same contract as
bench_realized.bench_realized_line.

Entry bp: execution_signals.fill_slippage_bps (migration 145, written by
parity_mark.backfill_broker_fill_truth) vs the official close.
Exit bp:  signal_pnl.exit_slippage_bps (migration 155, written by
alpaca_reconcile.backfill_exit_slippage) vs the signal's own stop/target.
Latency: alpaca_submissions.filled_at - submitted_at.

The verdict compares OUR MEDIAN realized entry bp against OUR OWN modelled
one-way half-spread for the same tickers (data/derived/ticker_cost_bps.json via
backtest.unified_backtest.load_ticker_cost_bps:101-125) — never a foreign asset
class's bands: OK <= 1.5x modelled, WARN <= 3x, FAIL above.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OK_MULT = 1.5
WARN_MULT = 3.0


def _median(xs):
    vals = sorted(float(x) for x in xs)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _mean(xs):
    vals = [float(x) for x in xs]
    return (sum(vals) / len(vals)) if vals else None


def _pctile(xs, q: float):
    """Nearest-rank percentile — stable on the 1-20 row samples a daily cycle
    produces, where linear interpolation invents values we never paid."""
    vals = sorted(float(x) for x in xs)
    if not vals:
        return None
    import math
    k = max(1, math.ceil(q * len(vals)))
    return vals[min(k, len(vals)) - 1]


def verdict(median_bps, modelled_bps) -> str:
    """'OK' | 'WARN' | 'FAIL' | 'n/a' — realized median vs our own cost model."""
    if median_bps is None or modelled_bps is None:
        return 'n/a'
    try:
        model = float(modelled_bps)
        realized = float(median_bps)
    except (TypeError, ValueError):
        return 'n/a'
    if model <= 0:
        return 'n/a'
    ratio = realized / model
    if ratio <= OK_MULT:
        return 'OK'
    if ratio <= WARN_MULT:
        return 'WARN'
    return 'FAIL'


def modelled_median_bps(tickers, cost_bps=None):
    """Median modelled one-way half-spread over the tickers we actually traded,
    or None when the artifact is missing or covers none of them."""
    if cost_bps is None:
        from backtest.unified_backtest import load_ticker_cost_bps
        cost_bps = load_ticker_cost_bps()
    if not cost_bps:
        return None
    vals = [float(cost_bps[t]) for t in tickers if t in cost_bps]
    return _median(vals) if vals else None


def summarize(entry_rows, exit_rows, cost_bps=None) -> dict:
    """Pure: rows -> the stats dict format_line renders.

    entry_rows: [{'ticker', 'bps', 'notional_usd', 'latency_s'}]
    exit_rows:  [{'ticker', 'bps', 'notional_usd'}]
    cost_usd is the SIGNED bp-weighted notional sum, so a favourable fill offsets
    an adverse one — it is realized cost, not an adverse-only tally."""
    entry_rows = [r for r in (entry_rows or []) if r.get('bps') is not None]
    exit_rows = [r for r in (exit_rows or []) if r.get('bps') is not None]
    e_bps = [r['bps'] for r in entry_rows]
    x_bps = [r['bps'] for r in exit_rows]
    lat = [r['latency_s'] for r in entry_rows if r.get('latency_s') is not None]
    cost = sum(float(r['bps']) / 10000.0 * float(r.get('notional_usd') or 0.0)
               for r in entry_rows + exit_rows)
    tickers = [r['ticker'] for r in entry_rows if r.get('ticker')]
    model = modelled_median_bps(tickers, cost_bps=cost_bps) if tickers else None
    e_median = _median(e_bps)
    return {
        'entry_n': len(e_bps), 'entry_mean': _mean(e_bps),
        'entry_median': e_median, 'entry_p90': _pctile(e_bps, 0.90),
        'exit_n': len(x_bps), 'exit_mean': _mean(x_bps),
        'latency_median_s': _median(lat),
        'cost_usd': cost,
        'modelled_median': model,
        'verdict': verdict(e_median, model),
    }


def _bp(v):
    return 'n/a' if v is None else f'{float(v):.1f}bp'


def _sec(v):
    return 'n/a' if v is None else f'{float(v):.1f}s'


def format_line(st: dict, run_date) -> str:
    """The one-line digest string. n=0 renders 'n/a', never a 0 that would read
    as 'we paid no slippage today'."""
    return (f"fill_slippage: entry n={st['entry_n']} mean={_bp(st['entry_mean'])} "
            f"median={_bp(st['entry_median'])} p90={_bp(st['entry_p90'])} | "
            f"exit n={st['exit_n']} mean={_bp(st['exit_mean'])} | "
            f"latency_med={_sec(st['latency_median_s'])} | "
            f"cost=${st['cost_usd']:,.0f} | "
            f"modelled_med={_bp(st['modelled_median'])} verdict={st['verdict']} "
            f"asof={str(run_date)[:10]}")


_ENTRY_SQL = """
    SELECT es.ticker,
           es.fill_slippage_bps,
           COALESCE(s.filled_qty, 0) * COALESCE(es.broker_fill_price, 0),
           EXTRACT(EPOCH FROM (s.filled_at - s.submitted_at))
      FROM execution_signals es
      JOIN alpaca_submissions s ON s.run_date = es.target_date
                               AND s.ticker = es.ticker
                               AND s.strategy_id = es.strategy_id
     WHERE es.target_date = %s
       AND es.fill_slippage_bps IS NOT NULL
"""

_EXIT_SQL = """
    SELECT es.ticker,
           sp.exit_slippage_bps,
           COALESCE(sp.closed_price, 0) * COALESCE(s.filled_qty, 0)
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
      LEFT JOIN alpaca_submissions s ON s.run_date = es.target_date
                                    AND s.ticker = es.ticker
                                    AND s.strategy_id = es.strategy_id
     WHERE sp.pnl_date = %s
       AND sp.exit_slippage_bps IS NOT NULL
"""


def load_entry_rows(conn, run_date) -> list:
    with conn.cursor() as cur:
        cur.execute(_ENTRY_SQL, (run_date,))
        return [{'ticker': r[0], 'bps': float(r[1]),
                 'notional_usd': float(r[2] or 0.0),
                 'latency_s': (None if r[3] is None else float(r[3]))}
                for r in (cur.fetchall() or [])]


def load_exit_rows(conn, run_date) -> list:
    with conn.cursor() as cur:
        cur.execute(_EXIT_SQL, (run_date,))
        return [{'ticker': r[0], 'bps': float(r[1]),
                 'notional_usd': float(r[2] or 0.0)}
                for r in (cur.fetchall() or [])]


def fill_slippage_line(run_date, *, conn=None, cost_bps=None):
    """The full line, or None on any failure (logged). Owns its connection when
    conn is None — mirrors bench_realized.bench_realized_line:164-189."""
    import os
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        st = summarize(load_entry_rows(conn, run_date),
                       load_exit_rows(conn, run_date), cost_bps=cost_bps)
        return format_line(st, run_date)
    except Exception as e:  # noqa: BLE001
        logger.warning('[fill_slippage] skipped (%s: %s)', type(e).__name__, e)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
```

- [ ] **Step 4** — Wire it into `src/execution/send_report.py`. Insert immediately after the
`bench_realized` try/except block (which currently ends with
`print(f'[send_report] bench_realized skipped: {e}')` at line 667):

```python
    # Stream B (2026-09-12) item 14: realized fill-slippage line, entry + exit
    # legs, with a verdict against our own per-ticker half-spread cost model.
    # Report-only, fail-open; same daily post as bench_realized, no new webhook.
    try:
        from execution.fill_slippage import fill_slippage_line
        _fs = fill_slippage_line(run_date)
        if _fs:
            summary = f'{summary}\n{_fs}'
            print(f'[send_report] {_fs}')
    except Exception as e:
        print(f'[send_report] fill_slippage skipped: {e}')
```

- [ ] **Step 5** — Run the task's test plus the touched module's tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_fill_slippage_digest.py tests/execution/test_bench_realized.py -q
```

- [ ] **Step 6** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/execution/fill_slippage.py src/execution/send_report.py tests/execution/test_fill_slippage_digest.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(report): fill_slippage: line in the daily #trade-reports digest

Stream B item 14. n / mean / median / p90 entry bp from the migration-145 column,
n / mean exit bp from the new migration-155 column, median filled_at-submitted_at
latency, signed sum-of-dollars cost, and a verdict against OUR OWN per-ticker
half-spread artifact (OK <= 1.5x modelled, WARN <= 3x, FAIL above) — never a
foreign asset class's bands. n=0 renders n/a rather than a zero that would read as
'no slippage today'. Appended fail-open exactly like the bench_realized line.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: B3 — migration 156 + the per-ticker ownership ledger, computed in `reconcile`

**Safety:** the ownership pass runs at the END of `alpaca_reconcile.main()`, after
`cleanup_phantom_signals`, inside the pipeline's `reconcile` step (daily 10:00 ET cycle and every
intraday redeploy). It is REPORT-ONLY in this task — it writes `position_ownership` rows and logs
transitions; nothing reads it yet (Task 8 adds the opt-in sizer block). `run_ownership_pass`
swallows every exception and returns `{'skipped': …}`, so it can never fail the reconcile step,
and it SKIPS entirely when `stop_reattach.fetch_positions()` returns `None` (broker unreadable) —
classifying an unreadable book would mark every ticker `shortfall`. Migration 156 is the next free
number after 155 (Task 3). Inert until merged to main and johnbot restarts.

**Resolved ambiguity (`signal_qty`):** `execution_signals` has NO share-quantity column (schema
`012_execution_engine.sql:28-48` plus the additive migrations 014/101/126/145). The share count
lives on `alpaca_submissions.filled_qty`, joined by `(run_date -> target_date, ticker,
strategy_id)` — the same key `parity_mark.backfill_broker_fill_truth:323-332` uses. `filled_qty`
is the ENTRY quantity and never decreases, so a position entered at 100 and reduced to 40 would
read `shortfall` forever. It is therefore netted by the exit fills now recorded in `broker_fills`
(Task 4): any fill on that ticker after the earliest entry submission whose `order_id` is NOT
itself a recorded entry submission is an exit, and its signed delta is applied once per ticker.
Documented limitation: before `broker_fills` has a day of history, partially-exited positions read
`shortfall` — which is exactly why the default is report-only and the block flag ships unset.

**Files:**
- Create: `src/database/migrations/156_position_ownership.sql`
- Create: `src/execution/position_ownership.py`
- Modify: `src/execution/alpaca_reconcile.py` — `main()` after the `cleanup_phantom_signals(...)` call (line 525)
- Modify: `tests/database/test_stream_b_migration_shape.py` — add the 156 assertions
- Test: `tests/execution/test_position_ownership.py` (Create)

**Interfaces:**
- Consumes: `stop_reattach.fetch_positions() -> list[dict] | None` (line 230; equity-only, `None` means the CLI call failed); `broker_fills` (migration 155); `alpaca_submissions`; `execution_signals`
- Produces:
  - table `position_ownership(cycle_date DATE, ticker TEXT, account_qty NUMERIC, signal_qty NUMERIC, unknown_qty NUMERIC, status TEXT, created_at TIMESTAMPTZ, PRIMARY KEY (cycle_date, ticker))`
  - `position_ownership.STATUS_OK / STATUS_UNALLOCATED / STATUS_SHORTFALL`
  - `position_ownership.classify(account_qty, signal_qty, *, extra_tol=0.001, shortfall_tol=0.005, dust=1.0) -> tuple[float, str]`
  - `position_ownership.compute_ownership(account_qty: dict, signal_qty: dict, **kw) -> list[dict]`
  - `position_ownership.transitions(rows, previous: dict) -> list[str]`
  - `position_ownership.load_account_qty(fetch_positions=None) -> dict | None`
  - `position_ownership.load_signal_qty(cur) -> dict`
  - `position_ownership.load_previous_statuses(cur, cycle_date) -> dict`
  - `position_ownership.persist_ownership(cur, cycle_date, rows) -> int`
  - `position_ownership.latest_status_map(cur) -> dict`
  - `position_ownership.run_ownership_pass(conn, cycle_date, *, dry_run=False, account_qty=None, log_fn=None) -> dict`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_position_ownership.py`:

```python
"""B3 (spec item 15): do the shares the broker holds match what signals claim?

unknown = account_qty - signal_qty on SIGNED quantities, so a short book reads
the same way a long one does. Tolerances are asymmetric — a shortfall (signals
marking a position the broker doesn't hold) is the dangerous direction — and
floored at 1 share so rounding dust is never a finding.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import position_ownership as po  # noqa: E402


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


# ── classify ───────────────────────────────────────────────────────────────

def test_exact_match_is_ok():
    assert po.classify(100.0, 100.0) == (0.0, po.STATUS_OK)


def test_one_share_of_dust_is_ok_in_both_directions():
    assert po.classify(101.0, 100.0)[1] == po.STATUS_OK
    assert po.classify(99.0, 100.0)[1] == po.STATUS_OK


def test_broker_extra_beyond_tolerance_is_unallocated():
    unknown, status = po.classify(10_000.0, 9_000.0)
    assert unknown == 1000.0 and status == po.STATUS_UNALLOCATED


def test_signals_claiming_more_than_the_broker_holds_is_shortfall():
    unknown, status = po.classify(9_000.0, 10_000.0)
    assert unknown == -1000.0 and status == po.STATUS_SHORTFALL


def test_percentage_tolerances_are_asymmetric():
    # 10,000 share position: 0.1% extra (10 sh) ok, 0.5% shortfall (50 sh) ok.
    assert po.classify(10_010.0, 10_000.0)[1] == po.STATUS_OK
    assert po.classify(10_011.0, 10_000.0)[1] == po.STATUS_UNALLOCATED
    assert po.classify(9_950.0, 10_000.0)[1] == po.STATUS_OK
    assert po.classify(9_949.0, 10_000.0)[1] == po.STATUS_SHORTFALL


def test_short_book_uses_the_same_signed_rule():
    assert po.classify(-100.0, -100.0)[1] == po.STATUS_OK
    # broker is shorter than the ledger claims -> extra short exposure nobody owns
    assert po.classify(-10_000.0, -9_000.0)[1] == po.STATUS_SHORTFALL
    assert po.classify(-9_000.0, -10_000.0)[1] == po.STATUS_UNALLOCATED


# ── compute_ownership ──────────────────────────────────────────────────────

def test_broker_only_ticker_is_unallocated():
    rows = {r['ticker']: r for r in po.compute_ownership({'ZZZ': 500.0}, {})}
    assert rows['ZZZ']['status'] == po.STATUS_UNALLOCATED
    assert rows['ZZZ']['signal_qty'] == 0.0


def test_signal_only_ticker_is_shortfall():
    rows = {r['ticker']: r for r in po.compute_ownership({}, {'YYY': 500.0})}
    assert rows['YYY']['status'] == po.STATUS_SHORTFALL
    assert rows['YYY']['account_qty'] == 0.0


def test_compute_ownership_covers_the_union_and_sorts():
    rows = po.compute_ownership({'BBB': 1.0, 'AAA': 1.0}, {'CCC': 1.0})
    assert [r['ticker'] for r in rows] == ['AAA', 'BBB', 'CCC']


# ── transitions ────────────────────────────────────────────────────────────

def test_transitions_reports_only_changes():
    rows = po.compute_ownership({'AAA': 100.0, 'BBB': 500.0}, {'AAA': 100.0, 'BBB': 0.0})
    lines = po.transitions(rows, {'AAA': po.STATUS_OK, 'BBB': po.STATUS_UNALLOCATED})
    assert lines == []


def test_transitions_reports_a_new_problem():
    rows = po.compute_ownership({'BBB': 500.0}, {'BBB': 0.0})
    lines = po.transitions(rows, {'BBB': po.STATUS_OK})
    assert len(lines) == 1 and 'ok -> unallocated' in lines[0] and 'BBB' in lines[0]


def test_transitions_reports_a_recovery():
    rows = po.compute_ownership({'BBB': 100.0}, {'BBB': 100.0})
    lines = po.transitions(rows, {'BBB': po.STATUS_SHORTFALL})
    assert len(lines) == 1 and 'shortfall -> ok' in lines[0]


def test_transitions_skips_a_first_sighting_of_a_healthy_ticker():
    rows = po.compute_ownership({'NEW': 100.0}, {'NEW': 100.0})
    assert po.transitions(rows, {}) == []


def test_transitions_reports_a_first_sighting_of_an_unhealthy_ticker():
    rows = po.compute_ownership({'NEW': 500.0}, {})
    lines = po.transitions(rows, {})
    assert len(lines) == 1 and 'new -> unallocated' in lines[0]


# ── loaders + pass ─────────────────────────────────────────────────────────

def test_load_account_qty_sums_signed_share_counts():
    positions = [{'symbol': 'AAA', 'qty': '100'}, {'symbol': 'BBB', 'qty': '-50'}]
    assert po.load_account_qty(lambda: positions) == {'AAA': 100.0, 'BBB': -50.0}


def test_load_account_qty_returns_none_when_the_broker_is_unreadable():
    assert po.load_account_qty(lambda: None) is None


def test_persist_ownership_is_append_only():
    cur = _Cursor()
    rows = po.compute_ownership({'AAA': 100.0}, {'AAA': 100.0})
    assert po.persist_ownership(cur, date(2026, 9, 15), rows) == 1
    sql, params = cur.calls[0]
    assert sql.startswith('INSERT INTO position_ownership')
    assert 'ON CONFLICT (cycle_date, ticker) DO NOTHING' in sql
    assert params[1] == 'AAA' and params[5] == po.STATUS_OK


def test_run_ownership_pass_skips_when_the_broker_is_unreadable(monkeypatch):
    # account_qty defaults to None, so the pass calls load_account_qty; stub it
    # to the "couldn't ask the broker" answer.
    monkeypatch.setattr(po, 'load_account_qty', lambda *a, **k: None)
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                log_fn=lambda m: None)
    assert out == {'skipped': 'broker_unavailable'}


def test_run_ownership_pass_counts_and_never_raises(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(po, 'load_signal_qty', lambda c: {'AAA': 100.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    out = po.run_ownership_pass(_Conn(cur), date(2026, 9, 15),
                                account_qty={'AAA': 100.0, 'ZZZ': 500.0},
                                log_fn=lambda m: None)
    assert out['rows'] == 2 and out['unallocated'] == 1 and out['shortfall'] == 0
    assert len(out['transitions']) == 1


def test_run_ownership_pass_swallows_a_db_failure(monkeypatch):
    def _boom(c):
        raise RuntimeError('relation position_ownership does not exist')
    monkeypatch.setattr(po, 'load_signal_qty', _boom)
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                account_qty={'AAA': 1.0}, log_fn=lambda m: None)
    assert 'skipped' in out
```

- [ ] **Step 2** — Run it and confirm the expected failure (`ModuleNotFoundError: No module named 'execution.position_ownership'`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_position_ownership.py -q
```

- [ ] **Step 3** — Create `src/database/migrations/156_position_ownership.sql`.

> **Re-check the migration number first.** A concurrent session is planning Stream E
> (`docs/superpowers/plans/2026-09-12-qd-stream-e-reliability.md` was untracked in the worktree
> when this plan was written) and E3/E5 may claim numbers. Run
> `cd /root/openclaw/.claude/worktrees/qd-adoptions && ls src/database/migrations | tail -3`
> before creating the file; if 155/156 are taken, shift BOTH Stream B migrations to the next free
> pair and update the filenames, the `_sql(...)` calls in
> `tests/database/test_stream_b_migration_shape.py`, and the citations in Tasks 4-9 and the
> changelog entry together.

Contents:

```sql
-- 156: per-ticker ownership ledger.
-- Stream B item 15 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:167-181).
--
-- APPEND-ONLY: one row per (cycle_date, ticker), written by the reconcile step.
-- account_qty is the broker's signed share count; signal_qty is what the open
-- execution_signals rows claim (entry fills netted by attributed exit fills);
-- unknown_qty = account_qty - signal_qty; status is 'ok' | 'unallocated'
-- (broker holds shares no signal claims) | 'shortfall' (signals claim shares
-- the broker does not hold). The sizer reads only the newest cycle_date, and
-- only when OPENCLAW_OWNERSHIP_BLOCK=1.
CREATE TABLE IF NOT EXISTS position_ownership (
  cycle_date   DATE NOT NULL,
  ticker       TEXT NOT NULL,
  account_qty  NUMERIC,
  signal_qty   NUMERIC,
  unknown_qty  NUMERIC,
  status       TEXT,
  created_at   TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (cycle_date, ticker)
);

CREATE INDEX IF NOT EXISTS position_ownership_ticker_idx
  ON position_ownership (ticker, cycle_date DESC);
CREATE INDEX IF NOT EXISTS position_ownership_status_idx
  ON position_ownership (cycle_date DESC, status);
```

- [ ] **Step 4** — Extend `tests/database/test_stream_b_migration_shape.py` with the 156 assertions (append at the end of the file):

```python
POSITION_OWNERSHIP_COLUMNS = (
    'cycle_date', 'ticker', 'account_qty', 'signal_qty', 'unknown_qty',
    'status', 'created_at',
)


def test_156_declares_every_position_ownership_column():
    body = _create_body(_sql('156_position_ownership.sql'), 'position_ownership')
    for col in POSITION_OWNERSHIP_COLUMNS:
        assert re.search(rf'^\s*{col}\s+\w', body, re.M), f'position_ownership.{col} missing'


def test_156_primary_key_supports_the_append_only_upsert():
    assert 'PRIMARY KEY (cycle_date, ticker)' in _sql('156_position_ownership.sql')


def test_156_is_idempotent_and_append_only():
    upper = _sql('156_position_ownership.sql').upper()
    assert 'CREATE TABLE IF NOT EXISTS' in _sql('156_position_ownership.sql')
    for stmt in ('DROP ', 'DELETE ', 'TRUNCATE '):
        assert stmt not in upper, f'{stmt.strip()} violates the append-only invariant'


def test_156_is_the_next_free_number():
    existing = sorted(p.name for p in MIG.glob('*.sql'))
    assert '156_position_ownership.sql' in existing
    assert not any(n.startswith('156_') and n != '156_position_ownership.sql'
                   for n in existing)
```

- [ ] **Step 5** — Create `src/execution/position_ownership.py`:

```python
"""position_ownership.py — per-ticker broker-vs-ledger ownership.

Stream B item 15 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:167-181).

Nightly, in the pipeline's `reconcile` step: for each ticker compare the
broker's signed share count against what the open execution_signals rows claim,
classify ok / unallocated / shortfall, append one row per (cycle_date, ticker),
and log ONLY on a status transition.

Enforcement is opt-in. With OPENCLAW_OWNERSHIP_BLOCK=1 the sizer sheds OPENS and
ADDS on tickers whose latest status is not ok. Unset (the default) is report-only
and byte-identical to today's behaviour. Exits and flattens are NEVER blocked —
the sizer applies the blocklist through the same `_shed` helper the stop-out
cooldown uses (regime_blended_sizer._apply_entry_hygiene_gate:2398-2404).

signal_qty: execution_signals carries no share column, so the count comes from
alpaca_submissions.filled_qty joined by (run_date -> target_date, ticker,
strategy_id) — the key parity_mark.backfill_broker_fill_truth:323-332 uses.
filled_qty is the ENTRY quantity and never decreases, so it is netted by the
exit fills in broker_fills (migration 155): any fill on that ticker after the
earliest entry submission whose order_id is NOT itself a recorded entry
submission is an exit, applied ONCE per ticker. Before broker_fills has a day of
history a partially-exited position reads `shortfall` — which is why the default
is report-only.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

EXTRA_TOL_FRAC = 0.001      # broker may hold 0.1% more than the ledger claims
SHORTFALL_TOL_FRAC = 0.005  # ledger may claim 0.5% more than the broker holds
DUST_SHARES = 1.0           # absolute floor: 1 share is never a finding

STATUS_OK = 'ok'
STATUS_UNALLOCATED = 'unallocated'
STATUS_SHORTFALL = 'shortfall'


def classify(account_qty, signal_qty, *,
             extra_tol: float = EXTRA_TOL_FRAC,
             shortfall_tol: float = SHORTFALL_TOL_FRAC,
             dust: float = DUST_SHARES):
    """(unknown_qty, status) for one ticker.

    unknown = account - signal on SIGNED quantities, so a short book reads the
    same way a long one does. unknown > 0 means the broker holds exposure no
    open signal claims (unallocated); unknown < 0 means open signals claim
    exposure the broker does not hold (shortfall) — the dangerous direction,
    which is why it gets the looser band before we cry wolf. Both are floored at
    `dust` shares so rounding residue is never a finding."""
    a = float(account_qty)
    s = float(signal_qty)
    unknown = a - s
    scale = max(abs(a), abs(s))
    if unknown > 0:
        tol = max(dust, extra_tol * scale)
        return unknown, (STATUS_OK if unknown <= tol else STATUS_UNALLOCATED)
    if unknown < 0:
        tol = max(dust, shortfall_tol * scale)
        return unknown, (STATUS_OK if -unknown <= tol else STATUS_SHORTFALL)
    return 0.0, STATUS_OK


def compute_ownership(account_qty: dict, signal_qty: dict, **kw) -> list:
    """[{ticker, account_qty, signal_qty, unknown_qty, status}] over the UNION of
    both key sets, sorted by ticker. A ticker present on only one side gets 0.0
    on the other — exactly the case this ledger exists to catch."""
    rows = []
    for tkr in sorted(set(account_qty or {}) | set(signal_qty or {})):
        a = float((account_qty or {}).get(tkr) or 0.0)
        s = float((signal_qty or {}).get(tkr) or 0.0)
        unknown, status = classify(a, s, **kw)
        rows.append({'ticker': tkr, 'account_qty': a, 'signal_qty': s,
                     'unknown_qty': round(unknown, 6), 'status': status})
    return rows


def transitions(rows, previous: dict) -> list:
    """One line per ticker whose status CHANGED vs `previous` ({ticker: status}
    from the last cycle). A ticker with no prior row counts as a transition only
    when its status is not ok — the first sighting of a healthy name is not news
    and would flood the log on the very first pass."""
    previous = previous or {}
    out = []
    for r in rows:
        prev = previous.get(r['ticker'])
        if prev == r['status']:
            continue
        if prev is None and r['status'] == STATUS_OK:
            continue
        out.append(f"[ownership] {r['ticker']}: {prev or 'new'} -> {r['status']} "
                   f"(account={r['account_qty']:g} signal={r['signal_qty']:g} "
                   f"unknown={r['unknown_qty']:+g})")
    return out


def load_account_qty(fetch_positions=None):
    """{ticker: signed share qty} from the broker, or None when the CLI call
    FAILED. stop_reattach.fetch_positions returns None for "couldn't ask" vs []
    for a genuinely flat book (:234-236); the ownership pass MUST skip on None —
    classifying an unreadable book would mark every ticker shortfall."""
    if fetch_positions is None:
        from execution.stop_reattach import fetch_positions as _fp
        fetch_positions = _fp
    positions = fetch_positions()
    if positions is None:
        return None
    out: dict = {}
    for p in positions:
        sym = p.get('symbol')
        if not sym:
            continue
        try:
            out[sym] = out.get(sym, 0.0) + float(p.get('qty') or 0.0)
        except (TypeError, ValueError):
            continue
    return out


_SIGNAL_QTY_SQL = """
    WITH entries AS (
        SELECT es.ticker,
               SUM(CASE WHEN UPPER(es.direction) IN ('LONG','BUY','BUY_VOL')
                        THEN 1 ELSE -1 END * COALESCE(s.filled_qty, 0)) AS entry_qty,
               MIN(s.submitted_at) AS first_submitted_at
          FROM execution_signals es
          JOIN alpaca_submissions s ON s.run_date = es.target_date
                                   AND s.ticker = es.ticker
                                   AND s.strategy_id = es.strategy_id
         WHERE es.status = 'open'
           AND (es.lifecycle_state IS NULL OR es.lifecycle_state = 'FILLED')
           AND es.ticker IS NOT NULL
         GROUP BY es.ticker
    )
    SELECT e.ticker,
           e.entry_qty + COALESCE((
               SELECT SUM(CASE WHEN LOWER(bf.side) = 'sell' THEN -bf.qty ELSE bf.qty END)
                 FROM broker_fills bf
                WHERE bf.ticker = e.ticker
                  AND bf.filled_at >= e.first_submitted_at
                  AND NOT EXISTS (SELECT 1 FROM alpaca_submissions s2
                                   WHERE s2.alpaca_order_id = bf.order_id)
           ), 0) AS signal_qty
      FROM entries e
"""


def load_signal_qty(cur) -> dict:
    """{ticker: signed share qty the open signals claim}. Entry fills netted by
    every non-entry fill on that ticker since the earliest entry submission —
    applied ONCE per ticker, never once per signal (two strategies on the same
    name would otherwise double-count the exit)."""
    cur.execute(_SIGNAL_QTY_SQL)
    return {r[0]: float(r[1] or 0.0) for r in (cur.fetchall() or []) if r and r[0]}


def load_previous_statuses(cur, cycle_date) -> dict:
    """{ticker: status} at the most recent cycle_date STRICTLY BEFORE this one —
    the baseline `transitions` diffs against."""
    cur.execute(
        'SELECT ticker, status FROM position_ownership WHERE cycle_date = '
        '(SELECT MAX(cycle_date) FROM position_ownership WHERE cycle_date < %s)',
        (cycle_date,))
    return {r[0]: r[1] for r in (cur.fetchall() or []) if r and r[0]}


def persist_ownership(cur, cycle_date, rows) -> int:
    """Append one row per ticker. ON CONFLICT DO NOTHING: a second reconcile run
    on the same cycle_date is a no-op, never a rewrite (append-only)."""
    n = 0
    for r in rows:
        cur.execute(
            'INSERT INTO position_ownership '
            '(cycle_date, ticker, account_qty, signal_qty, unknown_qty, status) '
            'VALUES (%s,%s,%s,%s,%s,%s) '
            'ON CONFLICT (cycle_date, ticker) DO NOTHING',
            (cycle_date, r['ticker'], r['account_qty'], r['signal_qty'],
             r['unknown_qty'], r['status']))
        n += 1
    return n


def latest_status_map(cur) -> dict:
    """{ticker: status} at the newest cycle_date — what the sizer blocklist and
    the position_ownership_clean system check read."""
    cur.execute(
        'SELECT ticker, status FROM position_ownership WHERE cycle_date = '
        '(SELECT MAX(cycle_date) FROM position_ownership)')
    return {r[0]: r[1] for r in (cur.fetchall() or []) if r and r[0]}


def run_ownership_pass(conn, cycle_date, *, dry_run: bool = False,
                       account_qty=None, log_fn=None) -> dict:
    """The reconcile-step hook. Returns
    {'rows', 'unallocated', 'shortfall', 'transitions'} or {'skipped': reason}.

    NEVER raises: an ownership finding must not fail the reconcile step, whose
    critical path is the submission reconcile."""
    emit = log_fn or logger.info
    try:
        if account_qty is None:
            account_qty = load_account_qty()
        if account_qty is None:
            emit('[ownership] broker position list unavailable — pass SKIPPED')
            return {'skipped': 'broker_unavailable'}
        cur = conn.cursor()
        signal_qty = load_signal_qty(cur)
        rows = compute_ownership(account_qty, signal_qty)
        lines = transitions(rows, load_previous_statuses(cur, cycle_date))
        if not dry_run:
            persist_ownership(cur, cycle_date, rows)
            conn.commit()
        for line in lines:
            emit(line)
        stats = {
            'rows': len(rows),
            'unallocated': sum(1 for r in rows if r['status'] == STATUS_UNALLOCATED),
            'shortfall': sum(1 for r in rows if r['status'] == STATUS_SHORTFALL),
            'transitions': lines,
        }
        emit(f"[ownership] {stats['rows']} ticker(s): {stats['unallocated']} unallocated, "
             f"{stats['shortfall']} shortfall, {len(lines)} transition(s)")
        return stats
    except Exception as e:  # noqa: BLE001
        emit(f'[ownership] pass failed ({type(e).__name__}: {e}) — skipped')
        return {'skipped': f'{type(e).__name__}: {e}'}
```

- [ ] **Step 6** — Hook it into the reconcile step. In `src/execution/alpaca_reconcile.py`,
inside `main()`, immediately after the `cleanup_phantom_signals(conn, args.dry_run, broker_tickers)`
call (line 525) and still inside the same `try:`:

```python
        # Stream B (2026-09-12) item 15: per-ticker ownership ledger. REPORT-ONLY
        # here — OPENCLAW_OWNERSHIP_BLOCK=1 is what makes the sizer act on it.
        # run_ownership_pass swallows its own failures and returns {'skipped': …},
        # so this can never fail the reconcile step.
        from execution.position_ownership import run_ownership_pass
        log(f'ownership: {run_ownership_pass(conn, args.date, dry_run=args.dry_run, log_fn=log)}')
```

- [ ] **Step 7** — Run the task's tests plus the touched modules'; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_position_ownership.py tests/database/test_stream_b_migration_shape.py tests/execution/test_alpaca_reconcile.py tests/execution/test_alpaca_reconcile_sweep.py -q
```

- [ ] **Step 8** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/database/migrations/156_position_ownership.sql src/execution/position_ownership.py src/execution/alpaca_reconcile.py tests/execution/test_position_ownership.py tests/database/test_stream_b_migration_shape.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(reconcile): per-ticker ownership ledger (migration 156, report-only)

Stream B item 15. Nightly in the reconcile step: broker signed share count vs
what open signals claim, classified ok / unallocated / shortfall with asymmetric
percentage tolerances and a 1-share dust floor, appended to position_ownership
and logged only on a status transition. execution_signals has no share column, so
signal_qty comes from alpaca_submissions.filled_qty netted by the non-entry fills
now in broker_fills — applied once per ticker so two strategies on one name can't
double-count the exit. Skips entirely when the broker position list is unreadable
(None), because classifying an unreadable book would mark everything shortfall.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: B3 enforcement — `OPENCLAW_OWNERSHIP_BLOCK=1` blocks opens and adds, never exits

**Safety:** `regime_blended_sizer.py` is the LIVE production sizer behind the pipeline's `trade`
step (`regime_blended_sizer_live.py`, `OPENCLAW_REGIME_BLENDED_LIVE=1` since 2026-05-12), run at
15:00 ET and on every intraday redeploy. `_apply_entry_hygiene_gate` already has only-shed
semantics via `_shed` (lines 2398-2404): a blocked ticker that is NOT held has its target dropped,
a flip becomes close-only, and a same-sign INCREASE is capped at the held size — so a reduce and a
flatten (`target == 0.0`) pass through untouched. The ownership blocklist reuses that helper
verbatim, which is what makes "exits and flattens are never blocked" structural rather than a
promise. `_load_ownership_blocklist` returns an EMPTY set unless `OPENCLAW_OWNERSHIP_BLOCK=1`, so
an unset flag leaves sizing byte-identical to today. The `[ownership]` line is emitted from the
loader, called at the `_emit_orders_from_targets` call site (line 2469), so it still prints when
`OPENCLAW_ENTRY_HYGIENE=0` short-circuits the gate.

Grep-verified (`grep -rn "_apply_entry_hygiene_gate" src scripts tests`): line 2469 is the ONLY
production caller — `tests/execution/test_entry_hygiene_gate.py:198` already pins that with
`module.count('_apply_entry_hygiene_gate(') == 2`, and this task keeps the count at 2 by editing
that invocation rather than adding one. The other callers are two test files
(`test_entry_hygiene_gate.py:32`, `test_sameday_premarket_protection.py:271,282,293,302`) that
pass every lookup by injection and pass NO `ownership_blocked` — which is exactly why the gate
must resolve an omitted argument to the empty set instead of loading it (Step 4c). Inert until
merged to main.

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` — new helpers after `_load_recent_stopouts` (ends line 2317); `_apply_entry_hygiene_gate` signature (lines 2362-2363), docstring (2364-2380), defaults block (2384-2393), accumulator list (2396), `_shed` closure (2398-2404, unchanged), loop branch (after the premarket-veto `continue` at 2411-2414), final warning (2438-2444); call site in `_emit_orders_from_targets` (line 2469)
- Test: `tests/execution/test_ownership_sizer_block.py` (Create)

**Interfaces:**
- Consumes: `position_ownership.latest_status_map(cur) -> dict`; `position_ownership.STATUS_OK`; `regime_blended_sizer._shed(tkr)` (closure inside `_apply_entry_hygiene_gate`, line 2397)
- Produces:
  - `regime_blended_sizer._ownership_block_on() -> bool`
  - `regime_blended_sizer._ownership_blocklist_from(status_map, *, enforcing=None) -> set`
  - `regime_blended_sizer._load_ownership_blocklist() -> set`
  - `_apply_entry_hygiene_gate(target_usd, broker, *, stopouts=None, liq=None, params=None, premarket_vetoes=None, risk_exits=None, ownership_blocked=None)`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_ownership_sizer_block.py`:

```python
"""B3 enforcement: ownership-blocked names can be REDUCED but never ADDED to.

The block reuses the entry-hygiene gate's `_shed` helper, so the guarantee is
structural: not held -> target dropped; flip -> close-only; same-sign increase ->
capped at held; reduce and flatten pass through untouched.
"""
from __future__ import annotations

import importlib

import pytest

rbs = importlib.import_module("execution.regime_blended_sizer")
po = importlib.import_module("execution.position_ownership")


@pytest.fixture(autouse=True)
def _hygiene_enabled(monkeypatch):
    # tests/execution/conftest.py defaults OPENCLAW_ENTRY_HYGIENE=0 for the e2e
    # sizer harnesses; this file tests the gate, so re-enable it (same as
    # tests/execution/test_entry_hygiene_gate.py).
    monkeypatch.setenv("OPENCLAW_ENTRY_HYGIENE", "1")


PARAMS = {
    'stopout_cooldown_days': 7.0,
    'risk_exit_cooldown_days': 7.0,
    'entry_min_price_usd': 2.0,
    'entry_min_adv_usd': 400_000.0,
    'entry_participation_frac': 0.01,
}


def _gate(target, broker, *, ownership_blocked=None):
    # Every lookup is injected: an omitted one falls through to the REAL
    # Postgres on this box (modules under src/execution load .env at import).
    return rbs._apply_entry_hygiene_gate(
        dict(target), broker,
        stopouts={}, liq=({}, {}), params=dict(PARAMS),
        risk_exits={}, premarket_vetoes=set(),
        ownership_blocked=set(ownership_blocked or ()))


# ── shed semantics ─────────────────────────────────────────────────────────

def test_blocked_ticker_not_held_is_dropped():
    assert "AAA" not in _gate({"AAA": 5000.0}, {}, ownership_blocked=["AAA"])


def test_blocked_ticker_add_is_capped_at_held_size():
    out = _gate({"AAA": 9000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 4000.0


def test_blocked_ticker_reduce_is_allowed():
    out = _gate({"AAA": 1000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 1000.0


def test_blocked_ticker_flatten_is_never_blocked():
    out = _gate({"AAA": 0.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 0.0


def test_blocked_ticker_flip_becomes_close_only():
    out = _gate({"AAA": -5000.0}, {"AAA": 4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == 0.0


def test_blocked_short_add_is_capped_at_held_size():
    out = _gate({"AAA": -9000.0}, {"AAA": -4000.0}, ownership_blocked=["AAA"])
    assert out["AAA"] == -4000.0


def test_unblocked_ticker_is_untouched():
    out = _gate({"BBB": 5000.0}, {}, ownership_blocked=["AAA"])
    assert out["BBB"] == 5000.0


def test_empty_blocklist_is_a_passthrough():
    out = _gate({"AAA": 5000.0}, {}, ownership_blocked=[])
    assert out["AAA"] == 5000.0


# ── blocklist resolution ───────────────────────────────────────────────────

_STATUSES = {'AAA': po.STATUS_UNALLOCATED, 'BBB': po.STATUS_OK,
             'CCC': po.STATUS_SHORTFALL}


def test_blocklist_is_empty_without_the_flag(monkeypatch):
    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    assert rbs._ownership_blocklist_from(_STATUSES) == set()


def test_blocklist_is_empty_when_the_flag_is_not_exactly_one(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "true")
    assert rbs._ownership_blocklist_from(_STATUSES) == set()


def test_blocklist_holds_every_non_ok_ticker_with_the_flag(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    assert rbs._ownership_blocklist_from(_STATUSES) == {"AAA", "CCC"}


def test_blocklist_of_an_empty_status_map_is_empty(monkeypatch):
    monkeypatch.setenv("OPENCLAW_OWNERSHIP_BLOCK", "1")
    assert rbs._ownership_blocklist_from({}) == set()
    assert rbs._ownership_blocklist_from(None) == set()


def test_blocklist_logs_the_ownership_line_in_report_only_mode(monkeypatch, caplog):
    monkeypatch.delenv("OPENCLAW_OWNERSHIP_BLOCK", raising=False)
    with caplog.at_level("INFO", logger=rbs.logger.name):
        rbs._ownership_blocklist_from(_STATUSES)
    assert any("[ownership]" in r.getMessage() for r in caplog.records)


# ── the gate itself must never reach the DB ────────────────────────────────

def test_gate_defaults_to_no_block_and_never_loads():
    """An omitted ownership_blocked must resolve to the empty set, NOT to a DB
    read: tests/execution/test_entry_hygiene_gate.py and
    test_sameday_premarket_protection.py call this gate without the kwarg and
    their contract is 'no DB access in tests'. Enforcement is supplied by the
    single production call site instead."""
    import inspect
    src = inspect.getsource(rbs._apply_entry_hygiene_gate)
    assert '_load_ownership_blocklist' not in src, \
        'the gate must not load the blocklist; the call site supplies it'
    out = rbs._apply_entry_hygiene_gate(
        {"AAA": 5000.0}, {}, stopouts={}, liq=({}, {}), params=dict(PARAMS),
        risk_exits={}, premarket_vetoes=set())
    assert out["AAA"] == 5000.0


def test_the_single_production_call_site_supplies_the_blocklist():
    """test_entry_hygiene_gate.py:198 already pins the gate to exactly ONE
    invocation (definition + call = 2 occurrences). Pin that the one invocation
    is the one that passes the blocklist, so a second emission route can never
    silently skip ownership enforcement."""
    import inspect
    tail = inspect.getsource(rbs._emit_orders_from_targets)
    assert 'ownership_blocked=_load_ownership_blocklist()' in tail
    assert inspect.getsource(rbs).count('_apply_entry_hygiene_gate(') == 2
```

- [ ] **Step 2** — Run it and confirm the expected failure (`TypeError: _apply_entry_hygiene_gate() got an unexpected keyword argument 'ownership_blocked'`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_ownership_sizer_block.py -q
```

- [ ] **Step 3** — Add the blocklist helpers to `src/execution/regime_blended_sizer.py`, immediately after `_load_recent_stopouts` (which ends at line 2317, before `def _load_liquidity_stats`):

```python
def _ownership_block_on() -> bool:
    """Stream B item 15 enforcement flag. Unset (or anything other than '1')
    means report-only: the ledger is written and logged, sizing is untouched."""
    return os.environ.get('OPENCLAW_OWNERSHIP_BLOCK') == '1'


def _ownership_blocklist_from(status_map, *, enforcing=None) -> set:
    """Pure: {ticker: ownership status} -> the set the hygiene gate sheds.

    ALWAYS logs the `[ownership]` line so the trade step shows the finding even
    in report-only mode, and returns an EMPTY set unless enforcing — so an unset
    OPENCLAW_OWNERSHIP_BLOCK leaves today's sizing byte-identical."""
    from execution.position_ownership import STATUS_OK
    if enforcing is None:
        enforcing = _ownership_block_on()
    bad = {t for t, s in (status_map or {}).items() if s and s != STATUS_OK}
    logger.info('[ownership] %d ticker(s) not ok=%s enforcing=%s',
                len(bad), sorted(bad)[:20], bool(enforcing))
    return bad if enforcing else set()


def _load_ownership_blocklist() -> set:
    """Read the newest position_ownership cycle and resolve the blocklist.
    Fail-open (empty set) on any error: a ledger read must never stop the book
    from trading."""
    try:
        import psycopg2
        from execution.position_ownership import latest_status_map
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
            status_map = latest_status_map(cur)
    except Exception as e:  # noqa: BLE001
        logger.warning('[ownership] latest-status lookup failed (%s) — no block applied', e)
        return set()
    return _ownership_blocklist_from(status_map)
```

- [ ] **Step 4** — Wire the blocklist through `_apply_entry_hygiene_gate`. Four edits in
`src/execution/regime_blended_sizer.py`:

(a) Signature (lines 2362-2363):

```python
def _apply_entry_hygiene_gate(target_usd, broker, *, stopouts=None, liq=None, params=None,
                              premarket_vetoes=None, risk_exits=None,
                              ownership_blocked=None):
```

(b) Docstring — add a fourth entry to the cooldown-class list (after the `risk exit` line at 2372):

```
      ownership      — the broker's share count and the open signals' claim
                       disagree on this name (Stream B item 15). Direction-
                       agnostic like the news veto: the finding says "we do not
                       know who owns these shares", not "this side lost". Opt-in
                       via OPENCLAW_OWNERSHIP_BLOCK=1; the default resolves to an
                       empty set, so unset == today's behaviour.
```

(c) Resolve, do NOT load. After the `if liq is None:` block (lines 2392-2393) add:

```python
    ownership_blocked = ownership_blocked or frozenset()
```

**Do not add a `_load_ownership_blocklist()` fallback here.** `tests/execution/
test_entry_hygiene_gate.py:32` and `tests/execution/test_sameday_premarket_protection.py:271,282,293,302`
call this gate WITHOUT `ownership_blocked`, and both files' contract is "all inputs injectable,
no DB access in tests" — an in-gate loader would open `psycopg2.connect(POSTGRES_URI)` against
production Postgres in every one of those tests, while the fleet backtest runs. Step 5 supplies
the set at the ONE production call site; an omitted argument means no enforcement, which is the
fail-open direction.

and change the accumulator line (2396) to:

```python
    cooled, illiquid, part_capped, vetoed, risk_cooled, own_blocked = [], [], [], [], [], []
```

(d) Loop branch — insert immediately after the premarket-veto `continue` (line 2414), before
`stop_dir = stopouts.get(tkr)`:

```python
        if tkr in ownership_blocked and target != 0.0:
            _shed(tkr)
            own_blocked.append(tkr)
            continue
```

and widen the final warning (lines 2438-2444) to:

```python
    if cooled or illiquid or part_capped or vetoed or risk_cooled or own_blocked:
        logger.warning(
            'regime_blended_sizer.entry_hygiene: premarket-veto blocked=%s, '
            'stop-out cooldown blocked=%s, risk-exit cooldown blocked=%s, '
            'ownership blocked=%s, liquidity floor blocked=%s, '
            'participation-capped=%s',
            sorted(vetoed), sorted(cooled), sorted(risk_cooled), sorted(own_blocked),
            sorted(illiquid), sorted(part_capped))
```

- [ ] **Step 5** — Emit the line outside the gate's early return. In `_emit_orders_from_targets`
(line 2469), change the hygiene call to:

```python
    # Stream B item 15: the `[ownership]` line is emitted every cycle (report-only
    # by default) because the loader runs HERE, not inside the gate — the gate
    # short-circuits on OPENCLAW_ENTRY_HYGIENE=0 and would swallow the line. The
    # returned set is empty unless OPENCLAW_OWNERSHIP_BLOCK=1.
    target_usd = _apply_entry_hygiene_gate(
        target_usd, broker, ownership_blocked=_load_ownership_blocklist())
```

- [ ] **Step 6** — Run the task's test plus the touched module's gate tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_ownership_sizer_block.py tests/execution/test_entry_hygiene_gate.py tests/execution/test_sameday_premarket_protection.py tests/execution/test_asset_eligibility_gate.py tests/execution/test_position_ownership.py -q
```

- [ ] **Step 7** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/execution/regime_blended_sizer.py tests/execution/test_ownership_sizer_block.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(sizer): OPENCLAW_OWNERSHIP_BLOCK gates opens and adds on un-owned tickers

Stream B item 15 enforcement. The blocklist joins the entry-hygiene gate as a
fourth, direction-agnostic cooldown class and reuses _shed, so 'exits and
flattens are never blocked' is structural: not held drops, a flip becomes
close-only, a same-sign increase caps at the held size, a reduce passes through.
Unset flag resolves to an empty set — today's sizing byte-for-byte. The
[ownership] line is emitted from the loader at the call site so it still prints
when OPENCLAW_ENTRY_HYGIENE=0 short-circuits the gate.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: B3 — `position_ownership_clean` system check

**Safety:** system checks are diagnostic probes run on demand
(`python3 -m system_checks --check position_ownership_clean`) and by
`src/agent/run_maintenance.js` (`--mode daily`, Mon–Fri 12:00 ET). Read-only: one SELECT, no
writes, no broker calls. It SKIPs when the table has not been migrated yet or has no rows, so it
is quiet until Task 7's migration lands. Per `src/system_checks/README.md` it returns
`(Status, str)` and must not raise.

**Files:**
- Modify: `src/system_checks/checks/broker.py` — append after `_alpaca_clock_reachable` (ends at the end of file, line 216)
- Test: `tests/system_checks/test_position_ownership_clean.py` (Create)

**Interfaces:**
- Consumes: `system_checks.registry.check` decorator; `system_checks.types.Status`; `position_ownership` table (migration 156); module-level `psycopg2` already imported at `src/system_checks/checks/broker.py:6`
- Produces: registered check `position_ownership_clean`, tags `['broker', 'pipeline']`, requires `['db']`; module function `broker._position_ownership_clean() -> tuple[Status, str]`

- [ ] **Step 1** — Write the failing test file `tests/system_checks/test_position_ownership_clean.py`:

```python
"""position_ownership_clean — the B3 regression probe.

A shortfall means signals are marking a position the broker does not hold
(phantom P&L); an unallocated means shares no strategy owns are sitting in the
book. Both are findings the daily maintenance sweep must surface.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.checks import broker  # noqa: E402
from system_checks.types import Status  # noqa: E402

TODAY = date.today()


class _Cursor:
    """Answers each query by the table/keyword it mentions."""
    def __init__(self, regclass='position_ownership', latest=TODAY, counts=None):
        self.regclass = regclass
        self.latest = latest
        self.counts = counts or {}
        self._last = ''

    def execute(self, sql, params=None):
        self._last = ' '.join(sql.split())

    def fetchone(self):
        if 'to_regclass' in self._last:
            return (self.regclass,)
        if 'MAX(cycle_date)' in self._last:
            return (self.latest,)
        if 'CURRENT_DATE' in self._last:
            return ((TODAY - self.latest).days if self.latest else 0,)
        return (None,)

    def fetchall(self):
        return list(self.counts.items())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, cur):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub/stub')
    monkeypatch.setattr(broker.psycopg2, 'connect', lambda *a, **k: _Conn(cur))


def test_all_ok_passes(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'ok': 12}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.PASS and '12' in detail


def test_unallocated_warns(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'ok': 10, 'unallocated': 2}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.WARN and 'unallocated' in detail


def test_shortfall_fails(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'ok': 10, 'shortfall': 1}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.FAIL and 'SHORTFALL' in detail


def test_shortfall_outranks_unallocated(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'unallocated': 5, 'shortfall': 1}))
    assert broker._position_ownership_clean()[0] is Status.FAIL


def test_missing_table_skips(monkeypatch):
    _wire(monkeypatch, _Cursor(regclass=None))
    status, detail = broker._position_ownership_clean()
    assert status is Status.SKIP and 'migrated' in detail


def test_no_rows_yet_skips(monkeypatch):
    _wire(monkeypatch, _Cursor(latest=None))
    assert broker._position_ownership_clean()[0] is Status.SKIP


def test_stale_ledger_warns(monkeypatch):
    _wire(monkeypatch, _Cursor(latest=TODAY - timedelta(days=9), counts={'ok': 3}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.WARN and 'old' in detail


def test_query_failure_is_a_fail_not_a_raise(monkeypatch):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub/stub')

    def _boom(*a, **k):
        raise RuntimeError('connection refused')
    monkeypatch.setattr(broker.psycopg2, 'connect', _boom)
    status, detail = broker._position_ownership_clean()
    assert status is Status.FAIL and 'RuntimeError' in detail


def test_check_is_registered_with_the_right_tags():
    from system_checks.registry import all_checks
    reg = all_checks()
    assert 'position_ownership_clean' in reg, 'check not registered'
    meta = reg['position_ownership_clean']
    assert 'broker' in meta['tags'] and meta['requires'] == ['db']
```

- [ ] **Step 2** — Run it and confirm the expected failure (`AttributeError: module 'system_checks.checks.broker' has no attribute '_position_ownership_clean'`):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/system_checks/test_position_ownership_clean.py -q
```

- [ ] **Step 3** — Append to `src/system_checks/checks/broker.py`:

```python
_OWNERSHIP_STALE_DAYS = 3


@check(name='position_ownership_clean', tags=['broker', 'pipeline'], requires=['db'])
def _position_ownership_clean():
    """Every ticker's latest position_ownership row is 'ok' — the broker's share
    count and the open signals' claim agree (Stream B item 15).

    A SHORTFALL means signals are marking a position the broker does not hold
    (phantom P&L in every rollup); an UNALLOCATED means shares no strategy owns
    are sitting in the book. SKIPs quietly until migration 156 has been applied
    and the reconcile step has written a cycle."""
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.position_ownership')")
                if (cur.fetchone() or [None])[0] is None:
                    return Status.SKIP, 'position_ownership not migrated yet'
                cur.execute('SELECT MAX(cycle_date) FROM position_ownership')
                latest = (cur.fetchone() or [None])[0]
                if latest is None:
                    return Status.SKIP, 'no position_ownership rows yet'
                cur.execute('SELECT CURRENT_DATE - %s', (latest,))
                age_days = int((cur.fetchone() or [0])[0] or 0)
                cur.execute('SELECT status, COUNT(*) FROM position_ownership '
                            'WHERE cycle_date = %s GROUP BY status', (latest,))
                counts = {r[0]: int(r[1]) for r in (cur.fetchall() or [])}
    except Exception as exc:  # noqa: BLE001
        return Status.FAIL, f'ownership query failed: {type(exc).__name__}: {exc}'[:200]

    if age_days > _OWNERSHIP_STALE_DAYS:
        return Status.WARN, (f'latest ownership ledger is {age_days}d old ({latest}) — '
                             f'is the reconcile step running?')
    shortfall = counts.get('shortfall', 0)
    unallocated = counts.get('unallocated', 0)
    total = sum(counts.values())
    if shortfall:
        return Status.FAIL, (f'{shortfall} ticker(s) SHORTFALL, {unallocated} unallocated '
                             f'of {total} on {latest}')
    if unallocated:
        return Status.WARN, f'{unallocated} unallocated ticker(s) of {total} on {latest}'
    return Status.PASS, f'{total} ticker(s) ok on {latest}'
```

- [ ] **Step 4** — Run the task's test plus the neighbouring broker-check tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/system_checks/test_position_ownership_clean.py tests/system_checks/test_unreconciled_submissions_backlog.py tests/system_checks/test_system_checks_framework.py -q
```

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add src/system_checks/checks/broker.py tests/system_checks/test_position_ownership_clean.py
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "feat(system_checks): position_ownership_clean regression probe

Stream B item 15. Shortfall (signals marking a position the broker does not hold)
FAILs; unallocated (shares no strategy owns) WARNs; a ledger older than 3 days
WARNs so a dead reconcile step is visible. SKIPs until migration 156 lands and
the first cycle is written. Read-only, no broker call.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: changelog entry

**Safety:** documentation only; touches no runtime path.

**Files:**
- Modify: `docs/archive/changelog.md` — insert a new bullet immediately after the `## Recent Changes` heading (line 8), above the existing `2026-09-08 20:40 UTC` entry, per the newest-first convention

**Interfaces:**
- Consumes: nothing
- Produces: nothing

- [ ] **Step 1** — Confirm the insertion point is still the first bullet under `## Recent Changes`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && sed -n '1,12p' docs/archive/changelog.md
```

- [ ] **Step 2** — Insert this bullet immediately after the `## Recent Changes` heading and its blank line, before the existing newest entry:

```markdown
- **2026-09-12: Stream B — broker truth (QuantDinger items 3, 14, 15).** Spec `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` §2, plan `docs/superpowers/plans/2026-09-12-qd-stream-b-broker-truth.md`. **B1 (item 3, pure bug fix, no flag):** a broker stop fill left `signal_pnl` saying `open` — `engine.update_pnl:2051-2058` only infers `close_reason='stop_loss'` at EOD from a parquet close crossing the stop, so the stop-out cooldown (`regime_blended_sizer._load_recent_stopouts`) never saw an intraday fill and the name was re-bought on the next cycle. `afterhours_tp.run_exit_fill_reporter` (every 10 min on `openclaw-afterhours-stop-monitor.timer`) now closes every HELD ledger row on the fill's ticker/side via `open_reconcile.drop_signal_close(reason='stop_loss', closed_at=<broker fill ts>)` — new keyword-only `closed_at` (`pnl_date` stays today, so the ON CONFLICT key and same-day idempotency are unchanged). The first-run seed path closes NOTHING (a missing state file would otherwise mass-close the account's whole fill history) and every close is wrapped so a DB blip cannot cost the tick its ext-hours stop emulation. The `--limit 200` closed-order read is replaced by `stop_reattach.fetch_recent_closed_orders` — newest-first 500-row window plus a `--symbols`-scoped read over the open-signal tickers; the spec's `--after-order-id --direction asc` keyset was NOT used because that cursor is inception-anchored and on `--status closed` would walk the account's whole history oldest-first. **B2 (item 14):** migration 155 adds `broker_fills` (append-only, `activity_id` PK), `alpaca_submissions.filled_at` and `signal_pnl.exit_slippage_bps`. The `reconcile` step's existing paginated FILL-activity poll now lands every activity in `broker_fills` with `ON CONFLICT DO NOTHING`, enriched with `parent_order_id`/`client_order_id`/type/class from a symbol-scoped `--nested` closed-order read (activity records carry none of those, and the REST order model has no parent pointer — the parent is only visible by walking `legs`). A fill whose `parent_order_id` matches a submission's `alpaca_order_id` is that signal's exit: scored against `stop_loss` or `target_1` (`client_order_id` beats `order_type` — `ahsx_` exits are marketable limits emulating a stop) with the same signed adverse-positive convention as the migration-145 entry twin. New `fill_slippage:` line in the daily #trade-reports digest: entry n/mean/median/p90 bp, exit n/mean bp, median `filled_at − submitted_at` latency, signed Σ$ cost, and a verdict vs OUR OWN per-ticker half-spread artifact (OK ≤ 1.5×, WARN ≤ 3×, FAIL above); `n=0` renders `n/a`. **B3 (item 15):** migration 156 adds `position_ownership` (append-only, PK `(cycle_date, ticker)`); the `reconcile` step computes broker signed share count vs what open signals claim (`alpaca_submissions.filled_qty` netted once per ticker by the non-entry fills in `broker_fills` — `execution_signals` has no share column), classifies `ok`/`unallocated`/`shortfall` on asymmetric 0.1 %/0.5 % tolerances with a 1-share dust floor, and logs ONLY on a status transition; the pass SKIPS when the broker position list is unreadable. Enforcement is opt-in: `OPENCLAW_OWNERSHIP_BLOCK=1` makes the sizer shed OPENS and ADDS on non-`ok` tickers through the entry-hygiene gate's existing `_shed` helper, so exits, reduces and flattens are structurally never blocked; unset = byte-identical to today. `[ownership]` line every trade step (emitted from the loader, so it survives `OPENCLAW_ENTRY_HYGIENE=0`), plus system check `position_ownership_clean` (shortfall FAIL, unallocated WARN, >3 d stale WARN). Migrations apply on johnbot's next restart. Deliberate deviation: the migration tests assert DDL TEXT statically (`tests/database/test_stream_b_migration_shape.py`) instead of connecting like `test_sp7_migrations.py`, because pytest on this box reaches the real Postgres and a fleet backtest was running.
```

- [ ] **Step 3** — Verify the file still renders as newest-first and nothing else moved:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && sed -n '1,14p' docs/archive/changelog.md
cd /root/openclaw/.claude/worktrees/qd-adoptions && git diff --stat docs/archive/changelog.md
```

- [ ] **Step 4** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && git add docs/archive/changelog.md
cd /root/openclaw/.claude/worktrees/qd-adoptions && git commit -m "docs(changelog): Stream B — broker truth (items 3, 14, 15)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

### Spec coverage

| Spec requirement (§2) | Task(s) | Where |
|---|---|---|
| B1: classified `stop`/`ah_exit` fills call `drop_signal_close(reason='stop_loss', closed_at=filled_at)` | 2 | `afterhours_tp._close_signals_for_fill` |
| B1: close EVERY open `execution_signals` row on that ticker/side | 2 | `open_reconcile._held_signal_rows` + direction filter |
| B1: idempotent (seen-set + a second layer) | 2 | seen-set file; `drop_signal_close` flips `status='closed'` so `_held_signal_rows` stops returning the row |
| B1: a fill on a ticker with no open signal only logs | 2 | `test_fill_on_ticker_with_no_open_signal_only_logs` |
| B1: keep the Discord post | 2 | `_post_alert(msg, channel='trade-reports')` unchanged, before the close |
| B1: fix `--limit 200` | 1, 2 | `fetch_recent_closed_orders` (see deviation D1) |
| B2: `broker_fills` table with the 12 spec'd columns | 3 | `155_broker_fills.sql` |
| B2: `alpaca_submissions.filled_at` | 3, 4 | migration 155; `_apply_fill` COALESCE write |
| B2: fed by the paginated FILL activity poll, `ON CONFLICT DO NOTHING` | 4 | `ingest_broker_fills` from `fetch_fills_for_date` |
| B2: exit-leg join on `parent_order_id` = submission `alpaca_order_id` | 5 | `_EXIT_CANDIDATE_SQL` |
| B2: `exit_slippage_bps` vs stop/target, signed adverse-positive, on `signal_pnl` | 3, 5 | migration 155; `exit_slippage_bps` + `backfill_exit_slippage` |
| B2: `fill_slippage:` digest line (n, mean/median/p90 entry, n/mean exit, latency median, Σ$, verdict) | 6 | `fill_slippage.format_line` |
| B2: verdict bands from our half-spread artifact, never crypto bands | 6 | `modelled_median_bps` via `load_ticker_cost_bps`; `OK_MULT`/`WARN_MULT` |
| B2: tests — migration applies / ingest dedups / bp math / n=0 "n/a" | 3, 4, 5, 6 | see deviation D2 for "migration applies" |
| B3: nightly in `reconcile`, account vs signal qty, `unknown`, 3 statuses | 7 | `run_ownership_pass` hooked in `alpaca_reconcile.main()` |
| B3: tolerances 0.1 % extra / 0.5 % shortfall / 1-share dust | 7 | `classify` + `test_percentage_tolerances_are_asymmetric` |
| B3: `position_ownership` append-only | 7 | migration 156 + `ON CONFLICT DO NOTHING` |
| B3: log only on status transition | 7 | `transitions` + `load_previous_statuses` |
| B3: `OPENCLAW_OWNERSHIP_BLOCK=1` blocks opens/adds, never exits/flattens | 8 | `_shed` reuse; 6 shed-semantics tests |
| B3: `[ownership]` line in the trade step | 8 | `_ownership_blocklist_from` logger call at the `_emit_orders_from_targets` site |
| B3: system check `position_ownership_clean` | 9 | `src/system_checks/checks/broker.py` |
| §0: log to `docs/archive/changelog.md`, newest first | 10 | — |

### Deviations from the spec, stated

- **D1 — B1 pagination.** The spec says "use the keyset loop at :141-150 (`--after-order-id --direction asc`)". That cursor is ascending and inception-anchored; on `--status closed` it walks the account's entire order history oldest-first, and the 10-minute reporter would exhaust its page budget before reaching today's fills. Grep-verified that no `order list` call site in the repo uses a time bound and that the CLI's flags are `--status / --symbols / --limit / --direction / --after-order-id / --nested` (`stop_reattach.py:309-315` documents the newest-500 default window). The implemented fix keeps the default newest-first window, widens 200 → 500, and adds a `--symbols`-scoped read over the open-signal tickers so the names that can actually be closed always land in the window.
- **D2 — "migration applies" test.** `tests/database/test_sp7_migrations.py` proves this by connecting to the real Postgres. The plan's own constraints (and the running fleet backtest) forbid that, so migrations 155/156 are covered by a static DDL-shape test that also cross-checks `alpaca_reconcile._BROKER_FILL_COLUMNS` against the declared columns, so code/DDL drift fails.
- **D3 — `drop_signal_close(..., closed_at=…)`.** The spec calls a signature that does not exist: the function at `open_reconcile.py:68` takes `(cur, signal_id, ticker, closed_price, reason)` and hard-codes `closed_at = date.today()`. Task 2 adds a keyword-only `closed_at` (Task 2 Step 3) and deliberately leaves `pnl_date` at today so the `ON CONFLICT (signal_id, pnl_date)` key — and therefore same-day idempotency — is unchanged.
- **D4 — B2 order-shape columns.** The spec says `broker_fills` is "fed by the existing paginated `account activity list --activity-types FILL` poll". Alpaca activity records carry only `id / order_id / symbol / side / qty / price / transaction_time / order_status`; `parent_order_id`, `client_order_id`, `order_type` and `order_class` do not exist on them, and the REST order model has no parent pointer either (see `alpaca_replace_stop.py:79-119`, which derives the parent by walking `legs`). Those four are enriched from a symbol-scoped `--nested` closed-order read, NULL when the order is outside that window, with a counted `N without a parent_order_id` log line so a silent coverage loss shows up as a log finding, not just as `exit n=0`.
- **D5 — B3 `signal_qty`.** `execution_signals` has no share-quantity column, so the count comes from `alpaca_submissions.filled_qty`, netted once per ticker by the non-entry fills in `broker_fills`. Stated limitation: before `broker_fills` accumulates a day of history, partially-exited positions read `shortfall` — which is why B3 ships report-only and `OPENCLAW_OWNERSHIP_BLOCK` stays unset.
- **D6 — exit-level selection.** The spec says "vs the signal's stop/target level" without saying how to pick. `exit_level_kind` keys on `client_order_id` BEFORE `order_type` because `ahsx_` after-hours exits are marketable LIMITS emulating a stop; typing alone would score every emulated stop against `target_1`.

### Placeholder scan

Searched the plan for `TBD`, `similar to Task`, `add error handling`, `write tests`, `<fill in>`, `FIXME`, and bare `pass` bodies: none present outside this sentence. Every test file is given in full; every implementation block is complete runnable code. Deliberately repeated rather than cross-referenced: the `PARAMS` dict and the `_gate` helper in Task 8's test (rather than importing from `tests/execution/test_entry_hygiene_gate.py`), the `_Cursor`/`_Conn` fakes in Tasks 2, 4, 5, 7 and 9 (each shaped for its own module's access pattern), and the `broker_fills` column list in both the migration (Task 3), the ingest constant (Task 4) and the cross-check test.

### Signature consistency

| Symbol | Defined in | Consumed by | Match |
|---|---|---|---|
| `fetch_recent_closed_orders(symbols=None, *, include_unscoped=True, page=500, chunk=100, timeout=45)` | Task 1 | Task 2 (`symbols=…`), Task 4 (`_syms, include_unscoped=False`) | ✓ |
| `drop_signal_close(cur, signal_id, ticker, closed_price, reason='signal_dropped', *, closed_at=None)` | Task 2 | Task 2 `_close_signals_for_fill`; existing callers `reconcile_broker_closes:1077`, `close_stale_trackers`, `flatten_signal_close:227` all pass positionally + `reason=` only — unaffected | ✓ |
| `_coerce_close_date(value, fallback)` | Task 2 | `drop_signal_close` | ✓ |
| `_held_signal_rows(cur, ticker) -> [(signal_id, direction)]` | existing, `open_reconcile.py:528` | Task 2 | ✓ verified against source |
| `classify_exit_fills(orders)` emits `{id, symbol, side, qty, price, level, kind, filled_at}` | existing, `afterhours_tp.py:562-609` | Task 2 | ✓ verified against source |
| `_BROKER_FILL_COLUMNS` | Task 4 | Task 3's shape test cross-check | ✓ |
| `build_order_meta(orders)` / `ingest_broker_fills(cur, fills, order_meta=None, *, dry_run=False)` | Task 4 | `reconcile()` | ✓ |
| `exit_level_kind` / `exit_slippage_bps` / `plan_exit_slippage` / `backfill_exit_slippage(cur, run_date, *, lookback_days=5, dry_run=False)` | Task 5 | `reconcile()` (passes `dry_run=dry_run`) | ✓ |
| `load_ticker_cost_bps() -> dict | None` | existing, `unified_backtest.py:101` | Task 6 `modelled_median_bps` | ✓ verified against source |
| `fill_slippage_line(run_date, *, conn=None, cost_bps=None)` | Task 6 | `send_report.main()` | ✓ |
| `fetch_positions() -> list | None` | existing, `stop_reattach.py:230` | Task 7 `load_account_qty` | ✓ verified (None == CLI failure) |
| `classify` / `compute_ownership` / `transitions` / `persist_ownership` / `latest_status_map` / `run_ownership_pass(conn, cycle_date, *, dry_run=False, account_qty=None, log_fn=None)` | Task 7 | Task 8 (`latest_status_map`, `STATUS_OK`), Task 9 (table only), `alpaca_reconcile.main()` | ✓ |
| `_apply_entry_hygiene_gate(..., ownership_blocked=None)` | Task 8 | `_emit_orders_from_targets:2469` — grep-verified as the ONLY production caller; the two test-file callers (`test_entry_hygiene_gate.py:32`, `test_sameday_premarket_protection.py:271,282,293,302`) omit the new kwarg, so the gate resolves it to `frozenset()` and never loads | ✓ |
| `@check(name=..., tags=[...], requires=[...])` returning `(Status, str)` | existing, `src/system_checks/README.md` | Task 9 | ✓ |

### Env vars, tables, columns, flags used

`OPENCLAW_OWNERSHIP_BLOCK` (new, Task 8 — unset = today's behaviour), `OPENCLAW_EXIT_FILLS_STATE`
(existing, `afterhours_tp.py:557`), `OPENCLAW_ENTRY_HYGIENE` (existing), `POSTGRES_URI`,
`OPENCLAW_BT_SPREAD_COSTS` (existing kill switch, read inside `load_ticker_cost_bps`).
Tables: `broker_fills` (new, 155), `position_ownership` (new, 156), `alpaca_submissions`,
`execution_signals`, `signal_pnl` — all verified column-by-column against
`012_execution_engine.sql:28-65`, `043_alpaca_submissions.sql`, `064`, `119`, `122`, `126`, `127`,
`145`. Migration numbers 155/156 verified free (`ls src/database/migrations | tail -3` →
152/153/154).

### Line-number drift found while grounding (spec lines are from 2026-09-11)

| Spec citation | Actual (worktree, `d5e6c235`) |
|---|---|
| `afterhours_tp.py:562-610` `classify_exit_fills` | `562-609` ✓ |
| `afterhours_tp.py:614-655` `run_exit_fill_reporter` | `614-655` ✓ |
| `afterhours_tp.py:619-620` `--limit 200` | `619-620` ✓ |
| `afterhours_tp.py:141-150` keyset loop | `142-159` (comment at 141) |
| `regime_blended_sizer.py:2297-2316` `_load_recent_stopouts` | `2297-2317` |
| `engine.py:2049-2057` stop inference | `2051-2058` |
| `alpaca_reconcile.py:55-93` activity paging | `55-93` ✓ |
| `alpaca_reconcile.py:270-277` persisted fields | `265-277` (`_apply_fill` def at 260) |
| `send_report.py:392` digest | `_fmt_closed_positions_digest` def at `390` |
| `send_report.py:658-662` `bench_realized_line` | `659-667` |
| `unified_backtest.py:94-118` half-spread artifact | `load_ticker_cost_bps` at `101-119` (comment from 94) |
| `043_alpaca_submissions.sql:25` `submitted_at` | `25` ✓ |
| `open_reconcile.py:1041-1100` `reconcile_broker_closes` | `1041-1085` |
| `open_reconcile.py:976-994` `_derive_close_reason` | `976-993` |
| `stop_reattach.py:129-183` keyset loop | `129-182` |
| `stop_reattach.py:765-803` `audit_naked_positions` | `765-801` |
| `stop_reattach.py:847-905` `sweep_orphan_exits` | `847-904` |
