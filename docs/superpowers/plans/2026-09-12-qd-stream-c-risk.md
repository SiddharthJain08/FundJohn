# Stream C — risk (account breaker, circuit-breaker regimes, macro-event gate, social term) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Land Stream C of the QuantDinger adoptions on branch `worktree-qd-adoptions`:

- **C1** — an alpha-sleeve drawdown + daily-loss account breaker: evaluated every 5 min in RTH, flattens every non-benchmark position on breach, refuses alpha opens/adds while halted, operator-only re-arm.
- **C2** — the per-position circuit breaker stops claiming a HIGH_VOL/CRISIS exemption (ruling R2) and gains a regression test so the exemption cannot come back.
- **C3** — a macro-event calendar master (`data/master/macro_events.parquet`), a T-1..T new-entry block in the sizer, the matching backtest mirror, and a freshness system check + monthly timer.
- **C4** — the premarket panic scorer finally receives the real social term instead of a hardcoded zero (item 16, an operator-approved pure bug fix).

C1 and C3 ship SHADOW-first: the flag-unset path is byte-identical to today's behaviour and only emits a grep-able log line.

## Architecture

```
                       ┌──────────────────────────────────────────┐
 5-min RTH cron ──────►│ position_circuit_breaker.py  (C2)        │  per-position NAV cutoff, ALL regimes
 (cron-schedule.js)    ├──────────────────────────────────────────┤
                  └───►│ account_breaker.py           (C1)        │  alpha-NAV dd <= -10% OR equity daily <= -3%
                       │   ├─ reads: alpaca account equity,       │
                       │   │         alpaca position list,        │
                       │   │         benchmark_sleeve ids,        │
                       │   │         logs/pnl_daily_ohlc.json     │
                       │   ├─ writes: account_breaker_state,      │
                       │   │          account_daily_open,         │
                       │   │          circuit_breaker_fires       │
                       │   └─ acts:  regime_liquidator._close_symbol (RTH, poll-to-terminal)
                       └──────────────────────────────────────────┘
                                        │ halted=true
                                        ▼
 regime_blended_sizer._emit_orders_from_targets
   _apply_asset_eligibility_gate  (existing)
   _apply_entry_hygiene_gate      (existing)
   _apply_account_breaker_gate    (C1)   ─┐ both clamp with only-shed semantics
   _apply_macro_event_gate        (C3)   ─┘ via the shared _clamp_to_held helper
   _apply_net_exposure_cap        (existing, stays LAST)

 data/master/macro_events.parquet  <-- src/ingestion/ingest_macro_events.py (Fed/BLS/BEA, monthly timer)
        │
        └──► src/lib/macro_events.py  gated_sessions()  ──►  sizer gate (C3)
                                                        ──►  unified_backtest mirror (C3)
                                                        ──►  system_checks macro_events_fresh (C3)

 run_premarket_scan._load_social_for_tickers (C4) ──► ticker_sentiment_daily ──► ScoreInputs.social_*
```

Two orthogonal risk levers, deliberately asymmetric per ruling R2:

- **drawdown** is measured on **alpha NAV** (`equity − Σ market_value of benchmark tickers`) against a persisted rolling peak;
- **daily loss** is measured on **total equity** against the session's opening equity.

Do not "harmonize" them. The sleeve is allowed to lose on an SPY down day without the alpha book being blamed for it, and the alpha book is measured net of beta.

## Tech Stack

- Python 3.11, `pandas` / `pyarrow` (parquet masters), `psycopg2` (Postgres), stdlib `urllib.request` for keyless HTTP. No new packages.
- Node 20 for `src/engine/cron-schedule.js` (wiring only — no new cron expression, no new thread).
- pytest (`pytest.ini`: `testpaths=tests`, `pythonpath=src`; root `conftest.py` snapshots/restores `os.environ` per test; `tests/execution/conftest.py` stubs sizer gates and the benchmark-sleeve loaders).
- systemd units are authored as files under `docs/systemd/` and installed by the operator; no unit is installed by this plan.

## Spec

`docs/specs/2026-09-12-quantdinger-adoptions-spec.md` — §0 (non-negotiables) and §3 (Stream C: C1, C2, C3, C4). Operator rulings R2 and R3 in the spec header are binding.

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

Stream-C additions (binding for every task):

- Ruling **R2**: HIGH_VOL and CRISIS are **not exempt** from either breaker. No code path in this stream may read the regime to decide whether to act.
- Ruling **R3**: the event gate **blocks new entries** (never exits); all regimes; the benchmark sleeve is included unless `OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1`.
- Items 16 (C4) and the circuit-breaker regime change (C2) are operator-approved pure bug fixes and ship **without** a new env flag. C1 and C3 ship **with** flags, shadow-first.

Test rules (binding for every task):

- Run ONLY the task's own tests plus the touching module's existing tests, always as
  `cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/<path> -q`.
- NEVER run the whole suite (`python3 -m pytest`), never `tests/backtest/test_regime_stratified_backtest*`.
- Every test stubs its DB, CLI and network surfaces in fixtures (`monkeypatch.setattr` / `unittest.mock.patch`). A test must pass with Postgres down and the network unplugged.
- NEVER call the `alpaca` CLI from a test, directly or transitively; patch `_run_cli`, `_load_broker_positions`, `_close_symbol`, `_market_is_open` instead.
- No test may read `data/` masters; write synthetic parquet into `tmp_path` and point the module's `*_PATH` env override at it.
- A fleet backtest may be running: keep every pytest invocation short and single-process (no `-n`).

## File Structure

New files:

| Path | Responsibility |
|---|---|
| `src/execution/account_breaker.py` | C1: pure rule evaluation (alpha NAV, rolling peak, dd, daily loss), state persistence, re-arm token, flatten action, shadow/armed line, `main()` for the 5-min cron. |
| `src/database/migrations/157_account_breaker.sql` | C1: `account_breaker_state` (singleton row) + `account_daily_open` tables. Idempotent. |
| `src/lib/macro_events.py` | C3: reader for `data/master/macro_events.parquet` — `master_path()`, `HIGH_IMPORTANCE`, `load_events()`, `gated_sessions()`, `gating_event()`. |
| `src/ingestion/ingest_macro_events.py` | C3: Fed / BLS / BEA keyless parsers → `data/master/macro_events.parquet` via `append_dedup` on `(event, scheduled_at)`; `--from-file` and `--backfill` paths. |
| `src/system_checks/checks/macro_events_freshness.py` | C3: `macro_events_fresh` — the master must carry a high-importance event >= 30 d ahead. |
| `docs/systemd/openclaw-macro-events.service` | C3: monthly ingest unit (snapshot only; operator installs). |
| `docs/systemd/openclaw-macro-events.timer` | C3: `*-*-02 07:00:00 UTC`, `Persistent=true`. |
| `tests/fixtures/macro_events/fed_fomccalendars.html` | C3: hand-authored Fed calendar fixture. |
| `tests/fixtures/macro_events/bls_cpi_sched.html` | C3: hand-authored BLS CPI schedule fixture. |
| `tests/fixtures/macro_events/bls_empsit_sched.html` | C3: hand-authored BLS Employment Situation schedule fixture. |
| `tests/fixtures/macro_events/bea_schedule.html` | C3: hand-authored BEA release-schedule fixture. |
| `tests/execution/test_account_breaker_rules.py` | C1: pure-rule tests (dd, daily, peak, alpha NAV, regime-independence). |
| `tests/execution/test_account_breaker_state.py` | C1: state persistence, re-arm token semantics, line format. |
| `tests/execution/test_account_breaker_flatten.py` | C1: flatten action — bench excluded, RTH-only, retry, `pending_flatten`, `circuit_breaker_fires` rows. |
| `tests/execution/test_account_breaker_sizer_gate.py` | C1: `_apply_account_breaker_gate` only-shed semantics. |
| `tests/execution/test_account_breaker_cron_wiring.py` | C1: `cron-schedule.js` spawns the breaker inside the existing 5-min RTH cron. |
| `tests/execution/test_macro_event_gate.py` | C3: `_apply_macro_event_gate` semantics + bench exemption flag. |
| `tests/lib/test_macro_events.py` | C3: `gated_sessions()` / `gating_event()` over a synthetic parquet. |
| `tests/ingestion/test_ingest_macro_events.py` | C3: Fed/BLS/BEA parsers against the checked-in fixtures + master merge. |
| `tests/system_checks/test_macro_events_freshness.py` | C3: the freshness check PASS/FAIL/SKIP matrix. |
| `tests/backtest/test_event_gate_backtest_mirror.py` | C3: `_per_bar_simulate` skips entries on gated sessions when the BT flag is set. |
| `tests/sentiment/test_premarket_social_wiring.py` | C4: fake-cursor social loader + scorer receives non-zero social. |

Modified files:

| Path | Change |
|---|---|
| `src/pipeline/run_premarket_scan.py` | C4: `_load_social_for_tickers()`, real social values into `ScoreInputs` / `PremarketConfirmerInput` / the persisted row, `social_source` in a new per-ticker log line. |
| `tests/pipeline/test_premarket_scan.py` | C4: patch the new loader in the four `run_scan` tests. |
| `src/execution/position_circuit_breaker.py` | C2: docstring no longer claims a HIGH_VOL/CRISIS skip. |
| `tests/execution/test_position_circuit_breaker.py` | C2: regression tests that no regime branch exists and all four regimes are seeded. |
| `src/execution/regime_blended_sizer.py` | C1/C3: `_clamp_to_held`, `_apply_account_breaker_gate`, `_apply_macro_event_gate`, wired into `_emit_orders_from_targets`. |
| `src/engine/cron-schedule.js` | C1: second `spawn` of `account_breaker.py` inside the existing `*/5 9-16 * * 1-5` cron. |
| `src/system_checks/checks/master_freshness.py` | C3: `macro_events.parquet` added to `_COVERED_ELSEWHERE`. |
| `src/system_checks/checks/__init__.py` | C3: import the new check module. |
| `src/backtest/unified_backtest.py` | C3: gated-session entry skip in `_per_bar_simulate` + `event_gate` in `config_json`. |
| `docs/archive/changelog.md` | Stream C entry (newest first). |

## Shadow-line contracts (the operator greps these — do not reformat)

**C1**, emitted on EVERY tick (a missing line means the process died, which is why `rule=none` exists):

```
[account_breaker] shadow equity=203145.22 bench_mv=41000.00 alpha_nav=162145.22 peak=171200.00 dd=-0.0529 open_equity=205000.00 open_src=stored daily=-0.0090 rule=none breach=0 halted=0
[account_breaker] armed equity=190100.00 bench_mv=41000.00 alpha_nav=149100.00 peak=171200.00 dd=-0.1291 open_equity=205000.00 open_src=estimated daily=-0.0727 rule=drawdown breach=1 halted=1 flatten_ok=6 flatten_fail=1 pending=1
```

- token 2 is exactly `shadow` when `OPENCLAW_ACCOUNT_BREAKER` != `1`, exactly `armed` when it is `1`;
- `rule` is one of `none | drawdown | daily_loss | drawdown+daily_loss`;
- `open_src` is one of `stored | ohlc | equity` (`equity` = reconstructed from the current equity, so `account_daily_open.estimated=true`);
- the `flatten_ok=/flatten_fail=/pending=` tail appears only when a flatten was attempted (armed + breach).

**C3**, emitted on EVERY sizing cycle:

```
[event_gate] shadow session=2026-09-16 events=CPI@2026-09-16 blocked=12 capped=3 tickers=AAPL,AMD,MSFT bench_exempt=0
[event_gate] shadow session=2026-09-14 events=none blocked=0 capped=0 tickers= bench_exempt=0
[event_gate] armed session=2026-09-16 events=CPI@2026-09-16,FOMC_DECISION@2026-09-17 blocked=12 capped=3 tickers=AAPL,AMD,MSFT bench_exempt=1
```

- token 2 is exactly `shadow` when `OPENCLAW_EVENT_GATE` != `1`, exactly `armed` when it is `1`;
- `blocked` = targets dropped entirely (ticker not held), `capped` = same-sign increases clamped to the held size;
- `tickers` = up to the first 20 affected symbols, sorted, comma-separated (empty string when none).

**Two-clean-shadow-days rule (both C1 and C3):** the flag is set ONLY after two consecutive RTH sessions in which the shadow line appeared on every expected tick/cycle, carried no `failed`/`ERROR` token, and was reviewed by the operator. Record the two dates in the changelog entry (Task 12) at flip time. Nothing in this plan flips a flag; every flip is OPERATOR-RUN.

## Verified ground truth (re-verified 2026-09-13 in this worktree)

| Fact | Location |
|---|---|
| `panic_score` weights `60*neg + 30*min(n*10,100)/100 + 10*social_bear`; returns `0.0` when `news_count_window < 1` | `src/sentiment/premarket_scorer.py:38-46` |
| `ScoreInputs.social_post_count_window` declared, never read | `src/sentiment/premarket_scorer.py:17` |
| `social_post_count_window=0` / `social_bear_ratio=0.0` hardcoded | `src/pipeline/run_premarket_scan.py:191-192`, `207-208`, `231` |
| `_evaluate_ticker(position, cfg, scan_ts, scan_label, window_start)` | `src/pipeline/run_premarket_scan.py:181-182`; sole caller at `:276-279` |
| `premarket_panic_alerts` insert column tuple | `src/pipeline/run_premarket_scan.py:120-130` |
| `ticker_sentiment_daily(ticker, date, social_posts_24h, social_bull_ratio, social_bear_ratio, social_unique_authors, social_top_themes, news_count_24h, news_finbert_pos/neu/neg, news_mean_score, news_top_headlines, updated_at)`; PK `(ticker, date)` | `src/database/migrations/106_ticker_sentiment_daily.sql:5-25`; writer `src/ingestion/sentiment_storage.py:22-51` |
| Existing per-date reader pattern to copy | `src/execution/trade_handoff_builder.py:214-264` |
| **The HIGH_VOL/CRISIS regime skip is ALREADY GONE from the code** — only the module docstring still claims it | `src/execution/position_circuit_breaker.py:8-9` (stale claim) vs `:71-76` (removal comment) |
| All four regimes seeded with a positive `position_circuit_breaker_pct` (0.020 / 0.015 / 0.010 / 0.005), column `NOT NULL CHECK (> 0)` | `src/database/migrations/069_regime_blended_sizer.sql:9,12-18` |
| 5-min RTH cron that spawns the position breaker | `src/engine/cron-schedule.js:810-829` (`cron.schedule('*/5 9-16 * * 1-5', …)`) |
| NAV OHLC store path + `{open,high,low,close}` per ET date | `src/execution/bench_realized.py:23,39-43`; writer `src/channels/api/server.js:2547,2599-2626` (`samplePnlCandle`) |
| `_close_symbol(symbol, qty, market_open=None) -> (ok, payload)`; cancel-then-close, partial-flatten payload | `src/execution/regime_liquidator.py:281-352` |
| `_market_is_open() -> bool`, default False on failure | `src/execution/regime_liquidator.py:115-123` |
| `_load_broker_positions() -> {symbol: {qty, side, market_value}}` | `src/execution/regime_liquidator.py:212-233` |
| `_post_to_discord(channel, msg) -> bool` | `src/execution/regime_liquidator.py:139-176` |
| `circuit_breaker_fires(ts_utc, ticker, unrealized_pnl_pct_nav, threshold_pct, position_qty, close_result_json)` — all NOT NULL | `src/database/migrations/069_regime_blended_sizer.sql:62-71` |
| Risk-exit cooldown reads `circuit_breaker_fires`, skipping `close_result_json->>'dry_run' = 'true'` | `src/execution/regime_blended_sizer.py:2247-2280` |
| Broker-side close ledger auto-corrects from `circuit_breaker_fires` | `src/execution/open_reconcile.py:1004-1043` (`_derive_close_reason`, `_closed_today_tickers`) |
| `flatten_signal_close(cur, signal_id, ticker, closed_price)` → `drop_signal_close(reason='flattened')` | `src/execution/open_reconcile.py:254-279`, `:87-95` |
| Only-shed clamp pattern to copy (`_shed`) | `src/execution/regime_blended_sizer.py:2398-2405` |
| Gate chain insertion point | `src/execution/regime_blended_sizer.py:2466-2472` |
| Benchmark sleeve ids / tickers | `src/execution/benchmark_sleeve.py:23-64` |
| `trading_calendar` public API + `MASTER_PATH_ENV` override + `clear_cache()` | `src/lib/trading_calendar.py:28,34,92,139-185` |
| Ingester conventions (`_headers`, `_http_get`, `merge_into_master` → `append_dedup(..., mode='replace')`) | `src/ingestion/ingest_nasdaq_earnings_calendar.py:182-230` |
| `append_dedup(path, new_df, key_cols, mode)` — DuckDB-bounded, atomic, file-locked | `src/data/parquet_store.py:83-120` |
| System-check contract + `_COVERED_ELSEWHERE` | `src/system_checks/README.md`; `src/system_checks/checks/master_freshness.py:77-78` |
| `_per_bar_simulate` loop `for current_date in oos_dates:`; counters init; entry acceptance; return dict | `src/backtest/unified_backtest.py:859`, `:825-830`, `:945-1059`, `:1112-1124` |
| Run provenance literal written to `strategy_backtest_runs.config_json` | `src/backtest/unified_backtest.py:1439-1470` |
| Migrations apply via `postgres.js migrate()` on johnbot start; latest existing = `154`; Stream B reserves 155 + 156 | `src/database/postgres.js:42-55`, `src/channels/discord/bot.js:1662-1663`; `ls src/database/migrations \| tail` |

## Line-number drift found vs the spec (already corrected above)

- spec `C1` cites `bench_realized.py:23,40-43` → actual `:23, :39-43`; `server.js:2546,2671` → the sampler is `samplePnlCandle` at `:2599-2626`, the store path constant at `:2547`.
- spec `C1` cites `cron-schedule.js:810-820` → the cron block runs `:810-829`.
- spec `C4` cites `run_premarket_scan.py:...,231` → the confirmer's `social_bear_ratio=0.0` is at `:231`, correct; the row dict zeros are at `:207-208`, correct.
- spec `C1` cites `open_reconcile.flatten_signal_close` at "~227" → actual `:254`.
- spec `C2` says "remove the … branch" at `:8-9` → **there is no branch left**; `:71-76` records its removal on 2026-05-16. C2 is a docstring correction plus regression coverage (see Task 2).

---

### Task 1 — C4: wire the real social term into the premarket scan

**Files:**
- `src/pipeline/run_premarket_scan.py` (modify)
- `tests/sentiment/test_premarket_social_wiring.py` (new)
- `tests/pipeline/test_premarket_scan.py` (modify — patch the new loader in the four `run_scan` tests)

**Interfaces:**

Consumes:
- `ScoreInputs(news_count_window:int, news_finbert_neg_ratio:float, news_finbert_mean_score:float, social_post_count_window:int, social_bear_ratio:float)` and `panic_score(inp) -> float` — `src/sentiment/premarket_scorer.py:12-46`. **Unchanged by this task**, including the `news_count_window < 1 => 0.0` precondition.
- Postgres table `ticker_sentiment_daily(ticker TEXT, date DATE, social_posts_24h INT NOT NULL DEFAULT 0, social_bear_ratio NUMERIC, …)`, PK `(ticker, date)` — `src/database/migrations/106_ticker_sentiment_daily.sql:5-25`.
- `PremarketConfirmerInput(ticker, held_qty, panic_score, news_count, finbert_neg_ratio, social_bear_ratio, top_headlines)` — `src/sentiment/sonnet_premarket_confirmer.py`.

Produces (all new in `src/pipeline/run_premarket_scan.py`):
- `_social_max_age_days() -> int` (env `OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS`, default `3`)
- `_social_rows_from_cursor(cur, tickers: list[str], today: date, max_age_days: int) -> dict[str, dict]` — pure over an open cursor; values `{'social_posts_24h': int, 'social_bear_ratio': float, 'social_source': str}`
- `_load_social_for_tickers(tickers, today: date) -> dict[str, dict]` — fail-open `{}` wrapper that owns the connection
- `_evaluate_ticker(position, cfg, scan_ts, scan_label, window_start, social=None) -> dict` — signature gains a `social` map (default `None` ⇒ every ticker scores `social_source=absent`)

Log line produced (one per scanned ticker):
```
[premarket] ticker=SNDK news=7 neg=0.571 social_posts=140 social_bear=0.620 social_source=ticker_sentiment_daily:2026-09-12 score=71.2 advisory=True
```

- [ ] **Step 1** — Write the failing test file `tests/sentiment/test_premarket_social_wiring.py`:

```python
"""C4 (item 16): the pre-market scan reads the real per-ticker social row from
ticker_sentiment_daily instead of hardcoding zeros, and reports where the value
came from via `social_source` in its log line.

Every DB surface is a fake cursor — these tests must pass with Postgres down.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from src.pipeline import run_premarket_scan as mod
from src.sentiment.premarket_scorer import ScoreInputs, panic_score


class FakeCursor:
    """Minimal psycopg2 cursor stand-in: records the SQL + params, replays rows."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self._rows)


TODAY = date(2026, 9, 13)


# ── _social_rows_from_cursor ────────────────────────────────────────────────

def test_rows_from_cursor_maps_posts_and_bear_ratio():
    cur = FakeCursor([('SNDK', date(2026, 9, 12), 140, 0.62)])
    out = mod._social_rows_from_cursor(cur, ['SNDK'], TODAY, 3)
    assert out == {
        'SNDK': {
            'social_posts_24h': 140,
            'social_bear_ratio': 0.62,
            'social_source': 'ticker_sentiment_daily:2026-09-12',
        }
    }


def test_rows_from_cursor_bounds_the_date_window():
    cur = FakeCursor([])
    mod._social_rows_from_cursor(cur, ['AAPL', 'MSFT'], TODAY, 3)
    assert cur.params == (['AAPL', 'MSFT'], TODAY, date(2026, 9, 10))
    assert 'DISTINCT ON (ticker)' in cur.sql
    assert 'ORDER BY ticker, date DESC' in cur.sql


def test_rows_from_cursor_null_bear_ratio_becomes_zero():
    cur = FakeCursor([('AMD', date(2026, 9, 13), 0, None)])
    out = mod._social_rows_from_cursor(cur, ['AMD'], TODAY, 3)
    assert out['AMD']['social_bear_ratio'] == 0.0
    assert out['AMD']['social_posts_24h'] == 0


def test_load_social_is_fail_open(monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    assert mod._load_social_for_tickers(['AAPL'], TODAY) == {}


def test_load_social_short_circuits_on_empty_tickers(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError('must not open a connection for an empty ticker list')

    monkeypatch.setattr(mod.psycopg2, 'connect', _boom)
    assert mod._load_social_for_tickers([], TODAY) == {}


def test_social_max_age_days_env_override(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS', '7')
    assert mod._social_max_age_days() == 7
    monkeypatch.setenv('OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS', 'nonsense')
    assert mod._social_max_age_days() == 3
    monkeypatch.delenv('OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS', raising=False)
    assert mod._social_max_age_days() == 3


# ── _evaluate_ticker wiring ─────────────────────────────────────────────────

def _cfg(confirmer=False):
    return mod.ScanConfig(
        scan_enabled=True, confirmer_enabled=confirmer, autoclose_enabled=False,
        advisory_threshold=35.0, autoclose_min_severity=4,
        max_tickers_per_scan=25, confirmer_budget_usd=0.5,
    )


def _news(monkeypatch, count=5, neg=0.4):
    monkeypatch.setattr(mod, 'score_news_for_tickers', lambda tickers, start: [{
        'news_count_24h': count, 'news_finbert_neg': neg, 'news_mean_score': -0.2,
        'news_top_headlines': [], 'evidence_uuids': [],
    }])


SCAN_TS = datetime(2026, 9, 13, 11, 30, tzinfo=timezone.utc)
WINDOW = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
POS = {'symbol': 'SNDK', 'qty': 10, 'avg_entry_price': 100.0}


def test_evaluate_ticker_passes_social_through_to_the_score(monkeypatch):
    _news(monkeypatch)
    social = {'SNDK': {'social_posts_24h': 140, 'social_bear_ratio': 1.0,
                       'social_source': 'ticker_sentiment_daily:2026-09-12'}}
    row = mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social=social)
    assert row['social_post_count_window'] == 140
    assert row['social_bear_ratio'] == 1.0
    # 60*0.4 + 30*min(50,100)/100 + 10*1.0 = 24 + 15 + 10 = 49
    assert row['panic_score'] == 49.0


def test_evaluate_ticker_absent_social_scores_as_zero(monkeypatch):
    _news(monkeypatch)
    row = mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social={})
    assert row['social_post_count_window'] == 0
    assert row['social_bear_ratio'] == 0.0
    assert row['panic_score'] == 39.0     # 24 + 15 + 0


def test_evaluate_ticker_logs_social_source(monkeypatch, caplog):
    _news(monkeypatch)
    social = {'SNDK': {'social_posts_24h': 3, 'social_bear_ratio': 0.5,
                       'social_source': 'ticker_sentiment_daily:2026-09-11'}}
    with caplog.at_level(logging.INFO, logger=mod.log.name):
        mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social=social)
    line = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'social_source=ticker_sentiment_daily:2026-09-11' in line
    assert 'social_posts=3' in line


def test_evaluate_ticker_logs_absent_source(monkeypatch, caplog):
    _news(monkeypatch)
    with caplog.at_level(logging.INFO, logger=mod.log.name):
        mod._evaluate_ticker(POS, _cfg(), SCAN_TS, '07:30', WINDOW, social=None)
    assert 'social_source=absent' in '\n'.join(r.getMessage() for r in caplog.records)


def test_confirmer_receives_the_real_social_bear_ratio(monkeypatch):
    _news(monkeypatch, count=9, neg=0.9)
    seen = {}

    class _Result:
        verdict = 'bearish_news_driven'
        severity = 5
        rationale = 'r'
        evidence_uuids: list = []
        cost_usd = 0.01

    def _confirm(inp, max_budget_usd=None):
        seen['social_bear_ratio'] = inp.social_bear_ratio
        return _Result()

    monkeypatch.setattr(mod, 'confirm_panic', _confirm)
    social = {'SNDK': {'social_posts_24h': 50, 'social_bear_ratio': 0.75,
                       'social_source': 'ticker_sentiment_daily:2026-09-12'}}
    mod._evaluate_ticker(POS, _cfg(confirmer=True), SCAN_TS, '07:30', WINDOW,
                         social=social)
    assert seen['social_bear_ratio'] == 0.75


# ── the scorer's MVP precondition is deliberately preserved ─────────────────

def test_pure_social_with_no_news_still_scores_zero():
    """news_count_window < 1 => 0.0 is a documented precondition
    (premarket_scorer.py:35-39). Item 16 wires the INPUT; it must not turn the
    scan into a pure-social scorer."""
    assert panic_score(ScoreInputs(0, 0.0, 0.0, 500, 1.0)) == 0.0
```

- [ ] **Step 2** — Run it and confirm it fails for the right reason (no `_social_rows_from_cursor`, `_evaluate_ticker` has no `social` kwarg):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/sentiment/test_premarket_social_wiring.py -q
```
Expected: collection succeeds, then `AttributeError: module 'src.pipeline.run_premarket_scan' has no attribute '_social_rows_from_cursor'` and `TypeError: _evaluate_ticker() got an unexpected keyword argument 'social'`.

- [ ] **Step 3** — Implement the loader. In `src/pipeline/run_premarket_scan.py`, insert immediately after `STRICT_AUTOCLOSE_VERDICTS = {...}` (currently line 45):

```python
SOCIAL_MAX_AGE_DAYS_ENV = 'OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS'
DEFAULT_SOCIAL_MAX_AGE_DAYS = 3


def _social_max_age_days() -> int:
    """How stale a ticker_sentiment_daily row may be and still be used.

    The social stages (Reddit + StockTwits, run_sentiment_step.py:239-264) run
    inside the afternoon compute chain, so the freshest row on a 07:30 ET scan
    is normally YESTERDAY's. 3 days covers a long weekend; anything older is
    treated as absent."""
    try:
        return max(0, int(os.environ.get(SOCIAL_MAX_AGE_DAYS_ENV,
                                         DEFAULT_SOCIAL_MAX_AGE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_SOCIAL_MAX_AGE_DAYS


def _social_rows_from_cursor(cur, tickers, today, max_age_days: int) -> dict:
    """Newest ticker_sentiment_daily social row per ticker within the window.

    Pure over an open cursor so tests can drive it with a fake. Returns
    {ticker: {social_posts_24h, social_bear_ratio, social_source}}; tickers
    with no row in range are simply absent from the map."""
    cur.execute(
        """
        SELECT DISTINCT ON (ticker)
               ticker, date, social_posts_24h, social_bear_ratio
          FROM ticker_sentiment_daily
         WHERE ticker = ANY(%s)
           AND date <= %s
           AND date >= %s
         ORDER BY ticker, date DESC
        """,
        (list(tickers), today, today - timedelta(days=max_age_days)),
    )
    out: dict = {}
    for row in cur.fetchall() or []:
        ticker, row_date, posts, bear = row[0], row[1], row[2], row[3]
        out[ticker] = {
            'social_posts_24h': int(posts or 0),
            'social_bear_ratio': float(bear) if bear is not None else 0.0,
            'social_source': f'ticker_sentiment_daily:{row_date}',
        }
    return out


def _load_social_for_tickers(tickers, today) -> dict:
    """Fail-open owner of the connection. {} on ANY failure — a scan that
    cannot reach Postgres must still score the news term (the status quo
    before item 16), never abort."""
    tickers = list(tickers or [])
    if not tickers:
        return {}
    try:
        dsn = os.environ['POSTGRES_URI']
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            return _social_rows_from_cursor(cur, tickers, today,
                                            _social_max_age_days())
    except Exception as e:  # noqa: BLE001 — social is flavour, never fatal
        log.warning('[premarket] social load failed (%s: %s); scoring with social=0',
                    type(e).__name__, e)
        return {}
```

- [ ] **Step 4** — Wire it into `_evaluate_ticker`. Replace the current signature + inputs block (lines 181-193) with:

```python
def _evaluate_ticker(position: dict, cfg: ScanConfig, scan_ts: datetime,
                     scan_label: str, window_start: datetime,
                     social: dict | None = None) -> dict:
    ticker = position['symbol']
    news_rows = score_news_for_tickers([ticker], window_start)
    n = news_rows[0] if news_rows else None

    soc = (social or {}).get(ticker) or {}
    social_posts = int(soc.get('social_posts_24h') or 0)
    social_bear = float(soc.get('social_bear_ratio') or 0.0)
    social_source = soc.get('social_source') or 'absent'

    inputs = ScoreInputs(
        news_count_window=int(n['news_count_24h'] or 0) if n else 0,
        news_finbert_neg_ratio=float(n['news_finbert_neg'] or 0.0) if n else 0.0,
        news_finbert_mean_score=float(n['news_mean_score'] or 0.0) if n else 0.0,
        social_post_count_window=social_posts,
        social_bear_ratio=social_bear,
    )
```

Immediately after `advisory = score >= cfg.advisory_threshold` (line 195) add:

```python
    log.info('[premarket] ticker=%s news=%d neg=%.3f social_posts=%d '
             'social_bear=%.3f social_source=%s score=%.1f advisory=%s',
             ticker, inputs.news_count_window, inputs.news_finbert_neg_ratio,
             social_posts, social_bear, social_source, score, advisory)
```

Replace the two hardcoded zeros in the persisted row (lines 207-208) with:

```python
        'social_post_count_window': social_posts,
        'social_bear_ratio': social_bear,
```

Replace the confirmer's hardcoded zero (line 231) with:

```python
                social_bear_ratio=social_bear,
```

- [ ] **Step 5** — Wire it into `run_scan`. Replace the `rows = [...]` comprehension (lines 276-279) with:

```python
    social = _load_social_for_tickers(
        [p['symbol'] for p in positions], scan_ts.astimezone(_ET).date())
    rows = [
        _evaluate_ticker(p, cfg, scan_ts, scan_label, window_start, social=social)
        for p in positions
    ]
```

- [ ] **Step 6** — Run the new tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/sentiment/test_premarket_social_wiring.py -q
```

- [ ] **Step 7** — Keep the existing scan tests DB-free. In `tests/pipeline/test_premarket_scan.py`, add
`@patch('src.pipeline.run_premarket_scan._load_social_for_tickers', return_value={})`
as the OUTERMOST decorator (so its mock argument becomes the LAST positional parameter) on the four tests that call `run_scan` and reach the loader — `test_rules_only_path_persists_and_posts_when_score_above_threshold`, `test_confirmer_path_calls_sonnet_only_for_above_threshold`, `test_autoclose_fires_only_when_gate_on_and_strict_severity_met`, `test_autoclose_skipped_on_llm_error_even_when_gate_on` — appending a `_social` parameter to each signature. Add this comment above the first of them:

```python
# C4 (item 16): run_scan now reads ticker_sentiment_daily. src/pipeline modules
# load .env at import, so an unpatched loader would reach the REAL Postgres from
# a unit test. Every run_scan test stubs it to {} (= "no social row", the
# pre-item-16 behaviour these assertions were written against).
```

- [ ] **Step 8** — Run the touching modules' existing tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/pipeline/test_premarket_scan.py tests/sentiment/test_premarket_scorer.py tests/sentiment/test_premarket_social_wiring.py -q
```

- [ ] **Step 9** — Commit (run each line as its own command):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/pipeline/run_premarket_scan.py tests/sentiment/test_premarket_social_wiring.py tests/pipeline/test_premarket_scan.py
git commit -F - <<'MSG'
feat(premarket): wire the real social term into the panic scan (C4, item 16)

ScoreInputs.social_post_count_window / social_bear_ratio were hardcoded to 0
since the MVP; the per-ticker social row has been in ticker_sentiment_daily all
along (written by run_sentiment_step's Reddit + StockTwits stages) and the scan
never read it. Adds a fail-open, date-bounded loader and a per-ticker log line
carrying social_source so the operator can see where the value came from.

Operator-approved pure bug fix (spec 2026-09-12 §0, item 16) — no env flag.
The scorer's `news_count_window < 1 => 0.0` precondition is unchanged.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 2 — C2: the per-position circuit breaker has no regime exemption

**Files:**
- `src/execution/position_circuit_breaker.py` (modify — docstring only)
- `tests/execution/test_position_circuit_breaker.py` (modify — regression coverage)

**Interfaces:**

Consumes:
- `should_fire_breaker(position: dict, nav: float, threshold_pct: float) -> tuple[bool, float]` — `src/execution/position_circuit_breaker.py:27-40`
- `regime_sizer_params(regime_state TEXT PK, …, position_circuit_breaker_pct REAL NOT NULL CHECK (> 0))`, seeded for all four regimes — `src/database/migrations/069_regime_blended_sizer.sql:5-18`

Produces: no new symbols. The module's public surface is unchanged.

**IMPORTANT — the code change the spec asks for is already done.** `main()` reads `market_regime.state` at `:64-70` solely to look up that regime's threshold (`:77-84`); the skip branch was deleted on 2026-05-16 and `:71-76` records why. Only the module docstring at `:8-9` still claims the exemption. So C2 = correct the docstring + add the regression tests that would catch a re-introduction. Do **not** invent a regime branch in order to remove it.

- [ ] **Step 1** — Append the failing regression tests to `tests/execution/test_position_circuit_breaker.py`:

```python
# ── R2 (spec 2026-09-12): NO regime exemption, in code or in the docstring ──

import inspect                                        # noqa: E402
import re                                             # noqa: E402

from execution import position_circuit_breaker as pcb  # noqa: E402

_MIGRATION = (ROOT / 'src' / 'database' / 'migrations'
              / '069_regime_blended_sizer.sql')


def test_module_docstring_does_not_claim_a_regime_skip():
    """Ruling R2: HIGH_VOL/CRISIS are NOT exempt. The docstring was the only
    place that still said they were (2026-09-12)."""
    doc = (pcb.__doc__ or '').lower()
    assert 'are skipped' not in doc
    assert 'independent-mode positions' not in doc
    assert 'all four regimes' in doc


def test_main_has_no_regime_conditional_around_the_fire_path():
    """A `regime_state == 'CRISIS'` style early return must never come back.
    regime_state may only be used as the threshold lookup key and in the
    summary print."""
    src = inspect.getsource(pcb.main)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        for regime in ('HIGH_VOL', 'CRISIS'):
            assert regime not in stripped, f'regime literal in main(): {stripped!r}'
    assert not re.search(r'if\s+regime_state', src)


def test_all_four_regimes_have_a_positive_breaker_threshold_seeded():
    """The breaker aborts when regime_sizer_params has no row for the live
    regime, so "no exemption" also means "every regime is seeded". Asserted
    against the migration text — no DB needed."""
    sql = _MIGRATION.read_text()
    rows = re.findall(
        r"\('(LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS)',\s*[\d.]+,\s*[\d.]+,\s*([\d.]+)\)",
        sql)
    seeded = {r: float(v) for r, v in rows}
    assert set(seeded) == {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert all(v > 0 for v in seeded.values()), seeded


@pytest.mark.parametrize('regime', ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'])
def test_breaker_fires_identically_in_every_regime(regime):
    """should_fire_breaker takes no regime argument at all — the threshold is
    the only per-regime input. Parameterised so the intent shows in the test
    names."""
    thresholds = {'LOW_VOL': 0.020, 'TRANSITIONING': 0.015,
                  'HIGH_VOL': 0.010, 'CRISIS': 0.005}
    pos = {'ticker': 'AAPL', 'qty': 1000, 'avg_entry_price': 100, 'mark': 97.5}
    fire, ratio = should_fire_breaker(pos, 100_000, thresholds[regime])
    assert fire is True
    assert ratio == pytest.approx(-0.025, abs=1e-6)
```

- [ ] **Step 2** — Run and confirm the docstring test fails:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_position_circuit_breaker.py -q
```
Expected: `test_module_docstring_does_not_claim_a_regime_skip` FAILS (`'are skipped' in doc`); the other four PASS — they encode already-true invariants, which is exactly the regression net R2 asks for.

- [ ] **Step 3** — Fix the docstring. Replace lines 1-12 of `src/execution/position_circuit_breaker.py` with:

```python
#!/usr/bin/env python3
"""Intraday 5-min per-position circuit breaker.

Fires in ALL FOUR REGIMES (operator ruling R2, spec 2026-09-12 §3 C2). The old
"independent-mode positions (HIGH_VOL/CRISIS) are skipped — strategy-level
brackets are their cutoff" carve-out was removed from the code on 2026-05-16
(see the comment above the threshold lookup in main()) and is removed from this
docstring on 2026-09-12: with sharpe_cadence-LIVE running in every regime there
is no independent-mode bracket backstop, so losses compound in HIGH_VOL and
CRISIS too. Each regime contributes only its own threshold
(regime_sizer_params.position_circuit_breaker_pct — 2.0 / 1.5 / 1.0 / 0.5 % of
NAV), never an exemption.

Live closes require OPENCLAW_REGIME_BLENDED_LIVE=1; otherwise every fire is
logged to circuit_breaker_fires with close_result_json.dry_run=true and no
order is submitted.

Spec: docs/archive/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md §"position_circuit_breaker"
      docs/specs/2026-09-12-quantdinger-adoptions-spec.md §3 C2 (ruling R2)
"""
```

- [ ] **Step 4** — Run again; expect all PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_position_circuit_breaker.py -q
```

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/execution/position_circuit_breaker.py tests/execution/test_position_circuit_breaker.py
git commit -F - <<'MSG'
fix(risk): drop the stale HIGH_VOL/CRISIS exemption claim from the position breaker (C2, R2)

The skip branch itself was deleted on 2026-05-16; only the module docstring
still advertised it, which is what the QuantDinger review read. Corrects the
docstring and adds the regression net ruling R2 asks for: no regime literal or
`if regime_state` in main(), all four regimes seeded with a positive
position_circuit_breaker_pct, and a per-regime parameterisation of
should_fire_breaker.

Operator-approved pure bug fix (spec 2026-09-12 §0) — no env flag.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 3 — C1a: `account_breaker_state` migration + the pure rule engine

**Files:**
- `src/database/migrations/157_account_breaker.sql` (new — Stream B has reserved 155 and 156)
- `src/execution/account_breaker.py` (new)
- `tests/execution/test_account_breaker_rules.py` (new)

**Interfaces:**

Consumes:
- broker equity `float` (from `execution.alpaca_trader._fetch_account_state(sess)['equity']`, wired in Task 7)
- broker positions `dict[str, dict]` shaped `{symbol: {'qty': float, 'side': str, 'market_value': str}}` — exactly `regime_liquidator._load_broker_positions()` (`src/execution/regime_liquidator.py:212-233`)
- benchmark ticker set `set[str]`

Produces (in `src/execution/account_breaker.py`):
- `ARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER'`, `REARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER_REARM'`, `DD_LIMIT = -0.10`, `DAILY_LIMIT = -0.03`, `BENCH_LOOKBACK_DAYS = 30`
- `armed() -> bool`
- `alpha_nav(equity: float, positions: dict, bench_tickers: set[str]) -> tuple[float, float]` → `(alpha_nav, bench_market_value)`
- `evaluate(alpha: float, peak: float | None, equity: float, opening_equity: float | None) -> dict` → `{'peak','dd','daily','rule','breach'}`

Tables produced (migration 157):
- `account_breaker_state(id INT PK CHECK(id=1), halted BOOL, reason TEXT, breached_at TIMESTAMPTZ, peak_alpha_nav DOUBLE PRECISION, dd DOUBLE PRECISION, daily DOUBLE PRECISION, pending_flatten BOOL, rearmed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)` — singleton row seeded by the migration
- `account_daily_open(session_date DATE PK, opening_equity DOUBLE PRECISION NOT NULL, estimated BOOL NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_account_breaker_rules.py`:

```python
"""C1 pure rule engine (spec 2026-09-12 §3 C1, ruling R2).

  drawdown   alpha_nav / rolling_peak - 1        <= -0.10
  daily loss (equity - opening_equity)/opening   <= -0.03

alpha_nav is net of the benchmark sleeve; the daily rule is on TOTAL equity.
No DB, no CLI, no network in this file.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab  # noqa: E402

MIGRATION = ROOT / 'src' / 'database' / 'migrations' / '157_account_breaker.sql'


# ── migration ───────────────────────────────────────────────────────────────

def test_migration_creates_both_tables_idempotently():
    sql = MIGRATION.read_text()
    assert 'CREATE TABLE IF NOT EXISTS account_breaker_state' in sql
    assert 'CREATE TABLE IF NOT EXISTS account_daily_open' in sql
    # singleton row must exist before the first UPDATE
    assert re.search(r"INSERT INTO account_breaker_state\s*\(id\)\s*VALUES\s*\(1\)", sql)
    assert 'ON CONFLICT' in sql
    assert 'DROP ' not in sql.upper()          # append-only invariant


def test_migration_number_does_not_collide_with_stream_b():
    names = sorted(p.name for p in MIGRATION.parent.glob('15*.sql'))
    assert MIGRATION.name in names
    # Stream B reserved 155 and 156; C1 must not reuse either.
    assert not MIGRATION.name.startswith(('155_', '156_'))


# ── alpha_nav ───────────────────────────────────────────────────────────────

def _pos(**mv):
    return {sym: {'qty': 1.0, 'side': 'long', 'market_value': str(v)}
            for sym, v in mv.items()}


def test_alpha_nav_subtracts_benchmark_market_value():
    alpha, bench_mv = ab.alpha_nav(200_000.0, _pos(SPY=40_000, AAPL=30_000), {'SPY'})
    assert bench_mv == 40_000.0
    assert alpha == 160_000.0


def test_alpha_nav_with_no_benchmark_ticker_equals_equity():
    alpha, bench_mv = ab.alpha_nav(200_000.0, _pos(AAPL=30_000), set())
    assert (alpha, bench_mv) == (200_000.0, 0.0)


def test_alpha_nav_handles_short_benchmark_and_junk_values():
    alpha, bench_mv = ab.alpha_nav(
        100_000.0,
        {'SPY': {'qty': -1, 'market_value': '-25000'},
         'XXX': {'qty': 1, 'market_value': None}},
        {'SPY', 'XXX'})
    assert bench_mv == -25_000.0
    assert alpha == 125_000.0


# ── evaluate ────────────────────────────────────────────────────────────────

def test_no_breach_inside_both_limits():
    st = ab.evaluate(97_000.0, 100_000.0, 199_000.0, 200_000.0)
    assert st['rule'] == 'none' and st['breach'] is False
    assert st['dd'] == pytest.approx(-0.03)
    assert st['daily'] == pytest.approx(-0.005)


def test_drawdown_rule_trips_at_minus_ten_percent():
    st = ab.evaluate(90_000.0, 100_000.0, 200_000.0, 200_000.0)
    assert st['rule'] == 'drawdown' and st['breach'] is True
    assert st['dd'] == pytest.approx(-0.10)


def test_drawdown_just_inside_the_limit_does_not_trip():
    st = ab.evaluate(90_001.0, 100_000.0, 200_000.0, 200_000.0)
    assert st['breach'] is False


def test_daily_loss_rule_trips_at_minus_three_percent():
    st = ab.evaluate(100_000.0, 100_000.0, 194_000.0, 200_000.0)
    assert st['rule'] == 'daily_loss' and st['breach'] is True
    assert st['daily'] == pytest.approx(-0.03)


def test_both_rules_are_reported_together():
    st = ab.evaluate(85_000.0, 100_000.0, 190_000.0, 200_000.0)
    assert st['rule'] == 'drawdown+daily_loss' and st['breach'] is True


def test_peak_ratchets_up_and_never_down():
    st = ab.evaluate(120_000.0, 100_000.0, 200_000.0, 200_000.0)
    assert st['peak'] == 120_000.0 and st['dd'] == 0.0
    st2 = ab.evaluate(110_000.0, 120_000.0, 200_000.0, 200_000.0)
    assert st2['peak'] == 120_000.0


def test_missing_peak_seeds_from_current_alpha_nav():
    st = ab.evaluate(150_000.0, None, 200_000.0, 200_000.0)
    assert st['peak'] == 150_000.0 and st['dd'] == 0.0 and st['breach'] is False


def test_missing_opening_equity_reports_daily_none_and_never_trips_it():
    st = ab.evaluate(100_000.0, 100_000.0, 1.0, None)
    assert st['daily'] is None and st['rule'] == 'none'


def test_non_positive_peak_is_not_a_division_by_zero():
    st = ab.evaluate(-500.0, 0.0, 100.0, 100.0)
    assert st['dd'] == 0.0 and st['rule'] == 'none'


# ── flag + regime independence ──────────────────────────────────────────────

def test_armed_reads_the_flag(monkeypatch):
    monkeypatch.delenv(ab.ARM_ENV, raising=False)
    assert ab.armed() is False
    monkeypatch.setenv(ab.ARM_ENV, '0')
    assert ab.armed() is False
    monkeypatch.setenv(ab.ARM_ENV, '1')
    assert ab.armed() is True


def test_module_never_reads_the_regime():
    """Ruling R2: all regimes. Nothing in this module may branch on regime."""
    src = MIGRATION.parent.parent.parent / 'execution' / 'account_breaker.py'
    text = src.read_text()
    for token in ('market_regime', 'regime_state', 'HIGH_VOL', 'CRISIS', 'LOW_VOL'):
        assert token not in text, f'regime coupling found: {token}'
```

- [ ] **Step 2** — Run and confirm it fails on the missing module:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_rules.py -q
```
Expected: `ModuleNotFoundError: No module named 'execution.account_breaker'` at collection.

- [ ] **Step 3** — Write the migration `src/database/migrations/157_account_breaker.sql`:

```sql
-- 157: account-level risk breaker (spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md
-- §3 C1, operator ruling R2). Two tables, both additive; nothing here deletes
-- or rewrites. Stream B reserved 155 and 156.
--
-- account_breaker_state is a SINGLETON (id = 1): the breaker is an account-wide
-- latch, not a per-ticker fact. History of individual closes already lives in
-- circuit_breaker_fires, which the account breaker writes to as well so
-- open_reconcile.reconcile_broker_closes and the sizer's risk-exit cooldown
-- (_load_recent_risk_exits) pick its flattens up with no new plumbing.
CREATE TABLE IF NOT EXISTS account_breaker_state (
  id               INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  halted           BOOLEAN NOT NULL DEFAULT FALSE,
  reason           TEXT,                 -- 'drawdown' | 'daily_loss' | 'drawdown+daily_loss'
  breached_at      TIMESTAMPTZ,          -- the re-arm token the operator must echo back
  peak_alpha_nav   DOUBLE PRECISION,     -- rolling peak of equity - benchmark market value
  dd               DOUBLE PRECISION,     -- alpha_nav / peak_alpha_nav - 1 at the last tick
  daily            DOUBLE PRECISION,     -- equity / opening_equity - 1 at the last tick
  pending_flatten  BOOLEAN NOT NULL DEFAULT FALSE,
  rearmed_at       TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO account_breaker_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE account_breaker_state IS
  'singleton account-breaker latch, spec 2026-09-12 C1; re-arm is operator-only via OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso>';

-- Opening equity per NYSE session. `estimated` is TRUE when the value was
-- reconstructed from the current equity because neither a stored row nor a
-- logs/pnl_daily_ohlc.json candle for the session was available.
CREATE TABLE IF NOT EXISTS account_daily_open (
  session_date    DATE PRIMARY KEY,
  opening_equity  DOUBLE PRECISION NOT NULL,
  estimated       BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE account_daily_open IS
  'per-session opening equity for the C1 daily-loss rule; estimated=true when reconstructed';
```

- [ ] **Step 4** — Write `src/execution/account_breaker.py` — module header, constants, and the two pure functions only (state, flatten and `main()` land in Tasks 4/5/7):

```python
#!/usr/bin/env python3
"""Account-level risk breaker (spec 2026-09-12 §3 C1, operator ruling R2).

Two rules, evaluated every 5 minutes during RTH by the SAME cron that runs
position_circuit_breaker.py (src/engine/cron-schedule.js) — no new thread:

    drawdown    alpha_nav / rolling_peak(alpha_nav) - 1   <= -0.10
    daily loss  (equity - opening_equity) / opening_equity <= -0.03

alpha_nav = broker equity - the market value of every BENCHMARK-sleeve ticker,
so an SPY-sleeve drawdown never trips the alpha drawdown rule; the daily rule
deliberately measures the WHOLE book. The asymmetry is ruling R2 as written —
do not harmonize the two.

ALL FOUR REGIMES. Nothing in this module reads market_regime.

Flag: OPENCLAW_ACCOUNT_BREAKER=1 arms the ACTION. Unset = SHADOW — the state
row and the `[account_breaker] shadow ...` line are still written every tick,
no order is submitted, and every circuit_breaker_fires row carries
close_result_json.dry_run=true so the sizer's risk-exit cooldown
(_load_recent_risk_exits) ignores it.

Re-arm is operator-only: OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso> in
.env clears exactly that halt (a stale token cannot clear a later breach) and
resets the rolling peak to the current alpha NAV.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

ARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER'
REARM_ENV = 'OPENCLAW_ACCOUNT_BREAKER_REARM'
NAV_OHLC_PATH_ENV = 'OPENCLAW_PNL_OHLC_PATH'
DEFAULT_NAV_OHLC_PATH = ROOT / 'logs' / 'pnl_daily_ohlc.json'

DD_LIMIT = -0.10        # alpha-sleeve drawdown from the rolling peak
DAILY_LIMIT = -0.03     # total-equity loss vs the session's opening equity
BENCH_LOOKBACK_DAYS = 30


def armed() -> bool:
    """True iff the operator has armed the ACTION. Read at call time so a
    .env edit takes effect on the next 5-minute tick without a restart."""
    return os.environ.get(ARM_ENV) == '1'


def nav_ohlc_path() -> Path:
    return Path(os.environ.get(NAV_OHLC_PATH_ENV) or DEFAULT_NAV_OHLC_PATH)


def alpha_nav(equity: float, positions: dict, bench_tickers) -> tuple[float, float]:
    """(alpha_nav, benchmark_market_value).

    `positions` is regime_liquidator._load_broker_positions() shape:
    {symbol: {'qty': float, 'side': str, 'market_value': str}}. A short
    benchmark leg has a negative market value and correctly RAISES alpha NAV.
    Unparseable market values are skipped (logged by the caller's line)."""
    bench_tickers = set(bench_tickers or ())
    bench_mv = 0.0
    for sym, p in (positions or {}).items():
        if sym not in bench_tickers:
            continue
        try:
            bench_mv += float((p or {}).get('market_value') or 0.0)
        except (TypeError, ValueError):
            continue
    return float(equity) - bench_mv, bench_mv


def evaluate(alpha: float, peak, equity: float, opening_equity) -> dict:
    """Pure rule evaluation. Returns
    {'peak', 'dd', 'daily', 'rule', 'breach'}.

    `peak` None (first ever tick) seeds from the current alpha NAV, so the
    breaker can never fire on its own first observation. `opening_equity`
    None/0 disables the daily rule for that tick (reported as daily=None)
    rather than inventing a denominator."""
    alpha = float(alpha)
    peak = alpha if peak is None else max(float(peak), alpha)
    dd = (alpha / peak - 1.0) if peak > 0 else 0.0

    daily = None
    if opening_equity not in (None, 0) and float(opening_equity) > 0:
        daily = float(equity) / float(opening_equity) - 1.0

    rules = []
    if dd <= DD_LIMIT:
        rules.append('drawdown')
    if daily is not None and daily <= DAILY_LIMIT:
        rules.append('daily_loss')

    return {'peak': peak, 'dd': dd, 'daily': daily,
            'rule': '+'.join(rules) if rules else 'none',
            'breach': bool(rules)}
```

- [ ] **Step 5** — Run; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_rules.py -q
```

- [ ] **Step 6** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/database/migrations/157_account_breaker.sql src/execution/account_breaker.py tests/execution/test_account_breaker_rules.py
git commit -F - <<'MSG'
feat(risk): account breaker rule engine + migration 157 (C1, R2)

alpha_nav = equity - benchmark market value; drawdown on alpha NAV vs a
persisted rolling peak (<= -10%), daily loss on TOTAL equity vs the session's
opening equity (<= -3%). The asymmetry is ruling R2 as written. Pure functions
only in this commit; state, flatten and the cron entry point follow.

Tables: account_breaker_state (singleton latch) + account_daily_open. 155/156
are reserved by Stream B.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 4 — C1b: state persistence, opening-equity snapshot, re-arm token, shadow line

**Files:**
- `src/execution/account_breaker.py` (extend)
- `tests/execution/test_account_breaker_state.py` (new)

**Interfaces:**

Consumes:
- `account_breaker_state` / `account_daily_open` (migration 157, Task 3)
- `logs/pnl_daily_ohlc.json` — `{"days": {"YYYY-MM-DD": {"open":f,"high":f,"low":f,"close":f}}}`, written by `samplePnlCandle` (`src/channels/api/server.js:2599-2626`); `open` is the prior session's close by construction (midnight ET rollover), which IS the session's opening equity
- `evaluate(...)` from Task 3

Produces:
- `load_state(cur) -> dict` with keys `halted, reason, breached_at, peak, dd, daily, pending_flatten`
- `save_state(cur, *, halted, reason, breached_at, peak, dd, daily, pending_flatten) -> None`
- `opening_equity(cur, session_date, equity, path=None) -> tuple[float, str]` where source ∈ `stored|ohlc|equity`
- `rearm_requested(state) -> bool`
- `clear_halt(cur, alpha: float) -> None`
- `format_line(mode: str, *, equity, bench_mv, alpha, st, open_equity, open_src, halted, flatten=None) -> str`

- [ ] **Step 1** — Write the failing test file `tests/execution/test_account_breaker_state.py`:

```python
"""C1 state layer: persistence, the opening-equity snapshot, the operator
re-arm token, and the exact shadow/armed line the operator greps.

All DB access goes through a fake cursor; the OHLC store is written into
tmp_path. No Postgres, no CLI, no network.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab  # noqa: E402


class FakeCursor:
    """Replays queued rows in order and records every (sql, params) pair."""

    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        out, self._rows = list(self._rows), []
        return out

    def sql_matching(self, needle):
        return [c for c in self.calls if needle in c[0]]


BREACHED = datetime(2026, 9, 16, 17, 42, 11, tzinfo=timezone.utc)


# ── load_state / save_state ─────────────────────────────────────────────────

def test_load_state_maps_the_singleton_row():
    cur = FakeCursor([(True, 'drawdown', BREACHED, 171_200.0, -0.129, -0.072, True)])
    st = ab.load_state(cur)
    assert st == {'halted': True, 'reason': 'drawdown', 'breached_at': BREACHED,
                  'peak': 171_200.0, 'dd': -0.129, 'daily': -0.072,
                  'pending_flatten': True}


def test_load_state_missing_row_is_a_clean_default():
    st = ab.load_state(FakeCursor([]))
    assert st['halted'] is False and st['peak'] is None and st['breached_at'] is None


def test_save_state_updates_the_singleton_only():
    cur = FakeCursor()
    ab.save_state(cur, halted=False, reason=None, breached_at=None,
                  peak=171_200.0, dd=-0.01, daily=-0.002, pending_flatten=False)
    (sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert 'WHERE id = 1' in sql
    assert params[3] == 171_200.0


# ── opening_equity ──────────────────────────────────────────────────────────

SESSION = date(2026, 9, 16)


def _ohlc(tmp_path, days):
    p = tmp_path / 'pnl_daily_ohlc.json'
    p.write_text(json.dumps({'days': days}))
    return p


def test_opening_equity_prefers_the_stored_row():
    cur = FakeCursor([(205_000.0,)])
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=Path('/nonexistent'))
    assert (value, src) == (205_000.0, 'stored')
    assert cur.sql_matching('INSERT INTO account_daily_open') == []


def test_opening_equity_falls_back_to_todays_ohlc_open(tmp_path):
    cur = FakeCursor([None])
    path = _ohlc(tmp_path, {'2026-09-16': {'open': 205_000.0, 'high': 206_000.0,
                                           'low': 189_000.0, 'close': 190_000.0}})
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    assert (value, src) == (205_000.0, 'ohlc')
    (_sql, params), = cur.sql_matching('INSERT INTO account_daily_open')
    assert params == (SESSION, 205_000.0, False)      # ohlc open is NOT estimated


def test_opening_equity_reconstructs_from_current_equity_and_marks_estimated(tmp_path):
    cur = FakeCursor([None])
    path = _ohlc(tmp_path, {})
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    assert (value, src) == (190_000.0, 'equity')
    (_sql, params), = cur.sql_matching('INSERT INTO account_daily_open')
    assert params == (SESSION, 190_000.0, True)


def test_opening_equity_unreadable_store_still_returns_a_value(tmp_path):
    cur = FakeCursor([None])
    bad = tmp_path / 'broken.json'
    bad.write_text('{not json')
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=bad)
    assert (value, src) == (190_000.0, 'equity')


# ── re-arm token ────────────────────────────────────────────────────────────

def _halted(breached_at=BREACHED):
    return {'halted': True, 'reason': 'drawdown', 'breached_at': breached_at,
            'peak': 171_200.0, 'dd': -0.13, 'daily': -0.07, 'pending_flatten': False}


def test_rearm_requires_the_exact_breached_at_token(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    assert ab.rearm_requested(_halted()) is True


def test_rearm_accepts_the_second_precision_spelling(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, '2026-09-16T17:42:11+00:00')
    assert ab.rearm_requested(_halted()) is True


def test_a_stale_token_cannot_clear_a_later_breach(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, '2026-08-01T14:00:00+00:00')
    assert ab.rearm_requested(_halted()) is False


def test_rearm_is_ignored_when_not_halted(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    st = _halted()
    st['halted'] = False
    assert ab.rearm_requested(st) is False


def test_rearm_absent_token_is_false(monkeypatch):
    monkeypatch.delenv(ab.REARM_ENV, raising=False)
    assert ab.rearm_requested(_halted()) is False


def test_clear_halt_resets_the_peak_to_current_alpha_nav():
    cur = FakeCursor()
    ab.clear_halt(cur, 149_100.0)
    (sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert 'halted = FALSE' in sql and 'rearmed_at = NOW()' in sql
    assert params[0] == 149_100.0


# ── the grep contract ───────────────────────────────────────────────────────

ST_CLEAN = {'peak': 171_200.0, 'dd': -0.0529, 'daily': -0.0090,
            'rule': 'none', 'breach': False}
ST_BREACH = {'peak': 171_200.0, 'dd': -0.1291, 'daily': -0.0727,
             'rule': 'drawdown', 'breach': True}


def test_shadow_line_is_byte_exact():
    line = ab.format_line('shadow', equity=203_145.22, bench_mv=41_000.0,
                          alpha=162_145.22, st=ST_CLEAN, open_equity=205_000.0,
                          open_src='stored', halted=False)
    assert line == (
        '[account_breaker] shadow equity=203145.22 bench_mv=41000.00 '
        'alpha_nav=162145.22 peak=171200.00 dd=-0.0529 open_equity=205000.00 '
        'open_src=stored daily=-0.0090 rule=none breach=0 halted=0')


def test_armed_line_carries_the_flatten_tail():
    line = ab.format_line('armed', equity=190_100.0, bench_mv=41_000.0,
                          alpha=149_100.0, st=ST_BREACH, open_equity=205_000.0,
                          open_src='estimated', halted=True,
                          flatten={'ok': 6, 'fail': 1, 'pending': True})
    assert line.endswith('rule=drawdown breach=1 halted=1 '
                         'flatten_ok=6 flatten_fail=1 pending=1')
    assert line.startswith('[account_breaker] armed ')


def test_line_renders_a_missing_daily_as_na():
    st = dict(ST_CLEAN, daily=None)
    line = ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0, st=st,
                          open_equity=0.0, open_src='equity', halted=False)
    assert ' daily=n/a ' in line
```

- [ ] **Step 2** — Run and confirm the failures are the missing functions:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_state.py -q
```
Expected: `AttributeError: module 'execution.account_breaker' has no attribute 'load_state'` (and `save_state`, `opening_equity`, `rearm_requested`, `clear_halt`, `format_line`).

- [ ] **Step 3** — Append the state layer to `src/execution/account_breaker.py`:

```python
_STATE_COLS = ('halted', 'reason', 'breached_at', 'peak_alpha_nav', 'dd',
               'daily', 'pending_flatten')
_EMPTY_STATE = {'halted': False, 'reason': None, 'breached_at': None,
                'peak': None, 'dd': None, 'daily': None, 'pending_flatten': False}


def load_state(cur) -> dict:
    """The singleton latch. A missing row (pre-migration, or a DB that has
    never run the breaker) is a clean, un-halted default."""
    cur.execute(
        'SELECT halted, reason, breached_at, peak_alpha_nav, dd, daily, '
        'pending_flatten FROM account_breaker_state WHERE id = 1')
    row = cur.fetchone()
    if not row:
        return dict(_EMPTY_STATE)
    halted, reason, breached_at, peak, dd, daily, pending = row
    return {'halted': bool(halted), 'reason': reason, 'breached_at': breached_at,
            'peak': None if peak is None else float(peak),
            'dd': None if dd is None else float(dd),
            'daily': None if daily is None else float(daily),
            'pending_flatten': bool(pending)}


def save_state(cur, *, halted, reason, breached_at, peak, dd, daily,
               pending_flatten) -> None:
    cur.execute(
        """
        UPDATE account_breaker_state
           SET halted = %s, reason = %s, breached_at = %s, peak_alpha_nav = %s,
               dd = %s, daily = %s, pending_flatten = %s, updated_at = NOW()
         WHERE id = 1
        """,
        (bool(halted), reason, breached_at, peak, dd, daily, bool(pending_flatten)),
    )


def opening_equity(cur, session_date, equity, path=None) -> tuple[float, str]:
    """(opening_equity, source) for `session_date`, snapshotting it on first use.

    Order: the stored account_daily_open row -> the session's candle `open` in
    logs/pnl_daily_ohlc.json -> the current equity. The OHLC `open` is the
    PRIOR session's close by construction (the sampler rolls candles at
    midnight ET so consecutive candles touch — server.js:2599-2626), which is
    exactly the opening equity for this rule, hence estimated=False. Only the
    current-equity fallback is marked estimated."""
    cur.execute('SELECT opening_equity FROM account_daily_open '
                'WHERE session_date = %s', (session_date,))
    row = cur.fetchone()
    if row and row[0] is not None:
        return float(row[0]), 'stored'

    value, source, estimated = None, None, True
    try:
        days = json.loads(Path(path or nav_ohlc_path()).read_text()).get('days') or {}
        day = days.get(session_date.isoformat())
        if isinstance(day, dict) and day.get('open') is not None:
            value, source, estimated = float(day['open']), 'ohlc', False
    except Exception as e:  # noqa: BLE001 — a missing/broken store is not fatal
        logger.warning('[account_breaker] pnl_daily_ohlc unreadable (%s: %s)',
                       type(e).__name__, e)
    if value is None:
        value, source, estimated = float(equity), 'equity', True

    cur.execute(
        'INSERT INTO account_daily_open (session_date, opening_equity, estimated) '
        'VALUES (%s, %s, %s) ON CONFLICT (session_date) DO NOTHING',
        (session_date, value, estimated))
    return value, source


def _iso_variants(dt) -> set:
    """Spellings of `breached_at` the operator may paste into .env."""
    if dt is None:
        return set()
    if getattr(dt, 'tzinfo', None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return {dt.isoformat(),
            dt.replace(microsecond=0).isoformat(),
            dt.isoformat().replace('+00:00', 'Z'),
            dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')}


def rearm_requested(state: dict) -> bool:
    """True iff the operator echoed back THIS halt's breached_at. Scoping the
    token to the exact breach is what stops a stale .env line from silently
    re-arming a later, different halt."""
    token = (os.environ.get(REARM_ENV) or '').strip()
    if not token or not state.get('halted'):
        return False
    return token in _iso_variants(state.get('breached_at'))


def clear_halt(cur, alpha: float) -> None:
    """Operator re-arm: drop the latch and reset the rolling peak to the
    current alpha NAV, so the next drawdown is measured from here."""
    cur.execute(
        """
        UPDATE account_breaker_state
           SET halted = FALSE, reason = NULL, breached_at = NULL,
               peak_alpha_nav = %s, pending_flatten = FALSE,
               rearmed_at = NOW(), updated_at = NOW()
         WHERE id = 1
        """,
        (float(alpha),),
    )


def format_line(mode: str, *, equity, bench_mv, alpha, st, open_equity,
                open_src, halted, flatten=None) -> str:
    """The operator greps `[account_breaker] shadow` / `[account_breaker] armed`.
    Emitted on EVERY tick — a missing line means the process died, which is why
    rule=none exists. Do not reorder or rename tokens."""
    daily = 'n/a' if st.get('daily') is None else f"{st['daily']:.4f}"
    line = (f"[account_breaker] {mode} equity={float(equity):.2f} "
            f"bench_mv={float(bench_mv):.2f} alpha_nav={float(alpha):.2f} "
            f"peak={float(st['peak']):.2f} dd={float(st['dd']):.4f} "
            f"open_equity={float(open_equity):.2f} open_src={open_src} "
            f"daily={daily} rule={st['rule']} breach={int(bool(st['breach']))} "
            f"halted={int(bool(halted))}")
    if flatten is not None:
        line += (f" flatten_ok={int(flatten['ok'])} "
                 f"flatten_fail={int(flatten['fail'])} "
                 f"pending={int(bool(flatten['pending']))}")
    return line
```

- [ ] **Step 4** — Run both C1 test files; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_rules.py tests/execution/test_account_breaker_state.py -q
```

- [ ] **Step 5** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/execution/account_breaker.py tests/execution/test_account_breaker_state.py
git commit -F - <<'MSG'
feat(risk): account-breaker state, opening-equity snapshot, re-arm token, shadow line (C1)

Singleton latch in account_breaker_state; opening equity resolved
stored -> logs/pnl_daily_ohlc.json candle open -> current equity (only the last
is estimated=true). Re-arm is scoped to the exact breached_at timestamp so a
stale .env token cannot clear a later halt, and clearing resets the rolling
peak to the current alpha NAV.

format_line() is the operator's grep contract and is byte-asserted in tests;
it is emitted on EVERY tick, including rule=none, so a missing line means the
process died rather than "nothing happened".

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 5 — C1c: the flatten action (RTH-only, benchmark-exempt, retrying)

**Files:**
- `src/execution/account_breaker.py` (extend)
- `tests/execution/test_account_breaker_flatten.py` (new)

**Interfaces:**

Consumes (reused, never duplicated):
- `regime_liquidator._close_symbol(symbol: str, qty: float, market_open: bool | None = None) -> tuple[bool, dict]` — `src/execution/regime_liquidator.py:281-352`. Market close via `alpaca position close`, cancel-then-close on a 40310000 "insufficient qty", partial-flatten payload `{'partial_flatten': True, 'closed_qty', 'hostage_qty'}`.
- `regime_liquidator._market_is_open() -> bool` — `:115-123`, defaults **False** on any failure.
- `regime_liquidator._load_broker_positions() -> dict` — `:212-233`.
- `circuit_breaker_fires(ts_utc, ticker, unrealized_pnl_pct_nav, threshold_pct, position_qty, close_result_json)`, all NOT NULL — `src/database/migrations/069_regime_blended_sizer.sql:62-71`.
- `strategy_registry.parameters ->> 'benchmark_sleeve' = 'true'` + `execution_signals(strategy_id, ticker, target_date)`.

Why `circuit_breaker_fires` and not a new table: `open_reconcile._derive_close_reason` / `_closed_today_tickers` (`src/execution/open_reconcile.py:1004-1043`) already scope the broker-close ledger reconcile to that table, and `regime_blended_sizer._load_recent_risk_exits` (`:2247-2280`) already builds the risk-exit cooldown from it while skipping `close_result_json->>'dry_run' = 'true'`. Writing there gives the account breaker signal_pnl closure and re-entry cooldown for free, and keeps shadow rows inert.

Produces:
- `bench_tickers(conn) -> set[str] | None` — **None means the lookup failed**; the caller must then NOT flatten (fail-closed, so a DB hiccup can never flatten the sleeve the breaker is required to leave alone).
- `rule_threshold(rule: str) -> float`, `rule_magnitude(rule: str, st: dict) -> float`
- `flatten_alpha(positions: dict, bench_tickers: set[str], *, cur, live: bool, rule: str, magnitude: float, journal: bool = True) -> dict` → `{'ok': int, 'fail': int, 'pending': bool, 'tickers': list[str]}`. `journal=False` computes the would-flatten counts and writes nothing — that is what `main()` uses in SHADOW, where the spec allows a log line **only**.

- [ ] **Step 1** — Write the failing test file `tests/execution/test_account_breaker_flatten.py`:

```python
"""C1 flatten action: benchmark positions are never closed, the lookup fails
CLOSED, failed submits leave pending_flatten set for the next tick, and every
attempt is journalled to circuit_breaker_fires (shadow rows carry dry_run=true
so the sizer's risk-exit cooldown ignores them).

_close_symbol is always patched — no test may reach the alpaca CLI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab            # noqa: E402
from execution import regime_liquidator as rl          # noqa: E402


class FakeCursor:
    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        out, self._rows = list(self._rows), []
        return out

    def fires(self):
        return [p for s, p in self.calls if 'INSERT INTO circuit_breaker_fires' in s]


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        conn_cur = self._cur

        class _Ctx:
            def __enter__(self_inner):
                return conn_cur

            def __exit__(self_inner, *_a):
                return False

        return _Ctx()


POSITIONS = {
    'SPY':  {'qty': 200.0,  'side': 'long',  'market_value': '41000'},
    'AAPL': {'qty': 100.0,  'side': 'long',  'market_value': '22000'},
    'AMD':  {'qty': -50.0,  'side': 'short', 'market_value': '-7000'},
    'FLAT': {'qty': 0.0,    'side': 'long',  'market_value': '0'},
}
ST = {'peak': 171_200.0, 'dd': -0.1291, 'daily': -0.0727,
      'rule': 'drawdown', 'breach': True}


# ── benchmark ticker lookup ─────────────────────────────────────────────────

def test_bench_tickers_reads_registry_then_recent_signals():
    cur = FakeCursor([[('S_beta_spy',)], [('SPY',)]])
    conn = FakeConn(cur)
    assert ab.bench_tickers(conn) == {'SPY'}
    assert any('strategy_registry' in s for s, _ in cur.calls)
    assert any('execution_signals' in s for s, _ in cur.calls)


def test_bench_tickers_no_sleeve_is_an_empty_set_not_none():
    cur = FakeCursor([[]])
    assert ab.bench_tickers(FakeConn(cur)) == set()


def test_bench_tickers_fails_closed_to_none_on_error():
    class Boom:
        def cursor(self):
            raise RuntimeError('db down')

    assert ab.bench_tickers(Boom()) is None


# ── flatten ─────────────────────────────────────────────────────────────────

def test_flatten_skips_benchmark_and_zero_qty_positions(monkeypatch):
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert closed == ['AAPL', 'AMD']          # sorted, SPY and FLAT excluded
    assert out == {'ok': 2, 'fail': 0, 'pending': False, 'tickers': ['AAPL', 'AMD']}


def test_flatten_skips_option_and_crypto_symbols(monkeypatch):
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    positions = {
        'AAPL260918C00250000': {'qty': 2.0, 'market_value': '400'},
        'BTC/USD': {'qty': 0.5, 'market_value': '30000'},
        'MSFT': {'qty': 10.0, 'market_value': '4000'},
    }
    ab.flatten_alpha(positions, set(), cur=FakeCursor(), live=True,
                     rule='drawdown', magnitude=-0.13)
    assert closed == ['MSFT']


def test_failed_submit_counts_and_sets_pending(monkeypatch):
    def _close(sym, qty, market_open=None):
        if sym == 'AMD':
            return False, {'error': 'insufficient qty'}
        return True, {}

    monkeypatch.setattr(rl, '_close_symbol', _close)
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=FakeCursor(), live=True,
                           rule='drawdown', magnitude=ST['dd'])
    assert out['ok'] == 1 and out['fail'] == 1 and out['pending'] is True


def test_close_symbol_raising_is_a_failure_not_a_crash(monkeypatch):
    def _close(sym, qty, market_open=None):
        raise RuntimeError('cli exploded')

    monkeypatch.setattr(rl, '_close_symbol', _close)
    out = ab.flatten_alpha({'AAPL': {'qty': 1.0, 'market_value': '100'}}, set(),
                           cur=FakeCursor(), live=True, rule='daily_loss',
                           magnitude=-0.05)
    assert out['fail'] == 1 and out['pending'] is True


def test_shadow_mode_submits_nothing_and_journals_dry_run(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError('shadow mode must not submit an order')

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=False,
                           rule='drawdown', magnitude=ST['dd'])
    assert out == {'ok': 2, 'fail': 0, 'pending': False, 'tickers': ['AAPL', 'AMD']}
    payloads = [json.loads(p[5]) for p in cur.fires()]
    assert payloads and all(p['dry_run'] is True for p in payloads)
    assert all(p['account_breaker'] is True and p['rule'] == 'drawdown'
               for p in payloads)


def test_live_fire_rows_carry_the_rule_threshold_and_signed_qty(monkeypatch):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (True, {'status': 'filled'}))
    cur = FakeCursor()
    ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=True,
                     rule='drawdown', magnitude=ST['dd'])
    by_ticker = {p[1]: p for p in cur.fires()}
    assert set(by_ticker) == {'AAPL', 'AMD'}
    assert by_ticker['AMD'][4] == -50.0                  # signed position_qty
    assert by_ticker['AAPL'][3] == pytest.approx(0.10)   # threshold_pct = |DD_LIMIT|
    assert by_ticker['AAPL'][2] == pytest.approx(-0.1291)
    assert json.loads(by_ticker['AAPL'][5])['dry_run'] is False


def test_journal_false_writes_nothing(monkeypatch):
    """main() uses journal=False in SHADOW: the spec allows a log line only, and
    a sustained breach would otherwise write dry-run rows every 5 minutes."""
    def _boom(*_a, **_k):
        raise AssertionError('shadow mode must not submit an order')

    monkeypatch.setattr(rl, '_close_symbol', _boom)
    cur = FakeCursor()
    out = ab.flatten_alpha(POSITIONS, {'SPY'}, cur=cur, live=False,
                           rule='drawdown', magnitude=ST['dd'], journal=False)
    assert out['ok'] == 2 and cur.fires() == []


def test_rule_threshold_and_magnitude_select_the_breaching_rule():
    assert ab.rule_threshold('drawdown') == pytest.approx(0.10)
    assert ab.rule_threshold('daily_loss') == pytest.approx(0.03)
    assert ab.rule_threshold('drawdown+daily_loss') == pytest.approx(0.10)
    assert ab.rule_magnitude('daily_loss', ST) == pytest.approx(-0.0727)
    assert ab.rule_magnitude('drawdown', ST) == pytest.approx(-0.1291)
```

- [ ] **Step 2** — Run; expect `AttributeError: … has no attribute 'bench_tickers'`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_flatten.py -q
```

- [ ] **Step 3** — Append the action layer to `src/execution/account_breaker.py`. Add `import re` to the import block, then:

```python
# Mirrors regime_blended_sizer._OCC_RE (:923) — option legs and crypto pairs are
# out of scope for the equity flatten; the option book has its own lifecycle.
_OCC_RE = re.compile(r'^[A-Z.]{1,6}\d{6}[CP]\d{8}$')


def _is_equity_symbol(sym) -> bool:
    s = str(sym or '').strip().upper()
    return bool(s) and '/' not in s and not _OCC_RE.match(s)


def bench_tickers(conn):
    """Tickers of the benchmark (beta) sleeve, or None when the lookup FAILED.

    None is load-bearing: the caller must refuse to flatten on None rather than
    treat "unknown" as "no benchmark", which would close the very sleeve C1 is
    required to leave untouched. The registry query is issued here (rather than
    via benchmark_sleeve.load_benchmark_sleeve_ids, which fails OPEN to an empty
    set) precisely so a DB error is distinguishable from an empty sleeve."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM strategy_registry "
                        "WHERE (parameters ->> 'benchmark_sleeve') = 'true'")
            ids = sorted({r[0] for r in (cur.fetchall() or []) if r and r[0]})
            if not ids:
                return set()
            cur.execute(
                """
                SELECT DISTINCT ticker FROM execution_signals
                 WHERE strategy_id = ANY(%s)
                   AND target_date >= (CURRENT_DATE - %s::int)
                """,
                (ids, BENCH_LOOKBACK_DAYS))
            return {r[0] for r in (cur.fetchall() or []) if r and r[0]}
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning('[account_breaker] benchmark ticker lookup failed '
                       '(%s: %s); refusing to flatten this tick',
                       type(e).__name__, e)
        return None


def rule_threshold(rule: str) -> float:
    """The magnitude of the limit that tripped, for circuit_breaker_fires.
    Drawdown wins when both rules fire."""
    return abs(DD_LIMIT) if str(rule).startswith('drawdown') else abs(DAILY_LIMIT)


def rule_magnitude(rule: str, st: dict) -> float:
    """The measured breach fraction that goes into
    circuit_breaker_fires.unrealized_pnl_pct_nav. The account breaker fires on
    ACCOUNT state, not on the position's own P&L, so the account-level ratio is
    the honest value to journal (the column is only read by
    _load_recent_risk_exits, which uses ticker + position_qty)."""
    if str(rule).startswith('drawdown'):
        return float(st.get('dd') or 0.0)
    return float(st.get('daily') or 0.0)


def _record_fire(cur, ticker, qty, magnitude, threshold, payload) -> None:
    cur.execute(
        """
        INSERT INTO circuit_breaker_fires
          (ts_utc, ticker, unrealized_pnl_pct_nav, threshold_pct, position_qty,
           close_result_json)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (datetime.now(timezone.utc), ticker, float(magnitude), float(threshold),
         float(qty), json.dumps(payload)),
    )


def flatten_alpha(positions: dict, bench_tkrs, *, cur, live: bool, rule: str,
                  magnitude: float, journal: bool = True) -> dict:
    """Close every NON-benchmark equity position. RTH-only — the caller gates
    on regime_liquidator._market_is_open(); _close_symbol assumes RTH.

    Returns {'ok', 'fail', 'pending', 'tickers'}. `pending` True means at least
    one submit failed, so the caller leaves account_breaker_state.pending_flatten
    set and the next 5-minute tick retries. In SHADOW (live=False) nothing is
    submitted and `ok` counts what WOULD have been closed; journalled rows then
    carry dry_run=true. `journal=False` writes nothing at all — main() uses that
    in shadow, where the spec allows a log line only and a sustained breach
    would otherwise append rows every 5 minutes."""
    from execution.regime_liquidator import _close_symbol

    bench_tkrs = set(bench_tkrs or ())
    threshold = rule_threshold(rule)
    ok = fail = 0
    touched: list = []

    for sym in sorted(positions or {}):
        if sym in bench_tkrs or not _is_equity_symbol(sym):
            continue
        try:
            qty = float((positions[sym] or {}).get('qty') or 0.0)
        except (TypeError, ValueError):
            continue
        if qty == 0.0:
            continue
        touched.append(sym)

        if live:
            try:
                closed, payload = _close_symbol(sym, qty, market_open=True)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not abort the flatten
                closed, payload = False, {'error': f'{type(e).__name__}: {e}'}
            payload = dict(payload) if isinstance(payload, dict) else {'result': payload}
            payload.update({'account_breaker': True, 'rule': rule, 'dry_run': False})
        else:
            closed = True
            payload = {'account_breaker': True, 'rule': rule, 'dry_run': True,
                       'would_close_qty': qty}

        if journal and cur is not None:
            _record_fire(cur, sym, qty, magnitude, threshold, payload)
        if closed:
            ok += 1
        else:
            fail += 1
            logger.warning('[account_breaker] close FAILED %s qty=%s: %s',
                           sym, qty, payload)

    return {'ok': ok, 'fail': fail, 'pending': fail > 0, 'tickers': touched}
```

- [ ] **Step 4** — Run the three C1 files; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_rules.py tests/execution/test_account_breaker_state.py tests/execution/test_account_breaker_flatten.py -q
```

- [ ] **Step 5** — Run the reused module's existing tests to prove nothing regressed:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_regime_liquidator.py tests/execution/test_close_subset.py tests/execution/test_close_symbol_cancel_then_close.py -q
```

- [ ] **Step 6** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/execution/account_breaker.py tests/execution/test_account_breaker_flatten.py
git commit -F - <<'MSG'
feat(risk): account-breaker flatten action, benchmark-exempt and retrying (C1)

Reuses regime_liquidator._close_symbol (RTH market close, cancel-then-close,
partial-flatten aware) rather than duplicating a close path, and journals every
attempt to circuit_breaker_fires so open_reconcile's broker-close reconcile and
the sizer's risk-exit cooldown pick the flatten up with no new plumbing.

The benchmark-ticker lookup fails CLOSED (None => do not flatten this tick):
treating "unknown" as "no sleeve" would close the one book C1 must leave alone.
Shadow rows carry dry_run=true so they create no re-entry cooldown.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 6 — C1d: the sizer refuses alpha opens/adds while halted

**Files:**
- `src/execution/regime_blended_sizer.py` (extend)
- `tests/execution/test_account_breaker_sizer_gate.py` (new)

**Interfaces:**

Consumes:
- `account_breaker_state.halted` (migration 157)
- the existing gate chain in `_emit_orders_from_targets` — `src/execution/regime_blended_sizer.py:2466-2472`
- the existing only-shed idiom `_shed` inside `_apply_entry_hygiene_gate` — `:2398-2405`

Produces (in `src/execution/regime_blended_sizer.py`):
- `_clamp_to_held(out: dict, tkr: str, broker: dict) -> str` — the shared only-shed primitive; returns `'blocked' | 'unflipped' | 'capped' | 'none'`. **Task 9 reuses it for the event gate — do not inline it twice.**
- `_load_account_breaker_halted() -> bool` — fail-**open** `False` (a DB hiccup must not freeze the fleet; the flatten itself is the hard stop)
- `_apply_account_breaker_gate(target_usd: dict, broker: dict, *, halted=None, bench_tkrs=None) -> dict`

Placement: between `_apply_entry_hygiene_gate` and `_apply_net_exposure_cap`. The net cap must stay LAST (its own comment at `:2470-2471` says so).

- [ ] **Step 1** — Write the failing test file `tests/execution/test_account_breaker_sizer_gate.py`:

```python
"""C1: while the account breaker is halted the sizer must refuse alpha OPENS
and ADDS. Exits, reductions and orphan closes are untouched, and benchmark
tickers are exempt (S_beta_spy positions and entries are never affected).

All inputs injected — no DB.
"""
from __future__ import annotations

import importlib

import pytest

rbs = importlib.import_module('execution.regime_blended_sizer')


def _gate(target, broker, *, halted=True, bench=None):
    return rbs._apply_account_breaker_gate(dict(target), broker, halted=halted,
                                           bench_tkrs=set(bench or ()))


# ── the shared only-shed primitive ──────────────────────────────────────────

def test_clamp_drops_an_unheld_open():
    out = {'AAPL': 5000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {}) == 'blocked'
    assert 'AAPL' not in out


def test_clamp_converts_a_flip_to_close_only():
    out = {'AAPL': 5000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {'AAPL': -3000.0}) == 'unflipped'
    assert out['AAPL'] == 0.0


def test_clamp_caps_an_add_at_the_held_size():
    out = {'AAPL': 9000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {'AAPL': 4000.0}) == 'capped'
    assert out['AAPL'] == 4000.0


def test_clamp_caps_a_short_add_at_the_held_size():
    out = {'AMD': -9000.0}
    assert rbs._clamp_to_held(out, 'AMD', {'AMD': -4000.0}) == 'capped'
    assert out['AMD'] == -4000.0


def test_clamp_leaves_a_reduction_alone():
    out = {'AAPL': 1000.0}
    assert rbs._clamp_to_held(out, 'AAPL', {'AAPL': 4000.0}) == 'none'
    assert out['AAPL'] == 1000.0


# ── the gate ────────────────────────────────────────────────────────────────

def test_not_halted_is_byte_identical():
    target = {'AAPL': 9000.0, 'ZZTA': 1000.0}
    assert _gate(target, {}, halted=False) == target


def test_halted_blocks_a_new_alpha_open():
    out = _gate({'AAPL': 5000.0}, {})
    assert 'AAPL' not in out


def test_halted_caps_an_alpha_add_at_the_held_size():
    out = _gate({'AAPL': 9000.0}, {'AAPL': 4000.0})
    assert out['AAPL'] == 4000.0


def test_halted_never_blocks_a_reduction():
    out = _gate({'AAPL': 1000.0}, {'AAPL': 4000.0})
    assert out['AAPL'] == 1000.0


def test_halted_leaves_the_benchmark_sleeve_alone():
    out = _gate({'SPY': 90_000.0, 'AAPL': 9000.0}, {'SPY': 40_000.0}, bench=['SPY'])
    assert out['SPY'] == 90_000.0
    assert 'AAPL' not in out


def test_halted_ignores_option_and_crypto_symbols():
    target = {'AAPL260918C00250000': 400.0, 'BTC/USD': 30_000.0}
    assert _gate(target, {}) == target


def test_empty_targets_short_circuit():
    assert _gate({}, {'AAPL': 4000.0}) == {}


def test_halted_lookup_fails_open(monkeypatch):
    """A DB hiccup must not freeze the fleet — the flatten is the hard stop."""
    def _boom(*_a, **_k):
        raise RuntimeError('db down')

    monkeypatch.setattr(rbs.psycopg2, 'connect', _boom)
    assert rbs._load_account_breaker_halted() is False


def test_gate_is_wired_into_the_emission_tail(monkeypatch):
    """It must run AFTER entry hygiene and BEFORE the net-exposure cap."""
    order = []
    monkeypatch.setattr(rbs, '_apply_asset_eligibility_gate',
                        lambda t, b, **k: (order.append('asset'), t)[1])
    monkeypatch.setattr(rbs, '_apply_entry_hygiene_gate',
                        lambda t, b, **k: (order.append('hygiene'), t)[1])
    monkeypatch.setattr(rbs, '_apply_account_breaker_gate',
                        lambda t, b, **k: (order.append('breaker'), t)[1])
    monkeypatch.setattr(rbs, '_apply_net_exposure_cap',
                        lambda t: (order.append('netcap'), t)[1])
    monkeypatch.setattr(rbs, '_classify_position_deltas', lambda t, b, m: [])
    rbs._emit_orders_from_targets({}, {}, 100_000.0, None, None, {}, {}, [], {},
                                  1.0, {'equity': 100_000.0}, broker={})
    assert order == ['asset', 'hygiene', 'breaker', 'netcap']
```

- [ ] **Step 2** — Run; expect `AttributeError: … has no attribute '_clamp_to_held'`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_sizer_gate.py -q
```

- [ ] **Step 3** — Add the primitive and the gate to `src/execution/regime_blended_sizer.py`, immediately BEFORE `def _emit_orders_from_targets(` (currently line 2448):

```python
# ── Only-shed clamp, shared by the C1 breaker gate and the C3 event gate ────
# Same semantics as _apply_entry_hygiene_gate's inner _shed (:2398-2405), lifted
# to module scope so the two new gates cannot drift from it: not held -> drop the
# target (an open is refused); opposite sign -> zero it (the close leg survives,
# the re-open leg dies); same-sign larger -> cap at the held size (no growth);
# same-sign smaller -> untouched (a reduction is an exit and is never blocked).
def _clamp_to_held(out: dict, tkr: str, broker: dict) -> str:
    """Mutates `out[tkr]` in place. Returns 'blocked' | 'unflipped' | 'capped'
    | 'none' so callers can report exactly what they did."""
    target = out.get(tkr)
    if target is None:
        return 'none'
    current = (broker or {}).get(tkr, 0.0)
    if current == 0.0:
        del out[tkr]
        return 'blocked'
    if (target > 0 > current) or (target < 0 < current):
        out[tkr] = 0.0
        return 'unflipped'
    if abs(target) > abs(current):
        out[tkr] = (1.0 if current > 0 else -1.0) * abs(current)
        return 'capped'
    return 'none'


def _load_account_breaker_halted() -> bool:
    """account_breaker_state.halted (spec 2026-09-12 C1).

    FAIL-OPEN (False) on any error, matching _load_recent_risk_exits: the hard
    stop is the breaker's own flatten, which runs in its own 5-minute process.
    A Postgres hiccup must not silently freeze the whole fleet's entries."""
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
            cur.execute('SELECT halted FROM account_breaker_state WHERE id = 1')
            row = cur.fetchone()
            return bool(row and row[0])
    except Exception as e:  # noqa: BLE001
        logger.warning('account_breaker: halted lookup failed (%s: %s); '
                       'treating as NOT halted', type(e).__name__, e)
        return False


def _apply_account_breaker_gate(target_usd, broker, *, halted=None,
                                bench_tkrs=None):
    """While the account breaker is halted, alpha OPENS and ADDS are refused.

    Exits, reductions and orphan closes are structurally unblockable (orphan
    closes never enter target_usd). Benchmark-sleeve tickers are exempt: ruling
    R2 halts the ALPHA book, and S_beta_spy positions and entries stay
    untouched. Option legs and crypto pairs are out of scope. `halted` and
    `bench_tkrs` are injectable for tests."""
    if not target_usd:
        return target_usd
    if halted is None:
        halted = _load_account_breaker_halted()
    if not halted:
        return target_usd

    bench_tkrs = set(bench_tkrs or ())
    out = dict(target_usd)
    blocked, unflipped, capped = [], [], []
    for tkr in [t for t in target_usd
                if t not in bench_tkrs and not _is_occ_symbol(t) and '/' not in t]:
        action = _clamp_to_held(out, tkr, broker)
        if action == 'blocked':
            blocked.append(tkr)
        elif action == 'unflipped':
            unflipped.append(tkr)
        elif action == 'capped':
            capped.append(tkr)
    if blocked or unflipped or capped:
        logger.warning(
            'account_breaker: HALTED — alpha opens blocked=%s, flips converted '
            'to close-only=%s, adds capped at held size=%s (benchmark exempt=%s)',
            sorted(blocked), sorted(unflipped), sorted(capped), sorted(bench_tkrs))
    return out
```

- [ ] **Step 4** — Wire it into `_emit_orders_from_targets`. Replace lines 2468-2471 (`target_usd = _apply_asset_eligibility_gate(...)` through the net-cap comment) with:

```python
    target_usd = _apply_asset_eligibility_gate(target_usd, broker)
    target_usd = _apply_entry_hygiene_gate(target_usd, broker)
    # C1 (spec 2026-09-12): while the account breaker is halted, alpha opens and
    # adds are refused. Benchmark tickers are exempt (ruling R2 halts the ALPHA
    # book). Inert when not halted — byte-identical routing.
    target_usd = _apply_account_breaker_gate(target_usd, broker,
                                             bench_tkrs=bench_tkrs)
    # Net cap runs LAST: the per-name gates above can re-skew net (dropping an
    # unshortable short leg raises net-long) — the emitted book must respect it.
    target_usd = _apply_net_exposure_cap(target_usd)
```

`bench_tkrs` is already a parameter of `_emit_orders_from_targets` (`:2451`) and is passed from the sizing path (`:2024-2026`); the zero-conviction flatten path passes it as `None`, which correctly means "no exemption" on a path that only closes.

- [ ] **Step 5** — Run the new tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_sizer_gate.py -q
```

- [ ] **Step 6** — Run the touching module's existing sizer tests; expect PASS (the gate is inert without a halt, and `tests/execution/conftest.py` keeps them DB-free):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_regime_blended_sizer.py tests/execution/test_entry_hygiene_gate.py tests/execution/test_sizer_benchmark_acting_gate.py tests/execution/test_sizer_flatten_zero_conviction.py tests/execution/test_sizer_per_ticker_cap.py -q
```

- [ ] **Step 7** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/execution/regime_blended_sizer.py tests/execution/test_account_breaker_sizer_gate.py
git commit -F - <<'MSG'
feat(sizer): refuse alpha opens/adds while the account breaker is halted (C1)

Adds _clamp_to_held (module-scope lift of the entry-hygiene _shed idiom, shared
with the C3 event gate) and _apply_account_breaker_gate, wired between entry
hygiene and the net-exposure cap so the cap still runs last. Exits, reductions
and orphan closes are structurally unblockable; benchmark-sleeve tickers are
exempt because ruling R2 halts the ALPHA book.

The halted lookup fails OPEN — the flatten in the 5-minute breaker process is
the hard stop, and a Postgres hiccup must not freeze the fleet's entries.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 7 — C1e: `main()`, the 5-minute cron wiring, and the `#trade-reports` post

**Files:**
- `src/execution/account_breaker.py` (extend — `run_once()` / `main()`)
- `src/engine/cron-schedule.js` (extend — a second `spawn` inside the EXISTING `*/5 9-16 * * 1-5` cron)
- `tests/execution/test_account_breaker_cron_wiring.py` (new — `run_once()` behaviour + a source assertion on the cron block)

**Interfaces:**

Consumes:
- `alpaca_trader._alpaca_session()` and `_fetch_account_state(sess) -> dict` (`src/execution/alpaca_trader.py:15,43`; returns zeros on failure, hence the `equity <= 0` guard)
- `regime_liquidator._market_is_open()`, `_load_broker_positions()`, `_post_to_discord(channel, msg)`
- everything produced by Tasks 3-5
- the existing cron block `cron.schedule('*/5 9-16 * * 1-5', …)` at `src/engine/cron-schedule.js:810-829`

Produces:
- `run_once(session_date=None) -> int` (0 ok, 1 soft failure — no evaluation this tick, 2 misconfiguration)
- `main(argv=None) -> int`
- log file `logs/account_breaker_<YYYY-MM-DD>.log`

Behaviour matrix:

| state | flag | action |
|---|---|---|
| market closed | any | log + exit 0, no DB |
| bench lookup returns `None` | any | log ERROR + exit 1, **no flatten, no state write** |
| no breach | any | update `peak/dd/daily`, `halted=false`, emit line with `rule=none` |
| breach, flag unset | shadow | emit line with `breach=1 halted=0` + the would-flatten tail; `halted` STAYS FALSE and nothing is journalled — otherwise the sizer gate would start blocking entries with the flag off |
| breach, flag `=1` | armed | flatten, `halted=true`, `breached_at=now`, journal `circuit_breaker_fires`, post `#trade-reports` |
| already halted, `pending_flatten` | armed | retry the flatten only; never re-evaluate, never move the peak |
| already halted + valid re-arm token | any | `clear_halt`, peak := current alpha NAV, then evaluate normally |

- [ ] **Step 1** — Write the failing test file `tests/execution/test_account_breaker_cron_wiring.py`:

```python
"""C1 entry point + cron wiring.

run_once() is driven entirely through patched surfaces — no CLI, no Postgres,
no Discord. The cron assertion is a source read, so it needs no node runtime.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab            # noqa: E402
from execution import alpaca_trader as at              # noqa: E402
from execution import regime_liquidator as rl          # noqa: E402

CRON = ROOT / 'src' / 'engine' / 'cron-schedule.js'
SESSION = date(2026, 9, 16)

POSITIONS = {
    'SPY':  {'qty': 200.0, 'side': 'long', 'market_value': '41000'},
    'AAPL': {'qty': 100.0, 'side': 'long', 'market_value': '22000'},
}


class FakeCursor:
    def __init__(self, state_row=None, open_row=None):
        self._queue = [state_row, open_row]
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self._queue.pop(0) if self._queue else None

    def fetchall(self):
        return []

    def sql_matching(self, needle):
        return [c for c in self.calls if needle in c[0]]

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = 0
        self.closed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Everything run_once() touches, patched. Returns the FakeCursor."""
    monkeypatch.setenv('POSTGRES_URI', 'postgres://stub')
    monkeypatch.setenv(ab.NAV_OHLC_PATH_ENV, str(tmp_path / 'missing.json'))
    monkeypatch.delenv(ab.ARM_ENV, raising=False)
    monkeypatch.delenv(ab.REARM_ENV, raising=False)
    monkeypatch.setattr(rl, '_market_is_open', lambda: True)
    monkeypatch.setattr(rl, '_load_broker_positions', lambda: dict(POSITIONS))
    monkeypatch.setattr(at, '_alpaca_session', lambda: object())
    monkeypatch.setattr(ab, 'bench_tickers', lambda conn: {'SPY'})
    posts = []
    monkeypatch.setattr(rl, '_post_to_discord',
                        lambda ch, msg: posts.append((ch, msg)) or True)

    holder = {'posts': posts}

    def _install(cur, equity):
        conn = FakeConn(cur)
        monkeypatch.setattr(ab.psycopg2, 'connect', lambda *_a, **_k: conn)
        monkeypatch.setattr(at, '_fetch_account_state', lambda sess: {'equity': equity})
        holder['conn'] = conn
        return conn

    holder['install'] = _install
    return holder


def _state(halted=False, peak=None, breached_at=None, reason=None, pending=False):
    return (halted, reason, breached_at, peak, None, None, pending)


# ── guards ──────────────────────────────────────────────────────────────────

def test_market_closed_skips_without_touching_the_db(monkeypatch, wired):
    monkeypatch.setattr(rl, '_market_is_open', lambda: False)
    monkeypatch.setattr(ab.psycopg2, 'connect',
                        lambda *_a, **_k: pytest.fail('must not connect'))
    assert ab.run_once(session_date=SESSION) == 0


def test_bench_lookup_failure_blocks_the_whole_tick(monkeypatch, wired, caplog):
    cur = FakeCursor(_state(peak=200_000.0), None)
    wired['install'](cur, 100_000.0)
    monkeypatch.setattr(ab, 'bench_tickers', lambda conn: None)
    assert ab.run_once(session_date=SESSION) == 1
    assert cur.sql_matching('UPDATE account_breaker_state') == []
    assert cur.sql_matching('INSERT INTO circuit_breaker_fires') == []


def test_zero_equity_is_a_soft_failure(monkeypatch, wired):
    cur = FakeCursor(_state(), None)
    wired['install'](cur, 0.0)
    assert ab.run_once(session_date=SESSION) == 1


# ── no breach ───────────────────────────────────────────────────────────────

def test_clean_tick_updates_the_peak_and_logs_rule_none(wired, caplog):
    cur = FakeCursor(_state(peak=160_000.0), (205_000.0,))
    wired['install'](cur, 200_000.0)     # alpha = 200000 - 41000 = 159000
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert ' shadow ' in line and 'rule=none' in line and 'breach=0' in line
    assert 'halted=0' in line and 'open_src=stored' in line
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[0] is False and params[3] == 160_000.0


# ── breach, shadow ──────────────────────────────────────────────────────────

def test_shadow_breach_does_not_halt_and_submits_nothing(monkeypatch, wired, caplog):
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda *_a, **_k: pytest.fail('shadow must not submit'))
    cur = FakeCursor(_state(peak=200_000.0), (205_000.0,))
    wired['install'](cur, 100_000.0)     # alpha = 59000, dd = -0.705
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert ' shadow ' in line and 'breach=1' in line and 'halted=0' in line
    assert 'flatten_ok=1' in line          # AAPL would close; SPY is exempt
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[0] is False              # halted stays FALSE with the flag off
    assert cur.sql_matching('INSERT INTO circuit_breaker_fires') == []
    assert wired['posts'] == []            # spec: shadow logs a line ONLY


# ── breach, armed ───────────────────────────────────────────────────────────

def test_armed_breach_flattens_halts_and_posts(monkeypatch, wired, caplog):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    cur = FakeCursor(_state(peak=200_000.0), (205_000.0,))
    wired['install'](cur, 100_000.0)
    with caplog.at_level('INFO', logger=ab.logger.name):
        assert ab.run_once(session_date=SESSION) == 0
    assert closed == ['AAPL']                        # SPY untouched
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[account_breaker] ')][-1]
    assert ' armed ' in line and 'halted=1' in line and 'pending=0' in line
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[0] is True and params[1] == 'drawdown'
    assert isinstance(params[2], datetime)
    assert len(cur.sql_matching('INSERT INTO circuit_breaker_fires')) == 1
    channel, msg = wired['posts'][0]
    assert channel == 'trade-reports'
    assert 'OPENCLAW_ACCOUNT_BREAKER_REARM=' in msg


def test_already_halted_retries_the_pending_flatten_only(monkeypatch, wired):
    monkeypatch.setenv(ab.ARM_ENV, '1')
    closed = []
    monkeypatch.setattr(rl, '_close_symbol',
                        lambda sym, qty, market_open=None: (closed.append(sym), (True, {}))[1])
    breached = datetime(2026, 9, 16, 17, 42, tzinfo=timezone.utc)
    cur = FakeCursor(_state(halted=True, peak=200_000.0, breached_at=breached,
                            reason='drawdown', pending=True), (205_000.0,))
    wired['install'](cur, 100_000.0)
    assert ab.run_once(session_date=SESSION) == 0
    assert closed == ['AAPL']
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[0] is True and params[3] == 200_000.0     # peak NOT moved
    assert params[6] is False                                # pending cleared


def test_operator_token_rearms_then_evaluates_normally(monkeypatch, wired):
    breached = datetime(2026, 9, 16, 17, 42, tzinfo=timezone.utc)
    monkeypatch.setenv(ab.REARM_ENV, breached.isoformat())
    cur = FakeCursor(_state(halted=True, peak=200_000.0, breached_at=breached,
                            reason='drawdown'), (205_000.0,))
    wired['install'](cur, 200_000.0)
    assert ab.run_once(session_date=SESSION) == 0
    rearm, = [c for c in cur.sql_matching('UPDATE account_breaker_state')
              if 'rearmed_at = NOW()' in c[0]]
    assert rearm[1][0] == 159_000.0        # peak reset to the current alpha NAV


# ── cron wiring (source assertion; no node runtime needed) ──────────────────

def test_account_breaker_spawns_inside_the_existing_five_minute_cron():
    js = CRON.read_text()
    marker = "cron.schedule('*/5 9-16 * * 1-5'"
    assert js.count(marker) == 1, 'the 5-min RTH cron must stay a single block'
    start = js.index(marker)
    end = js.index("}, { timezone: 'America/New_York' });", start)
    block = js[start:end]
    assert "'src/execution/position_circuit_breaker.py'" in block
    assert "'src/execution/account_breaker.py'" in block
    assert 'account_breaker_' in block            # its own dated log file


def test_no_new_cron_expression_was_introduced():
    js = CRON.read_text()
    schedules = re.findall(r"cron\.schedule\('([^']+)'", js)
    assert schedules.count('*/5 9-16 * * 1-5') == 1
```

- [ ] **Step 2** — Run; expect `AttributeError: … has no attribute 'run_once'` plus two cron assertion failures:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_cron_wiring.py -q
```

- [ ] **Step 3** — Append `run_once()` / `main()` to `src/execution/account_breaker.py`. Add `import psycopg2` and `from zoneinfo import ZoneInfo` to the imports, plus `_ET = ZoneInfo('America/New_York')` beside the other constants, then:

```python
def _post(channel: str, msg: str) -> None:
    """Best-effort Discord post — a webhook failure must never abort a tick."""
    try:
        from execution.regime_liquidator import _post_to_discord
        _post_to_discord(channel, msg)
    except Exception as e:  # noqa: BLE001
        logger.warning('[account_breaker] Discord post failed: %s', e)


def run_once(session_date=None) -> int:
    """One 5-minute evaluation. 0 = evaluated, 1 = soft failure (no evaluation
    this tick, retried in 5 minutes), 2 = misconfiguration."""
    from execution.alpaca_trader import _alpaca_session, _fetch_account_state
    from execution.regime_liquidator import _load_broker_positions, _market_is_open

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        logger.error('[account_breaker] POSTGRES_URI not set; aborting')
        return 2
    if not _market_is_open():
        logger.info('[account_breaker] market closed; skipping')
        return 0

    try:
        equity = float(_fetch_account_state(_alpaca_session())['equity'])
    except Exception as e:  # noqa: BLE001
        logger.error('[account_breaker] account fetch failed (%s: %s); aborting',
                     type(e).__name__, e)
        return 1
    if equity <= 0:
        # _fetch_account_state returns zeros on failure — never evaluate on that.
        logger.error('[account_breaker] equity=%s unusable; aborting', equity)
        return 1

    positions = _load_broker_positions()
    live = armed()
    mode = 'armed' if live else 'shadow'
    session = session_date or datetime.now(_ET).date()

    conn = psycopg2.connect(uri)
    try:
        bench = bench_tickers(conn)
        if bench is None:
            logger.error('[account_breaker] benchmark ticker lookup failed; '
                         'NO evaluation and NO flatten this tick')
            return 1
        alpha, bench_mv = alpha_nav(equity, positions, bench)

        cur = conn.cursor()
        state = load_state(cur)

        if rearm_requested(state):
            clear_halt(cur, alpha)
            conn.commit()
            logger.info('[account_breaker] re-armed by operator token; '
                        'peak reset to %.2f', alpha)
            state = {'halted': False, 'reason': None, 'breached_at': None,
                     'peak': alpha, 'dd': None, 'daily': None,
                     'pending_flatten': False}

        open_eq, open_src = opening_equity(cur, session, equity)

        if state['halted']:
            # Latched. Never re-evaluate and never move the peak — only retry a
            # flatten that failed to submit on an earlier tick.
            st = {'peak': alpha if state['peak'] is None else state['peak'],
                  'dd': 0.0 if state['dd'] is None else state['dd'],
                  'daily': state['daily'], 'rule': state['reason'] or 'none',
                  'breach': True}
            flat = None
            if state['pending_flatten'] and live:
                flat = flatten_alpha(positions, bench, cur=cur, live=True,
                                     rule=st['rule'],
                                     magnitude=rule_magnitude(st['rule'], st))
                save_state(cur, halted=True, reason=state['reason'],
                           breached_at=state['breached_at'], peak=st['peak'],
                           dd=st['dd'], daily=st['daily'],
                           pending_flatten=flat['pending'])
            conn.commit()
            logger.info(format_line(mode, equity=equity, bench_mv=bench_mv,
                                    alpha=alpha, st=st, open_equity=open_eq,
                                    open_src=open_src, halted=True, flatten=flat))
            return 0

        st = evaluate(alpha, state['peak'], equity, open_eq)
        flat = None
        breached_at = None

        if st['breach']:
            # journal=live: SHADOW gets a log line ONLY (spec §3 C1), so a
            # sustained breach never appends a dry-run row every 5 minutes.
            flat = flatten_alpha(positions, bench, cur=cur, live=live,
                                 rule=st['rule'],
                                 magnitude=rule_magnitude(st['rule'], st),
                                 journal=live)
            if live:
                breached_at = datetime.now(timezone.utc)

        if st['breach'] and live:
            save_state(cur, halted=True, reason=st['rule'],
                       breached_at=breached_at, peak=st['peak'], dd=st['dd'],
                       daily=st['daily'], pending_flatten=flat['pending'])
        else:
            # Shadow NEVER latches: _apply_account_breaker_gate reads `halted`,
            # so setting it with the flag off would change routing.
            save_state(cur, halted=False, reason=None, breached_at=None,
                       peak=st['peak'], dd=st['dd'], daily=st['daily'],
                       pending_flatten=False)
        conn.commit()

        logger.info(format_line(mode, equity=equity, bench_mv=bench_mv,
                                alpha=alpha, st=st, open_equity=open_eq,
                                open_src=open_src,
                                halted=bool(live and st['breach']), flatten=flat))

        if live and st['breach']:
            daily_txt = 'n/a' if st['daily'] is None else f"{st['daily'] * 100:.2f}%"
            attempted = flat['ok'] + flat['fail']
            _post('trade-reports',
                  ':rotating_light: **Account breaker HALTED** '
                  f"rule={st['rule']}\n"
                  f"• alpha NAV ${alpha:,.0f} vs peak ${st['peak']:,.0f} "
                  f"(dd {st['dd'] * 100:.2f}%, limit {DD_LIMIT * 100:.0f}%)\n"
                  f"• equity ${equity:,.0f} vs session open ${open_eq:,.0f} "
                  f"(daily {daily_txt}, limit {DAILY_LIMIT * 100:.0f}%)\n"
                  f"• flattened {flat['ok']}/{attempted} alpha positions; "
                  f"benchmark sleeve untouched ({sorted(bench)})\n"
                  f"• re-arm (operator only): set "
                  f"OPENCLAW_ACCOUNT_BREAKER_REARM={breached_at.isoformat()} in .env")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    return run_once()


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4** — Wire the cron. In `src/engine/cron-schedule.js`, inside the EXISTING `cron.schedule('*/5 9-16 * * 1-5', …)` callback (currently `:810-829`), immediately after `child.unref();` and before the `} catch (e) {`, add:

```js
            // C1 (spec 2026-09-12): the account-level breaker rides the SAME
            // 5-minute RTH cron — no new schedule, no new thread. A separate
            // process so a crash in one breaker cannot take the other down.
            const abLogPath = path.join(logDir, `account_breaker_${today}.log`);
            const abLogFd = fs.openSync(abLogPath, 'a');
            const abChild = spawn(PYTHON, ['src/execution/account_breaker.py'], {
                cwd: ROOT,
                env: { ...process.env },
                detached: true,
                stdio: ['ignore', abLogFd, abLogFd],
            });
            abChild.unref();
```

- [ ] **Step 5** — Run the new tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_cron_wiring.py -q
```

- [ ] **Step 6** — Run all four C1 test files together; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_account_breaker_rules.py tests/execution/test_account_breaker_state.py tests/execution/test_account_breaker_flatten.py tests/execution/test_account_breaker_sizer_gate.py tests/execution/test_account_breaker_cron_wiring.py -q
```

- [ ] **Step 7** — Syntax-check the JS (no service restart):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && node --check src/engine/cron-schedule.js
```

- [ ] **Step 8** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/execution/account_breaker.py src/engine/cron-schedule.js tests/execution/test_account_breaker_cron_wiring.py
git commit -F - <<'MSG'
feat(risk): account-breaker entry point on the existing 5-min RTH cron (C1)

run_once() evaluates once per tick and emits the [account_breaker] line every
time, including rule=none, so a missing line means the process died. SHADOW
never latches halted and never journals — the sizer gate reads `halted`, so
latching with the flag off would change routing.

Wired as a SECOND spawn inside the existing `*/5 9-16 * * 1-5` cron block: no
new schedule, no new thread, separate process and log file so one breaker
crashing cannot take the other down.

OPERATOR: arming is a .env edit + johnbot restart, only after two clean shadow
days (see the plan's shadow-line contract).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

- [ ] **Step 9** — OPERATOR-RUN (not part of the automated flow; needs the DB and must not run while the fleet backtest holds the box). Apply migration 157 and verify the singleton row:

```bash
psql "$POSTGRES_URI" -f /root/openclaw/src/database/migrations/157_account_breaker.sql
psql "$POSTGRES_URI" -c "SELECT * FROM account_breaker_state;"
```
(Or simply restart user-scope johnbot — `src/channels/discord/bot.js:1662` runs `postgres.js migrate()`, which applies every `migrations/*.sql` in sorted order on boot.)

---

### Task 8 — C3a: the macro-event reader + the Fed/BLS/BEA ingester

**Files:**
- `src/lib/macro_events.py` (new — the reader three later tasks depend on)
- `src/ingestion/ingest_macro_events.py` (new)
- `tests/lib/test_macro_events.py` (new)
- `tests/ingestion/test_ingest_macro_events.py` (new)
- `tests/fixtures/macro_events/{fed_fomccalendars,bls_cpi_sched,bls_annual_sched,bea_schedule}.html` (new)

**READ THIS FIRST — the HTML parsers are UNVALIDATED.** No live page was fetched while writing this plan (three `curl -I` probes only: Fed `fomccalendars.htm` **200 text/html**, BEA `/news/schedule` **200 text/html**, BLS `cpi.htm` **403** — Akamai blocks a bare HEAD, so BLS requires browser headers and probably a GET). Every fixture below is hand-authored to the structural motifs of those pages, not captured from them. Consequences, all handled by design:

- every parser works on **tag-stripped text**, not a DOM walk, so markup churn does not break it;
- `--from-file SOURCE=PATH` is a **required** interface so the operator can feed a saved page when a URL 404s or blocks;
- **Step 12 is an OPERATOR-RUN validation gate** that must pass before the timer is enabled;
- the live C3 gate and the freshness check need only FORWARD coverage, so a partial 2017→2027 backfill blocks only Task 11's backtest mirror, not the live half of C3.

**Interfaces:**

Consumes:
- `lib.trading_calendar.is_session(d)`, `next_session(d)`, `prev_session(d)`, `MASTER_PATH_ENV = 'OPENCLAW_TRADING_CALENDAR_PATH'`, `clear_cache()` — `src/lib/trading_calendar.py:34,92,139-171`
- `src.data.parquet_store.append_dedup(path, new_df, key_cols, mode) -> int` and `row_count(path)` — `src/data/parquet_store.py:83-120`
- conventions from `src/ingestion/ingest_nasdaq_earnings_calendar.py:182-230` (`_headers(ua)`, `_http_get`, `merge_into_master`)

Produces:

`src/lib/macro_events.py`
- `MASTER_PATH_ENV = 'OPENCLAW_MACRO_EVENTS_PATH'`, `DEFAULT_MASTER`, `master_path() -> Path`
- `EVENTS = ('FOMC_DECISION','CPI','NFP','PCE','GDP_ADV','FOMC_MINUTES')`
- `HIGH_IMPORTANCE = ('FOMC_DECISION','CPI','NFP')`
- `COLUMNS = ['event','scheduled_at','session_date','source','ingested_at']`
- `load_events(events=HIGH_IMPORTANCE) -> list[dict]` — `[{'event','session_date'}]`, `[]` on a missing/unreadable master
- `gated_sessions(start, end, events=HIGH_IMPORTANCE) -> dict[date, list[str]]`
- `gating_event(session, events=HIGH_IMPORTANCE) -> str | None` — `'CPI@2026-09-16,FOMC_DECISION@2026-09-17'` or `None`

`src/ingestion/ingest_macro_events.py`
- `MASTER_PATH`, `KEY_COLS = ['event','scheduled_at']`, `SOURCE_URLS` (one constant block)
- `session_date_for(scheduled_at_utc) -> date`
- `parse_fed(html, *, fetched_at) -> pd.DataFrame`
- `parse_titled(html, titles, source, *, fetched_at, default_event=None) -> pd.DataFrame`
- `merge_into_master(df, *, master_path=MASTER_PATH) -> dict`
- `run(sources, *, from_file=None, backfill=False, start_year=2017, end_year=2027, master_path=MASTER_PATH) -> tuple[int, dict]`
- `main(argv=None) -> int`

- [ ] **Step 1** — Write the failing reader test `tests/lib/test_macro_events.py`:

```python
"""C3 reader: T-1..T gated sessions over a synthetic macro_events master.

T-1 is the PREVIOUS NYSE SESSION, not the calendar day before — a Monday
release gates the preceding Friday.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from lib import macro_events as me
from lib import trading_calendar as tc


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    """Sessions Mon-Fri 2026-09-01..2026-10-31, minus Labor Day 2026-09-07."""
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-09-01', '2026-10-31')
            if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    yield
    tc.clear_cache()


def _master(tmp_path, monkeypatch, rows):
    df = pd.DataFrame(rows, columns=['event', 'scheduled_at', 'session_date',
                                     'source', 'ingested_at'])
    p = tmp_path / 'macro_events.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    return p


TS = pd.Timestamp('2026-09-01T12:00:00Z')


def _row(event, session, hour_utc=12):
    d = session
    return {'event': event,
            'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, hour_utc),
                                         tz='UTC'),
            'session_date': d, 'source': 'test', 'ingested_at': TS}


def test_missing_master_is_inert(tmp_path, monkeypatch):
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(tmp_path / 'nope.parquet'))
    assert me.load_events() == []
    assert me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30)) == {}
    assert me.gating_event(dt.date(2026, 9, 16)) is None


def test_load_events_filters_to_high_importance(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [
        _row('CPI', dt.date(2026, 9, 16)),
        _row('PCE', dt.date(2026, 9, 25)),
        _row('FOMC_MINUTES', dt.date(2026, 10, 7)),
    ])
    assert [r['event'] for r in me.load_events()] == ['CPI']
    assert len(me.load_events(events=me.EVENTS)) == 3


def test_gated_sessions_covers_t_minus_one_and_t(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16))])
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got == {dt.date(2026, 9, 15): ['CPI'], dt.date(2026, 9, 16): ['CPI']}


def test_t_minus_one_skips_a_holiday_and_the_weekend(tmp_path, monkeypatch, calendar):
    # 2026-09-08 is the Tuesday after Labor Day; its previous session is Fri 09-04.
    _master(tmp_path, monkeypatch, [_row('NFP', dt.date(2026, 9, 8))])
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert set(got) == {dt.date(2026, 9, 4), dt.date(2026, 9, 8)}


def test_two_events_on_one_session_are_merged_and_sorted(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16)),
                                    _row('FOMC_DECISION', dt.date(2026, 9, 17))])
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got[dt.date(2026, 9, 16)] == ['CPI', 'FOMC_DECISION']


def test_window_bounds_are_inclusive(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16))])
    assert me.gated_sessions(dt.date(2026, 9, 16), dt.date(2026, 9, 16)) == {
        dt.date(2026, 9, 16): ['CPI']}
    assert me.gated_sessions(dt.date(2026, 10, 1), dt.date(2026, 10, 31)) == {}


def test_gating_event_renders_the_shadow_line_token(tmp_path, monkeypatch, calendar):
    _master(tmp_path, monkeypatch, [_row('CPI', dt.date(2026, 9, 16)),
                                    _row('FOMC_DECISION', dt.date(2026, 9, 17))])
    assert me.gating_event(dt.date(2026, 9, 16)) == \
        'CPI@2026-09-16,FOMC_DECISION@2026-09-17'
    assert me.gating_event(dt.date(2026, 9, 14)) is None


def test_unreadable_master_is_inert_not_fatal(tmp_path, monkeypatch):
    p = tmp_path / 'macro_events.parquet'
    p.write_bytes(b'not a parquet')
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    assert me.load_events() == []
```

- [ ] **Step 2** — Run; expect `ModuleNotFoundError: No module named 'lib.macro_events'`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/lib/test_macro_events.py -q
```

- [ ] **Step 3** — Write `src/lib/macro_events.py`:

```python
"""Macro-event calendar reader (spec 2026-09-12 §3 C3, operator ruling R3).

Master: data/master/macro_events.parquet, built by
src/ingestion/ingest_macro_events.py. Append-only, dedup key
(event, scheduled_at). Columns:

    event         FOMC_DECISION | CPI | NFP | PCE | GDP_ADV | FOMC_MINUTES
    scheduled_at  UTC timestamp of the release
    session_date  the NYSE session the release lands in (lib.trading_calendar)
    source        'federalreserve.gov' | 'bls.gov' | 'bea.gov'
    ingested_at   UTC timestamp of the fetch

Every reader here is INERT (empty result + a warning) when the master is
missing or unreadable: a gate that cannot read its calendar must not block
trading. The file is ~1k rows, so a column-projected read is well inside the
8 GB box budget.

T-1 means the PREVIOUS NYSE SESSION, not the calendar day before — a Monday
release gates the preceding Friday, and a post-holiday release gates the
session before the holiday.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
MASTER_PATH_ENV = 'OPENCLAW_MACRO_EVENTS_PATH'
DEFAULT_MASTER = ROOT / 'data' / 'master' / 'macro_events.parquet'

EVENTS = ('FOMC_DECISION', 'CPI', 'NFP', 'PCE', 'GDP_ADV', 'FOMC_MINUTES')
# Ruling R3 scopes the entry block to these three.
HIGH_IMPORTANCE = ('FOMC_DECISION', 'CPI', 'NFP')
COLUMNS = ['event', 'scheduled_at', 'session_date', 'source', 'ingested_at']


def master_path() -> Path:
    return Path(os.environ.get(MASTER_PATH_ENV) or DEFAULT_MASTER)


def _as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def load_events(events=HIGH_IMPORTANCE) -> list:
    """[{'event', 'session_date'}] sorted by (session_date, event). [] when the
    master is absent or unreadable — the gate is then inert, never fatal."""
    p = master_path()
    if not p.exists():
        log.warning('[macro_events] master missing at %s; gate inert', p)
        return []
    try:
        import pandas as pd
        df = pd.read_parquet(p, columns=['event', 'session_date'])
    except Exception as e:  # noqa: BLE001
        log.warning('[macro_events] master unreadable (%s: %s); gate inert',
                    type(e).__name__, e)
        return []
    wanted = set(events or ())
    out = []
    for ev, sd in zip(df.get('event', []), df.get('session_date', [])):
        if wanted and str(ev) not in wanted:
            continue
        d = _as_date(sd)
        if d is not None:
            out.append({'event': str(ev), 'session_date': d})
    return sorted(out, key=lambda r: (r['session_date'], r['event']))


def _t_minus_one(session):
    from lib.trading_calendar import prev_session
    try:
        return prev_session(session)
    except Exception as e:  # noqa: BLE001
        log.warning('[macro_events] prev_session(%s) failed (%s); T-1 skipped',
                    session, e)
        return None


def gated_sessions(start, end, events=HIGH_IMPORTANCE) -> dict:
    """{session_date: [event, ...]} for every session in [start, end] that is
    T-1 or T of a listed release. Bounds are inclusive; either may be None."""
    out: dict = {}
    for r in load_events(events=events):
        t = r['session_date']
        for d in (t, _t_minus_one(t)):
            if d is None:
                continue
            if start is not None and d < start:
                continue
            if end is not None and d > end:
                continue
            bucket = out.setdefault(d, [])
            if r['event'] not in bucket:
                bucket.append(r['event'])
    for d in out:
        out[d].sort()
    return out


def gating_event(session, events=HIGH_IMPORTANCE):
    """'CPI@2026-09-16,FOMC_DECISION@2026-09-17' when `session` is T-1 or T of
    at least one listed release, else None. The string is the `events=` token
    of the [event_gate] line, so it is sorted and comma-joined."""
    hits = []
    for r in load_events(events=events):
        t = r['session_date']
        if t == session or _t_minus_one(t) == session:
            hits.append(f"{r['event']}@{t.isoformat()}")
    return ','.join(sorted(set(hits))) if hits else None
```

- [ ] **Step 4** — Run; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/lib/test_macro_events.py -q
```

- [ ] **Step 5** — Write the four fixtures under `tests/fixtures/macro_events/`.

`fed_fomccalendars.html`:
```html
<html><body>
<div class="panel panel-default">
  <div class="panel-heading"><h4>2026 FOMC Meetings</h4></div>
  <div class="panel-body">
    <div class="fomc-meeting"><div class="fomc-meeting__month">January</div>
      <div class="fomc-meeting__date">27-28</div>
      <div class="fomc-meeting__minutes">Minutes: (released February 18, 2026)</div></div>
    <div class="fomc-meeting"><div class="fomc-meeting__month">April</div>
      <div class="fomc-meeting__date">28-29</div>
      <div class="fomc-meeting__minutes">Minutes: (released May 20, 2026)</div></div>
    <div class="fomc-meeting"><div class="fomc-meeting__month">September</div>
      <div class="fomc-meeting__date">16-17</div></div>
    <div class="fomc-meeting"><div class="fomc-meeting__month">December</div>
      <div class="fomc-meeting__date">15-16</div></div>
  </div>
</div>
<div class="panel panel-default">
  <div class="panel-heading"><h4>2027 FOMC Meetings</h4></div>
  <div class="panel-body">
    <div class="fomc-meeting"><div class="fomc-meeting__month">January</div>
      <div class="fomc-meeting__date">26-27</div></div>
  </div>
</div>
</body></html>
```

`bls_cpi_sched.html`:
```html
<html><body>
<h1>Consumer Price Index</h1>
<table>
<tr><th>Reference Month</th><th>Release Date</th><th>Release Time</th></tr>
<tr><td>August 2026</td><td>September 11, 2026</td><td>08:30 AM</td></tr>
<tr><td>September 2026</td><td>October 13, 2026</td><td>08:30 AM</td></tr>
</table>
</body></html>
```

`bls_annual_sched.html`:
```html
<html><body>
<table>
<tr><th>Release</th><th>Date</th><th>Time</th></tr>
<tr><td>Employment Situation</td><td>September 4, 2026</td><td>08:30 AM</td></tr>
<tr><td>Consumer Price Index</td><td>September 11, 2026</td><td>08:30 AM</td></tr>
<tr><td>Producer Price Index</td><td>September 15, 2026</td><td>08:30 AM</td></tr>
</table>
</body></html>
```

`bea_schedule.html`:
```html
<html><body><ul>
<li><span class="title">Gross Domestic Product, 2nd Quarter 2026 (Advance Estimate)</span>
    <span class="date">July 30, 2026 8:30 a.m. EDT</span></li>
<li><span class="title">Personal Income and Outlays, July 2026</span>
    <span class="date">August 28, 2026 8:30 a.m. EDT</span></li>
<li><span class="title">Gross Domestic Product, 2nd Quarter 2026 (Second Estimate)</span>
    <span class="date">August 27, 2026 8:30 a.m. EDT</span></li>
</ul></body></html>
```

- [ ] **Step 6** — Write the failing ingester test `tests/ingestion/test_ingest_macro_events.py`:

```python
"""C3 ingester: Fed / BLS / BEA keyless parsers over the checked-in fixtures,
session_date derivation, and the append_dedup merge.

NO NETWORK. Every test parses a fixture from disk; the HTTP layer is only
exercised through injected `from_file` paths.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from lib import trading_calendar as tc
from src.ingestion import ingest_macro_events as mod

FIX = Path(__file__).resolve().parents[1] / 'fixtures' / 'macro_events'
TS = pd.Timestamp('2026-09-13T12:00:00Z')


@pytest.fixture(autouse=True)
def calendar(tmp_path, monkeypatch):
    """Sessions Mon-Fri 2026-01-01..2027-12-31, minus 2026-09-07 (Labor Day)."""
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-01-01', '2027-12-31')
            if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    yield
    tc.clear_cache()


def _by_event(df):
    return {e: sorted(g['session_date']) for e, g in df.groupby('event')}


# ── Fed ─────────────────────────────────────────────────────────────────────

def test_fed_parses_decision_dates_as_the_last_day_of_each_range():
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    got = _by_event(df)
    assert got['FOMC_DECISION'] == [dt.date(2026, 1, 28), dt.date(2026, 4, 29),
                                    dt.date(2026, 9, 17), dt.date(2026, 12, 16),
                                    dt.date(2027, 1, 27)]


def test_fed_minutes_release_dates_are_not_mistaken_for_meetings():
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    got = _by_event(df)
    assert got['FOMC_MINUTES'] == [dt.date(2026, 2, 18), dt.date(2026, 5, 20)]
    assert dt.date(2026, 2, 18) not in got['FOMC_DECISION']
    assert dt.date(2026, 5, 20) not in got['FOMC_DECISION']


def test_fed_decision_is_stamped_1400_et():
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    row = df[df['session_date'] == dt.date(2026, 9, 17)].iloc[0]
    assert row['scheduled_at'] == pd.Timestamp('2026-09-17T18:00:00Z')   # 14:00 EDT
    assert row['source'] == 'federalreserve.gov'


def test_fed_empty_html_is_an_empty_frame_not_a_crash():
    df = mod.parse_fed('<html><body>nothing here</body></html>', fetched_at=TS)
    assert df.empty and list(df.columns) == mod.COLUMNS


# ── BLS ─────────────────────────────────────────────────────────────────────

def test_bls_single_release_page_uses_the_default_event():
    df = mod.parse_titled((FIX / 'bls_cpi_sched.html').read_text(), (),
                          'bls.gov', fetched_at=TS, default_event='CPI')
    assert _by_event(df)['CPI'] == [dt.date(2026, 9, 11), dt.date(2026, 10, 13)]
    assert df.iloc[0]['scheduled_at'] == pd.Timestamp('2026-09-11T12:30:00Z')  # 08:30 EDT


def test_bls_annual_page_maps_titles_to_events_and_drops_the_rest():
    df = mod.parse_titled((FIX / 'bls_annual_sched.html').read_text(),
                          mod.BLS_TITLES, 'bls.gov', fetched_at=TS)
    got = _by_event(df)
    assert got == {'CPI': [dt.date(2026, 9, 11)], 'NFP': [dt.date(2026, 9, 4)]}


# ── BEA ─────────────────────────────────────────────────────────────────────

def test_bea_advance_gdp_and_pce_only():
    df = mod.parse_titled((FIX / 'bea_schedule.html').read_text(), mod.BEA_TITLES,
                          'bea.gov', fetched_at=TS)
    got = _by_event(df)
    assert got == {'GDP_ADV': [dt.date(2026, 7, 30)], 'PCE': [dt.date(2026, 8, 28)]}


def test_bea_second_estimate_does_not_leak_advance_from_the_previous_record():
    """The per-record context window is bounded by the PREVIOUS datetime match,
    so 'Advance Estimate' one item earlier cannot re-tag the second estimate."""
    df = mod.parse_titled((FIX / 'bea_schedule.html').read_text(), mod.BEA_TITLES,
                          'bea.gov', fetched_at=TS)
    assert dt.date(2026, 8, 27) not in list(df['session_date'])


# ── session_date ────────────────────────────────────────────────────────────

def test_session_date_is_the_same_day_for_a_pre_open_release():
    utc = pd.Timestamp('2026-09-11T12:30:00Z').to_pydatetime()
    assert mod.session_date_for(utc) == dt.date(2026, 9, 11)


def test_session_date_rolls_a_holiday_release_forward():
    utc = pd.Timestamp('2026-09-07T12:30:00Z').to_pydatetime()   # Labor Day
    assert mod.session_date_for(utc) == dt.date(2026, 9, 8)


# ── master merge ────────────────────────────────────────────────────────────

def test_merge_dedups_on_event_and_scheduled_at(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    df = mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(), fetched_at=TS)
    first = mod.merge_into_master(df, master_path=master)
    second = mod.merge_into_master(df, master_path=master)
    assert first['new_rows'] == len(df)
    assert second['new_rows'] == 0
    assert second['master_rows_after'] == first['master_rows_after']


def test_merge_is_additive_across_sources(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    mod.merge_into_master(mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(),
                                        fetched_at=TS), master_path=master)
    mod.merge_into_master(mod.parse_titled((FIX / 'bls_cpi_sched.html').read_text(), (),
                                           'bls.gov', fetched_at=TS,
                                           default_event='CPI'),
                          master_path=master)
    out = pd.read_parquet(master)
    assert set(out['event']) >= {'FOMC_DECISION', 'FOMC_MINUTES', 'CPI'}
    assert list(out.columns) == mod.COLUMNS


# ── the reader sees what the ingester wrote ─────────────────────────────────

def test_reader_round_trip(tmp_path, monkeypatch):
    from lib import macro_events as me
    master = tmp_path / 'macro_events.parquet'
    mod.merge_into_master(mod.parse_fed((FIX / 'fed_fomccalendars.html').read_text(),
                                        fetched_at=TS), master_path=master)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(master))
    got = me.gated_sessions(dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert got == {dt.date(2026, 9, 16): ['FOMC_DECISION'],
                   dt.date(2026, 9, 17): ['FOMC_DECISION']}


# ── run() wiring, via --from-file only (no network) ─────────────────────────

def test_run_from_file_writes_the_master_and_counts(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run(['fed'], master_path=master,
                        from_file={'fed': str(FIX / 'fed_fomccalendars.html')})
    assert rc == 0
    assert stats['urls_ok'] == 1 and stats['urls_failed'] == 0
    assert stats['new_rows'] == 7          # 5 decisions + 2 minutes
    assert master.exists()


def test_run_reports_a_failure_without_raising(tmp_path):
    master = tmp_path / 'macro_events.parquet'
    rc, stats = mod.run(['fed'], master_path=master,
                        from_file={'fed': str(tmp_path / 'missing.html')})
    assert rc == 1 and stats['urls_failed'] == 1 and stats['new_rows'] == 0
```

- [ ] **Step 7** — Run; expect `ModuleNotFoundError: No module named 'src.ingestion.ingest_macro_events'`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/ingestion/test_ingest_macro_events.py -q
```

- [ ] **Step 8** — Write `src/ingestion/ingest_macro_events.py`:

```python
#!/usr/bin/env python3
"""Keyless macro-event calendar ingest -> data/master/macro_events.parquet.

Spec: docs/specs/2026-09-12-quantdinger-adoptions-spec.md §3 C3 (ruling R3).
Append-only, dedup key (event, scheduled_at); the reader is src/lib/macro_events.py.

SOURCES (all keyless, all HTML):
  federalreserve.gov  FOMC_DECISION (statement day = the LAST day of each
                      meeting range, 14:00 ET) + FOMC_MINUTES (14:00 ET)
  bls.gov             CPI, NFP (Employment Situation) — 08:30 ET
  bea.gov             GDP_ADV (advance estimate only), PCE — 08:30 ET

PARSING STRATEGY: every parser runs over TAG-STRIPPED TEXT, never a DOM walk,
so markup churn on a government site does not silently zero the master. Probed
2026-09-13: the Fed page and the BEA schedule answer 200 text/html to a bare
HEAD; BLS answers 403 to a bare HEAD (Akamai) and needs browser headers, which
_headers() supplies.

THESE PARSERS WERE NOT VALIDATED AGAINST A LIVE FETCH. `--from-file
SOURCE=PATH` exists so the operator can feed a saved page when a URL blocks or
changes, and the plan's Step 12 is the operator validation gate that must pass
before the timer is enabled.

Usage:
  python3 src/ingestion/ingest_macro_events.py                       # forward refresh
  python3 src/ingestion/ingest_macro_events.py --sources fed
  python3 src/ingestion/ingest_macro_events.py --from-file fed=/tmp/fomc.html
  python3 src/ingestion/ingest_macro_events.py --backfill --start-year 2017 --end-year 2027
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

MASTER_PATH = ROOT / 'data' / 'master' / 'macro_events.parquet'
KEY_COLS = ['event', 'scheduled_at']
COLUMNS = ['event', 'scheduled_at', 'session_date', 'source', 'ingested_at']
SLEEP_BETWEEN_URLS_S = 1.0
_ET = ZoneInfo('America/New_York')

# ── the one URL block (edit here, nowhere else) ─────────────────────────────
FED_CALENDAR_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
FED_HISTORICAL_URL = 'https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm'
BLS_CPI_URL = 'https://www.bls.gov/schedule/news_release/cpi.htm'
BLS_EMPSIT_URL = 'https://www.bls.gov/schedule/news_release/empsit.htm'
BLS_ANNUAL_URL = 'https://www.bls.gov/schedule/news_release/{year}_sched.htm'
BEA_SCHEDULE_URL = 'https://www.bea.gov/news/schedule'

USER_AGENTS = [
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
]

BLS_TITLES = ((('consumer price index',), 'CPI'),
              (('employment situation',), 'NFP'))
BEA_TITLES = ((('gross domestic product', 'advance'), 'GDP_ADV'),
              (('personal income and outlays',), 'PCE'))

_MONTHS = {m.lower(): i for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'], start=1)}
_MONTH_RE = ('(?:January|February|March|April|May|June|July|August|September|'
             'October|November|December)')
_DATETIME_RE = re.compile(
    rf'({_MONTH_RE})\s+(\d{{1,2}}),\s*(20\d{{2}})\s+(\d{{1,2}}):(\d{{2}})\s*'
    r'([AaPp])\.?\s*[Mm]\.?')
_FED_YEAR_RE = re.compile(r'(20\d{2})\s+FOMC\s+Meeting')
_FED_MINUTES_RE = re.compile(
    rf'Minutes\s*:?\s*\(?\s*released\s+({_MONTH_RE})\s+(\d{{1,2}}),\s*(20\d{{2}})\s*\)?',
    re.I)
_FED_MEETING_RE = re.compile(
    rf'({_MONTH_RE})\s+(\d{{1,2}})(?:\s*[-–]\s*(?:({_MONTH_RE})\s+)?(\d{{1,2}}))?')


# ── text + time helpers ─────────────────────────────────────────────────────

def _text(html: str) -> str:
    """Tag-stripped, whitespace-collapsed page text."""
    txt = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html or '')
    txt = re.sub(r'(?s)<[^>]+>', ' ', txt)
    for ent, rep in (('&nbsp;', ' '), ('&amp;', '&'), ('&#8211;', '-'),
                     ('&ndash;', '-'), ('&mdash;', '-'), ('&#8212;', '-')):
        txt = txt.replace(ent, rep)
    return re.sub(r'\s+', ' ', txt).strip()


def session_date_for(scheduled_at_utc) -> dt.date:
    """The NYSE session a release lands in: its ET calendar day when that day
    is a session, otherwise the next session (a holiday release is felt at the
    next open)."""
    from lib.trading_calendar import is_session, next_session
    et = pd.Timestamp(scheduled_at_utc).tz_convert(_ET) \
        if pd.Timestamp(scheduled_at_utc).tzinfo else pd.Timestamp(scheduled_at_utc, tz='UTC').tz_convert(_ET)
    d = et.date()
    return d if is_session(d) else next_session(d)


def _row(event: str, naive_et: dt.datetime, source: str, fetched_at) -> dict:
    utc = naive_et.replace(tzinfo=_ET).astimezone(dt.timezone.utc)
    return {'event': event, 'scheduled_at': pd.Timestamp(utc),
            'session_date': session_date_for(utc), 'source': source,
            'ingested_at': pd.Timestamp(fetched_at)}


def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=KEY_COLS, keep='last')
    df['event'] = df['event'].astype('string')
    df['source'] = df['source'].astype('string')
    df['scheduled_at'] = pd.to_datetime(df['scheduled_at'], utc=True)
    df['ingested_at'] = pd.to_datetime(df['ingested_at'], utc=True)
    return df.reset_index(drop=True)


def _iter_datetimes(text: str):
    """Yield (context, naive_et_datetime) for every 'Month D, YYYY H:MM AM/PM'.

    `context` is the text between the PREVIOUS match and this one, which is the
    natural record boundary on a schedule page — a fixed-width lookback would
    let the previous row's title re-tag this row (the BEA advance/second
    estimate trap)."""
    prev_end = 0
    for m in _DATETIME_RE.finditer(text):
        ctx = text[prev_end:m.start()]
        prev_end = m.end()
        hour, minute = int(m.group(4)), int(m.group(5))
        ap = m.group(6).lower()
        if ap == 'p' and hour != 12:
            hour += 12
        if ap == 'a' and hour == 12:
            hour = 0
        try:
            yield ctx, dt.datetime(int(m.group(3)), _MONTHS[m.group(1).lower()],
                                   int(m.group(2)), hour, minute)
        except (KeyError, ValueError):
            continue


# ── parsers ─────────────────────────────────────────────────────────────────

def parse_fed(html: str, *, fetched_at) -> pd.DataFrame:
    """FOMC decision days (last day of each meeting range, 14:00 ET) + minutes
    release dates (14:00 ET), anchored on the '<YYYY> FOMC Meetings' headings."""
    text = _text(html)
    anchors = [(int(m.group(1)), m.start()) for m in _FED_YEAR_RE.finditer(text)]
    rows = []
    for i, (year, pos) in enumerate(anchors):
        end = anchors[i + 1][1] if i + 1 < len(anchors) else len(text)
        span = text[pos:end]
        # Minutes first, then REMOVE them: '(released May 20, 2026)' would
        # otherwise parse as a May 20 meeting.
        for m in _FED_MINUTES_RE.finditer(span):
            try:
                rows.append(_row('FOMC_MINUTES',
                                 dt.datetime(int(m.group(3)),
                                             _MONTHS[m.group(1).lower()],
                                             int(m.group(2)), 14, 0),
                                 'federalreserve.gov', fetched_at))
            except (KeyError, ValueError):
                continue
        span = _FED_MINUTES_RE.sub(' ', span)
        for m in _FED_MEETING_RE.finditer(span):
            month_name = m.group(3) or m.group(1)
            day = int(m.group(4) or m.group(2))
            try:
                rows.append(_row('FOMC_DECISION',
                                 dt.datetime(year, _MONTHS[month_name.lower()],
                                             day, 14, 0),
                                 'federalreserve.gov', fetched_at))
            except (KeyError, ValueError):
                continue
    return _frame(rows)


def parse_titled(html: str, titles, source: str, *, fetched_at,
                 default_event=None) -> pd.DataFrame:
    """Schedule pages whose rows are '<title> <Month D, YYYY> <H:MM AM>'.

    `titles` is ((required_substrings, event), ...) matched case-insensitively
    against the record's own context. `default_event` covers single-release
    pages (bls.gov/schedule/news_release/cpi.htm) whose rows carry no title."""
    rows = []
    for ctx, naive in _iter_datetimes(_text(html)):
        low = ctx.lower()
        event = None
        for needles, name in (titles or ()):
            if all(n in low for n in needles):
                event = name
                break
        if event is None:
            event = default_event
        if event is None:
            continue
        rows.append(_row(event, naive, source, fetched_at))
    return _frame(rows)


# ── HTTP ────────────────────────────────────────────────────────────────────

def _headers(ua: str) -> dict:
    return {'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9'}


def _http_get(url: str, headers: dict, timeout: int = 30) -> tuple:
    """(status, text). HTTP errors come back as (code, ''); network errors raise."""
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except HTTPError as e:
        return e.code, ''


# ── plan of work ────────────────────────────────────────────────────────────

def _jobs(sources, *, backfill: bool, start_year: int, end_year: int) -> list:
    """[(source_key, url, parse_callable)]. One place decides what gets fetched."""
    out = []
    if 'fed' in sources:
        out.append(('fed', FED_CALENDAR_URL,
                    lambda h, ts: parse_fed(h, fetched_at=ts)))
        if backfill:
            for y in range(start_year, end_year + 1):
                out.append((f'fed{y}', FED_HISTORICAL_URL.format(year=y),
                            lambda h, ts: parse_fed(h, fetched_at=ts)))
    if 'bls' in sources:
        out.append(('bls_cpi', BLS_CPI_URL,
                    lambda h, ts: parse_titled(h, (), 'bls.gov', fetched_at=ts,
                                               default_event='CPI')))
        out.append(('bls_empsit', BLS_EMPSIT_URL,
                    lambda h, ts: parse_titled(h, (), 'bls.gov', fetched_at=ts,
                                               default_event='NFP')))
        if backfill:
            for y in range(start_year, end_year + 1):
                out.append((f'bls{y}', BLS_ANNUAL_URL.format(year=y),
                            lambda h, ts: parse_titled(h, BLS_TITLES, 'bls.gov',
                                                       fetched_at=ts)))
    if 'bea' in sources:
        out.append(('bea', BEA_SCHEDULE_URL,
                    lambda h, ts: parse_titled(h, BEA_TITLES, 'bea.gov',
                                               fetched_at=ts)))
    return out


def merge_into_master(df: pd.DataFrame, *, master_path: Path = MASTER_PATH) -> dict:
    from src.data.parquet_store import append_dedup, row_count

    before = row_count(master_path)
    after = append_dedup(master_path, df, KEY_COLS, mode='replace') \
        if not df.empty else before
    new_rows = int(after - before)
    return {'rows': int(len(df)), 'new_rows': new_rows,
            'replaced_rows': int(len(df) - new_rows),
            'master_rows_after': int(after)}


def run(sources, *, from_file=None, backfill: bool = False,
        start_year: int = 2017, end_year: int = 2027,
        master_path: Path = MASTER_PATH, dry_run: bool = False) -> tuple:
    """Fetch + parse + merge. A failed URL is COUNTED and skipped; rc=1 only
    when every job failed (mirrors ingest_nasdaq_earnings_calendar)."""
    from_file = dict(from_file or {})
    stats = {'urls': 0, 'urls_ok': 0, 'urls_failed': 0, 'rows': 0,
             'new_rows': 0, 'replaced_rows': 0, 'master_rows_after': None}
    jobs = _jobs(set(sources), backfill=backfill, start_year=start_year,
                 end_year=end_year)
    # An explicit --from-file key replaces every job whose key starts with it,
    # so `--from-file fed=...` also satisfies the backfill's fed<year> jobs.
    stats['urls'] = len(jobs)
    fetched_at = pd.Timestamp.now(tz='UTC')

    for i, (key, url, parse) in enumerate(jobs):
        override = next((p for k, p in from_file.items() if key.startswith(k)), None)
        html = None
        if override:
            try:
                html = Path(override).read_text()
            except Exception as e:  # noqa: BLE001
                logger.warning('%s: %s unreadable (%s)', key, override, e)
        else:
            if i:
                time.sleep(SLEEP_BETWEEN_URLS_S)
            try:
                status, body = _http_get(url, _headers(USER_AGENTS[i % len(USER_AGENTS)]))
            except Exception as e:  # noqa: BLE001
                logger.warning('%s: request raised %s: %s', key, type(e).__name__, e)
                status, body = 0, ''
            if status == 200 and body:
                html = body
            else:
                logger.warning('%s: HTTP %s (%d bytes) — source unavailable',
                               key, status, len(body or ''))
        if html is None:
            stats['urls_failed'] += 1
            continue

        try:
            df = parse(html, fetched_at)
        except Exception as e:  # noqa: BLE001
            logger.warning('%s: parse raised %s: %s', key, type(e).__name__, e)
            stats['urls_failed'] += 1
            continue
        stats['urls_ok'] += 1
        if dry_run:
            print(f'[macro-events] DRY-RUN {key}: {len(df)} rows', flush=True)
            stats['rows'] += len(df)
            continue
        m = merge_into_master(df, master_path=master_path)
        for k in ('rows', 'new_rows', 'replaced_rows'):
            stats[k] += m[k]
        stats['master_rows_after'] = m['master_rows_after']
        print(f"[macro-events] {key}: rows={m['rows']} new_rows={m['new_rows']} "
              f"master_rows_after={m['master_rows_after']}", flush=True)

    if stats['master_rows_after'] is None and not dry_run:
        from src.data.parquet_store import row_count
        stats['master_rows_after'] = row_count(master_path)
    rc = 1 if jobs and stats['urls_ok'] == 0 else 0
    return rc, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--sources', default='fed,bls,bea',
                    help='comma-separated subset of fed,bls,bea')
    ap.add_argument('--from-file', action='append', default=[],
                    metavar='KEY=PATH',
                    help='parse a saved page instead of fetching (repeatable)')
    ap.add_argument('--backfill', action='store_true',
                    help='also fetch the per-year archive pages')
    ap.add_argument('--start-year', type=int, default=2017)
    ap.add_argument('--end-year', type=int, default=2027)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    from_file = {}
    for item in args.from_file:
        k, _, v = item.partition('=')
        if k and v:
            from_file[k] = v

    rc, s = run([x.strip() for x in args.sources.split(',') if x.strip()],
                from_file=from_file, backfill=args.backfill,
                start_year=args.start_year, end_year=args.end_year,
                master_path=MASTER_PATH, dry_run=args.dry_run)
    print(f"[macro-events] urls={s['urls']} ok={s['urls_ok']} failed={s['urls_failed']} "
          f"rows={s['rows']} new_rows={s['new_rows']} "
          f"master_rows_after={s['master_rows_after']} rc={rc}", flush=True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 9** — Run both C3 reader/ingester test files; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/lib/test_macro_events.py tests/ingestion/test_ingest_macro_events.py -q
```

- [ ] **Step 10** — Prove the shared calendar helpers still pass (the ingester imports them):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/ingestion/test_calendar_sites_ingestion.py tests/ingestion/test_ingest_trading_calendar.py -q
```

- [ ] **Step 11** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/lib/macro_events.py src/ingestion/ingest_macro_events.py tests/lib/test_macro_events.py tests/ingestion/test_ingest_macro_events.py tests/fixtures/macro_events
git commit -F - <<'MSG'
feat(data): macro-event calendar master + reader (C3, R3)

src/lib/macro_events.py is the single reader the sizer gate, the backtest
mirror and the freshness check all use; T-1 resolves through
trading_calendar.prev_session, so a Monday release gates the preceding Friday
and a post-holiday release gates the session before the holiday. A missing or
unreadable master is INERT, never fatal.

src/ingestion/ingest_macro_events.py parses Fed / BLS / BEA over TAG-STRIPPED
TEXT rather than a DOM walk, dedups on (event, scheduled_at) via
parquet_store.append_dedup, and exposes --from-file so a saved page can stand
in when a URL blocks. Probed 2026-09-13: Fed 200, BEA 200, BLS 403 on a bare
HEAD (Akamai — browser headers supplied).

PARSERS ARE UNVALIDATED against live HTML; the operator validation gate must
pass before the timer is enabled.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

- [ ] **Step 12** — **OPERATOR-RUN parser validation gate** (needs network; run when the box is idle — not during the fleet backtest). Nothing downstream may be armed until this passes.

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
python3 src/ingestion/ingest_macro_events.py --sources fed,bls,bea --dry-run
```
Expected: three `DRY-RUN <key>: N rows` lines with N > 0 each. If a source returns 0 rows or a non-200:

```bash
curl -sL -A 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36' \
  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm -o /tmp/fed.html
python3 src/ingestion/ingest_macro_events.py --sources fed --dry-run --from-file fed=/tmp/fed.html
```
Then copy the saved page over the matching `tests/fixtures/macro_events/*.html`, re-run
`python3 -m pytest tests/ingestion/test_ingest_macro_events.py -q`, adjust the regexes until the
real page parses, and commit the corrected fixture + parser. Repeat per source
(`bls_cpi`, `bls_empsit`, `bea`).

- [ ] **Step 13** — **OPERATOR-RUN one-off backfill** (after Step 12 passes; ~40 requests with a 1 s pause, several minutes):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
python3 src/ingestion/ingest_macro_events.py --backfill --start-year 2017 --end-year 2027
python3 -c "import pandas as pd; d=pd.read_parquet('data/master/macro_events.parquet'); print(d.groupby('event').size()); print(d['session_date'].min(), d['session_date'].max())"
```
Expected: FOMC_DECISION ≈ 8/year 2017-2027, CPI and NFP ≈ 12/year, `session_date` spanning 2017 → at least 2027-01. A partial backfill is acceptable for the LIVE gate (it needs forward coverage only) but Task 11's backtest mirror is only meaningful over the years actually covered — note the achieved span in the changelog.

---

### Task 9 — C3b: `macro_events_fresh` system check + the monthly timer units

**Files:**
- `src/system_checks/checks/macro_events_freshness.py` (new)
- `src/system_checks/checks/__init__.py` (modify — one import line)
- `src/system_checks/checks/master_freshness.py` (modify — one `_COVERED_ELSEWHERE` entry)
- `docs/systemd/openclaw-macro-events.service` (new)
- `docs/systemd/openclaw-macro-events.timer` (new)
- `tests/system_checks/test_macro_events_freshness.py` (new)

**Interfaces:**

Consumes:
- `lib.macro_events.master_path()`, `load_events(events=...)`, `HIGH_IMPORTANCE` (Task 8)
- the check contract from `src/system_checks/README.md`: `@check(name=..., tags=[...], requires=[...])` returning `(Status, str)`, detail under 200 chars, tags from `pipeline|broker|regime|strategies|agents|storage`
- `master_freshness._COVERED_ELSEWHERE` — `src/system_checks/checks/master_freshness.py:77-78`

Produces:
- check `macro_events_fresh`, tags `['storage']`, `requires=[]`
- `MIN_HORIZON_DAYS = 30`
- units `openclaw-macro-events.{service,timer}` (snapshots under `docs/systemd/`; the operator installs them)

**Why not a `_CADENCES` row.** `master_freshness` asks "was this file WRITTEN recently"; a calendar master needs the opposite question — "does it still know about the FUTURE". A monthly ingest that silently 403s leaves `ingested_at` and mtime looking healthy for weeks while the T-1..T gate quietly stops gating. So `macro_events.parquet` goes in `_COVERED_ELSEWHERE` (its own comment: "Covered by a dedicated, stricter check — do not double-report here") and forward coverage is asserted here. Without that entry, `master_freshness` would also WARN "no declared cadence" the moment the master lands.

- [ ] **Step 1** — Write the failing test `tests/system_checks/test_macro_events_freshness.py`:

```python
"""C3: macro_events_fresh asserts FORWARD coverage, which is the failure mode a
write-recency check cannot see."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from lib import macro_events as me
from src.system_checks.checks import macro_events_freshness as chk
from src.system_checks.checks import master_freshness as mf
from src.system_checks.types import Status

TS = pd.Timestamp('2026-09-13T12:00:00Z')


def _master(tmp_path, monkeypatch, rows):
    df = pd.DataFrame(rows, columns=me.COLUMNS)
    p = tmp_path / 'macro_events.parquet'
    df.to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))
    return p


def _row(event, day_offset):
    d = dt.date.today() + dt.timedelta(days=day_offset)
    return {'event': event,
            'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
            'session_date': d, 'source': 'test', 'ingested_at': TS}


def test_warn_when_master_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(tmp_path / 'nope.parquet'))
    status, detail = chk._macro_events_fresh()
    assert status is Status.WARN and 'missing' in detail


def test_fail_when_master_has_no_high_importance_rows(tmp_path, monkeypatch):
    _master(tmp_path, monkeypatch, [_row('PCE', 90)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.FAIL and 'no high-importance' in detail


def test_fail_when_forward_coverage_is_short(tmp_path, monkeypatch):
    _master(tmp_path, monkeypatch, [_row('CPI', 5), _row('NFP', 3),
                                    _row('FOMC_DECISION', -30)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.FAIL and 'forward coverage' in detail


def test_warn_when_one_event_type_has_no_future_rows(tmp_path, monkeypatch):
    _master(tmp_path, monkeypatch, [_row('CPI', 40), _row('NFP', 35)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.WARN and 'FOMC_DECISION' in detail


def test_pass_with_full_forward_coverage(tmp_path, monkeypatch):
    _master(tmp_path, monkeypatch, [_row('CPI', 40), _row('NFP', 35),
                                    _row('FOMC_DECISION', 60)])
    status, detail = chk._macro_events_fresh()
    assert status is Status.PASS and 'ahead' in detail
    assert len(detail) < 200


def test_check_is_registered_with_the_storage_tag():
    from src.system_checks.registry import all_checks
    names = {c.name: c for c in all_checks()}
    assert 'macro_events_fresh' in names
    assert 'storage' in names['macro_events_fresh'].tags


def test_master_freshness_does_not_double_report_macro_events():
    assert 'macro_events.parquet' in mf._COVERED_ELSEWHERE
    assert 'macro_events.parquet' not in mf._CADENCES
```

- [ ] **Step 2** — Run; expect `ImportError: cannot import name 'macro_events_freshness'`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/system_checks/test_macro_events_freshness.py -q
```

If `all_checks()` is not the registry's accessor name, read `src/system_checks/registry.py` and use whatever it exports (the contract in `src/system_checks/README.md` only guarantees the `@check` decorator); adjust that one test accordingly.

- [ ] **Step 3** — Write `src/system_checks/checks/macro_events_freshness.py`:

```python
"""Storage-tagged check: the macro-event master still knows about the FUTURE.

master_freshness asks "was this file written recently". For a calendar master
that is the wrong question: a monthly ingest that starts 403-ing leaves mtime
and max(ingested_at) looking healthy for weeks while the C3 T-1..T entry gate
quietly stops gating anything (gated_sessions() returns {} and the sizer sails
straight through every CPI print). So this check asserts FORWARD coverage —
a high-importance release at least MIN_HORIZON_DAYS ahead, and at least one
future row for each of FOMC_DECISION / CPI / NFP.

macro_events.parquet is therefore listed in master_freshness._COVERED_ELSEWHERE
rather than its _CADENCES table.
"""
from __future__ import annotations

import datetime as dt

from ..registry import check
from ..types import Status

MIN_HORIZON_DAYS = 30


@check(name='macro_events_fresh', tags=['storage'], requires=[])
def _macro_events_fresh():
    from lib.macro_events import HIGH_IMPORTANCE, load_events, master_path

    p = master_path()
    if not p.exists():
        return Status.WARN, f'macro_events master missing at {p} (C3 gate inert)'

    rows = load_events(events=HIGH_IMPORTANCE)
    if not rows:
        return Status.FAIL, f'macro_events at {p}: no high-importance rows'

    today = dt.date.today()
    horizon = max(r['session_date'] for r in rows)
    days = (horizon - today).days
    if days < MIN_HORIZON_DAYS:
        return Status.FAIL, (f'macro_events forward coverage {days}d '
                             f'(min {MIN_HORIZON_DAYS}d); last event {horizon}')

    counts: dict = {}
    for r in rows:
        if r['session_date'] >= today:
            counts[r['event']] = counts.get(r['event'], 0) + 1
    missing = [e for e in HIGH_IMPORTANCE if not counts.get(e)]
    if missing:
        return Status.WARN, (f'macro_events {days}d ahead but no future '
                             f'{",".join(missing)} rows (have {counts})')
    return Status.PASS, f'macro_events covers {days}d ahead ({counts})'
```

- [ ] **Step 4** — Register it. Append to `src/system_checks/checks/__init__.py`:

```python
from . import macro_events_freshness  # noqa: F401
```

And in `src/system_checks/checks/master_freshness.py` replace the `_COVERED_ELSEWHERE` line (currently `:78`) with:

```python
# Covered by a dedicated, stricter check — do not double-report here.
_COVERED_ELSEWHERE = {
    'options_aggregates_enriched.parquet',   # options_aux_freshness
    # macro_events: a calendar master's failure mode is losing FORWARD
    # coverage, not going stale backwards — macro_events_freshness asserts
    # a high-importance release >= 30 d ahead instead (spec 2026-09-12 C3).
    'macro_events.parquet',                  # macro_events_freshness
}
```

- [ ] **Step 5** — Run; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/system_checks/test_macro_events_freshness.py tests/system_checks/test_master_freshness_shares_outstanding.py -q
```

- [ ] **Step 6** — Write `docs/systemd/openclaw-macro-events.service`:

```
[Unit]
Description=Keyless macro-event calendar ingest (Fed/BLS/BEA -> data/master/macro_events.parquet)
After=network-online.target
Wants=network-online.target
OnFailure=openclaw-failure-notify@%n.service

[Service]
Type=oneshot
# User=root: matches every data/master/*.parquet writer (all root:root).
User=root
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
Environment=PYTHONPATH=/root/openclaw/src
# Forward refresh only (no --backfill): 4 GETs with browser headers and a 1 s
# pause between them. A 403/404/empty page is COUNTED (urls_failed) and
# skipped; the script exits 1 only when EVERY source failed. The 2017->2027
# backfill is a one-off operator run, never this timer.
ExecStart=/usr/bin/python3 /root/openclaw/src/ingestion/ingest_macro_events.py --sources fed,bls,bea
StandardOutput=append:/var/log/openclaw-macro-events.log
StandardError=append:/var/log/openclaw-macro-events.log
Nice=19
TimeoutStartSec=600
MemoryMax=500M

[Install]
WantedBy=multi-user.target
```

And `docs/systemd/openclaw-macro-events.timer`:

```
[Unit]
Description=Refresh the macro-event calendar master monthly (2nd, 07:00 UTC)

[Timer]
# The Fed publishes next year's schedule ~12 months ahead and BLS/BEA a year
# ahead, so monthly is ample. The 2nd (not the 1st) keeps it clear of the
# 06:00 UTC trading-calendar refresh on the 1st, which this ingest depends on
# for session_date. Persistent=true so the first enable populates immediately
# and a missed month catches up on boot.
OnCalendar=*-*-02 07:00:00 UTC
Persistent=true
Unit=openclaw-macro-events.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 7** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/system_checks/checks/macro_events_freshness.py src/system_checks/checks/__init__.py src/system_checks/checks/master_freshness.py docs/systemd/openclaw-macro-events.service docs/systemd/openclaw-macro-events.timer tests/system_checks/test_macro_events_freshness.py
git commit -F - <<'MSG'
feat(checks): macro_events_fresh forward-coverage probe + monthly timer units (C3)

A calendar master fails by losing knowledge of the FUTURE, not by going stale
backwards, so macro_events.parquet joins master_freshness._COVERED_ELSEWHERE
and gets its own check: a high-importance release >= 30 d ahead plus at least
one future FOMC_DECISION / CPI / NFP row.

Units are snapshots under docs/systemd/ only — the operator installs them, and
not before the parser validation gate passes.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

- [ ] **Step 8** — **OPERATOR-RUN install** (only after Task 8 Step 12 validation and Step 13 backfill; `Persistent=true` fires a catch-up run the moment the timer is enabled):

```bash
sudo cp /root/openclaw/docs/systemd/openclaw-macro-events.service /etc/systemd/system/
sudo cp /root/openclaw/docs/systemd/openclaw-macro-events.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-macro-events.timer
systemctl list-timers openclaw-macro-events.timer
sudo systemctl start openclaw-macro-events.service
journalctl -u openclaw-macro-events.service -n 30 --no-pager
cd /root/openclaw && python3 -m system_checks --check macro_events_fresh
```
Expected: the check reports PASS with a coverage figure well past 30 d.

---

### Task 10 — C3c: the sizer's T-1..T macro-event entry gate

**Files:**
- `src/execution/regime_blended_sizer.py` (extend)
- `tests/execution/test_macro_event_gate.py` (new)

**Interfaces:**

Consumes:
- `lib.macro_events.gating_event(session) -> str | None` (Task 8)
- `_clamp_to_held(out, tkr, broker) -> str` (Task 6)
- `bench_tkrs` — already a parameter of `_emit_orders_from_targets` (`:2451`), passed from the sizing path (`:2024-2026`)

Produces:
- `EVENT_GATE_ENV = 'OPENCLAW_EVENT_GATE'`, `EVENT_GATE_EXEMPT_BENCH_ENV = 'OPENCLAW_EVENT_GATE_EXEMPT_BENCH'`
- `_event_gate_enabled() -> bool`, `_event_gate_exempt_bench() -> bool`
- `_apply_macro_event_gate(target_usd, broker, *, session=None, events=None, bench_tkrs=None) -> dict`

Ruling R3 as implemented:
- **all regimes** — the function never reads the regime;
- **blocks new entries only** — `_clamp_to_held`, so exits, reductions and orphan closes are structurally unblockable;
- **T-1 through T** — `gating_event` resolves T-1 via `trading_calendar.prev_session`;
- **benchmark included by default** — `bench_tkrs` is honoured ONLY when `OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1`;
- **shadow by default** — with `OPENCLAW_EVENT_GATE` unset the ORIGINAL dict is returned unchanged and only the line is emitted.

**Placement — resolved ambiguity.** The spec says "in the sizer, before rule C / acting gate". Implemented instead in the emission tail, next to `_apply_entry_hygiene_gate`, because "drop every NEW open/add" is undecidable before the broker book is in hand (rule C runs on weights, long before `broker` is loaded), and because the shave-don't-redistribute semantics match the caps. Consequence, stated for the operator: blocked conviction is **shaved, not redistributed to SPY** — the same philosophy as the per-ticker and cluster caps.

- [ ] **Step 1** — Write the failing test `tests/execution/test_macro_event_gate.py`:

```python
"""C3 (ruling R3): T-1..T macro-event entry block.

Blocks NEW opens/adds only; exits, reductions and orphan closes are untouched;
all regimes; benchmark included unless OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1.
Every input injected — no calendar read, no DB.
"""
from __future__ import annotations

import datetime as dt
import importlib
import logging

import pytest

rbs = importlib.import_module('execution.regime_blended_sizer')

SESSION = dt.date(2026, 9, 16)
EVENTS = 'CPI@2026-09-16'


@pytest.fixture(autouse=True)
def _shadow_by_default(monkeypatch):
    monkeypatch.delenv('OPENCLAW_EVENT_GATE', raising=False)
    monkeypatch.delenv('OPENCLAW_EVENT_GATE_EXEMPT_BENCH', raising=False)


def _gate(target, broker, *, events=EVENTS, bench=None):
    return rbs._apply_macro_event_gate(dict(target), broker, session=SESSION,
                                       events=events, bench_tkrs=set(bench or ()))


# ── shadow ──────────────────────────────────────────────────────────────────

def test_shadow_returns_the_targets_untouched():
    target = {'AAPL': 9000.0, 'ZZTA': 1000.0}
    assert _gate(target, {'AAPL': 4000.0}) == target


def test_shadow_line_reports_what_would_have_been_blocked(caplog):
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        _gate({'AAPL': 9000.0, 'ZZTA': 1000.0}, {'AAPL': 4000.0})
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[event_gate] ')][-1]
    assert line == ('[event_gate] shadow session=2026-09-16 events=CPI@2026-09-16 '
                    'blocked=1 capped=1 tickers=AAPL,ZZTA bench_exempt=0')


def test_line_is_emitted_on_a_non_event_session(caplog):
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        _gate({'AAPL': 9000.0}, {}, events=None)
    line = [m for m in (r.getMessage() for r in caplog.records)
            if m.startswith('[event_gate] ')][-1]
    assert line == ('[event_gate] shadow session=2026-09-16 events=none '
                    'blocked=0 capped=0 tickers= bench_exempt=0')


# ── armed ───────────────────────────────────────────────────────────────────

def test_armed_blocks_a_new_open(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert 'ZZTA' not in _gate({'ZZTA': 1000.0}, {})


def test_armed_caps_an_add_at_the_held_size(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _gate({'AAPL': 9000.0}, {'AAPL': 4000.0})['AAPL'] == 4000.0


def test_armed_never_blocks_a_reduction(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _gate({'AAPL': 1000.0}, {'AAPL': 4000.0})['AAPL'] == 1000.0


def test_armed_converts_a_flip_to_close_only(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _gate({'AAPL': 5000.0}, {'AAPL': -3000.0})['AAPL'] == 0.0


def test_armed_on_a_non_event_session_changes_nothing(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    target = {'ZZTA': 1000.0}
    assert _gate(target, {}, events=None) == target


# ── R3: benchmark inclusion + regime independence ───────────────────────────

def test_benchmark_is_blocked_by_default(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert 'SPY' not in _gate({'SPY': 90_000.0}, {}, bench=['SPY'])


def test_benchmark_is_exempt_only_behind_the_switch(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    monkeypatch.setenv('OPENCLAW_EVENT_GATE_EXEMPT_BENCH', '1')
    out = _gate({'SPY': 90_000.0, 'ZZTA': 1000.0}, {}, bench=['SPY'])
    assert out['SPY'] == 90_000.0 and 'ZZTA' not in out


def test_bench_exempt_token_tracks_the_switch(monkeypatch, caplog):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE_EXEMPT_BENCH', '1')
    with caplog.at_level(logging.INFO, logger=rbs.logger.name):
        _gate({'ZZTA': 1000.0}, {}, bench=['SPY'])
    assert 'bench_exempt=1' in [m for m in (r.getMessage() for r in caplog.records)
                                if m.startswith('[event_gate] ')][-1]


def test_gate_never_reads_the_regime():
    import inspect
    src = inspect.getsource(rbs._apply_macro_event_gate)
    for token in ('regime', 'HIGH_VOL', 'CRISIS'):
        assert token not in src


def test_options_and_crypto_are_out_of_scope(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    target = {'AAPL260918C00250000': 400.0, 'BTC/USD': 30_000.0}
    assert _gate(target, {}) == target


# ── calendar failure is inert ───────────────────────────────────────────────

def test_calendar_failure_does_not_block_anything(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')

    def _boom(_session):
        raise RuntimeError('master unreadable')

    monkeypatch.setattr('lib.macro_events.gating_event', _boom)
    target = {'ZZTA': 1000.0}
    assert rbs._apply_macro_event_gate(dict(target), {}, session=SESSION) == target


def test_gate_is_wired_after_the_breaker_and_before_the_net_cap(monkeypatch):
    order = []
    monkeypatch.setattr(rbs, '_apply_asset_eligibility_gate',
                        lambda t, b, **k: (order.append('asset'), t)[1])
    monkeypatch.setattr(rbs, '_apply_entry_hygiene_gate',
                        lambda t, b, **k: (order.append('hygiene'), t)[1])
    monkeypatch.setattr(rbs, '_apply_account_breaker_gate',
                        lambda t, b, **k: (order.append('breaker'), t)[1])
    monkeypatch.setattr(rbs, '_apply_macro_event_gate',
                        lambda t, b, **k: (order.append('event'), t)[1])
    monkeypatch.setattr(rbs, '_apply_net_exposure_cap',
                        lambda t: (order.append('netcap'), t)[1])
    monkeypatch.setattr(rbs, '_classify_position_deltas', lambda t, b, m: [])
    rbs._emit_orders_from_targets({}, {}, 100_000.0, None, None, {}, {}, [], {},
                                  1.0, {'equity': 100_000.0}, broker={})
    assert order == ['asset', 'hygiene', 'breaker', 'event', 'netcap']
```

- [ ] **Step 2** — Run; expect `AttributeError: … has no attribute '_apply_macro_event_gate'`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_macro_event_gate.py -q
```

- [ ] **Step 3** — Add the gate to `src/execution/regime_blended_sizer.py`, immediately after `_apply_account_breaker_gate` (Task 6):

```python
EVENT_GATE_ENV = 'OPENCLAW_EVENT_GATE'
EVENT_GATE_EXEMPT_BENCH_ENV = 'OPENCLAW_EVENT_GATE_EXEMPT_BENCH'


def _event_gate_enabled() -> bool:
    return os.environ.get(EVENT_GATE_ENV) == '1'


def _event_gate_exempt_bench() -> bool:
    """Operator switch, ruling R3: the benchmark sleeve is NOT exempt by
    default (consistent with the 09-04 premarket-veto ruling)."""
    return os.environ.get(EVENT_GATE_EXEMPT_BENCH_ENV) == '1'


def _apply_macro_event_gate(target_usd, broker, *, session=None, events=None,
                            bench_tkrs=None):
    """Ruling R3: drop every NEW open/add from T-1 through the release session
    of a high-importance macro event (FOMC_DECISION, CPI, NFP).

    Exits, reductions and orphan closes are untouched (orphan closes never
    enter target_usd). ALL REGIMES — this function never reads the regime.
    Benchmark tickers are included unless OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1.

    SHADOW unless OPENCLAW_EVENT_GATE=1: the ORIGINAL dict is returned and only
    the `[event_gate] shadow ...` line is emitted, so routing is byte-identical.
    The line is emitted on EVERY cycle, including non-event sessions
    (events=none) — its absence must mean "the sizer did not run", never
    "nothing was gated".

    Blocked conviction is SHAVED, not redistributed (same philosophy as the
    per-ticker and cluster caps). `session`, `events` and `bench_tkrs` are
    injectable for tests; a calendar failure is inert."""
    if session is None:
        session = date.today()
    if events is None:
        try:
            from lib.macro_events import gating_event
            events = gating_event(session)
        except Exception as e:  # noqa: BLE001 — a gate that cannot read its
            # calendar must not block trading
            logger.warning('event_gate: calendar unreadable (%s: %s); inert',
                           type(e).__name__, e)
            events = None

    applying = _event_gate_enabled()
    exempt_bench = _event_gate_exempt_bench()
    exempt = set(bench_tkrs or ()) if exempt_bench else set()

    work = dict(target_usd or {})
    blocked, capped = [], []
    if events:
        for tkr in [t for t in (target_usd or {})
                    if t not in exempt and not _is_occ_symbol(t) and '/' not in t]:
            action = _clamp_to_held(work, tkr, broker)
            if action in ('blocked', 'unflipped'):
                # both are "this entry is refused"; the shadow line reports them
                # together under blocked=
                blocked.append(tkr)
            elif action == 'capped':
                capped.append(tkr)

    affected = sorted(set(blocked) | set(capped))
    logger.info('[event_gate] %s session=%s events=%s blocked=%d capped=%d '
                'tickers=%s bench_exempt=%d',
                'armed' if applying else 'shadow', session, events or 'none',
                len(blocked), len(capped), ','.join(affected[:20]),
                int(exempt_bench))
    return work if applying else target_usd
```

`date` is already imported at module scope (`from datetime import date` — used by `_sharpe_cadence_path`); confirm before relying on it.

- [ ] **Step 4** — Wire it in. In `_emit_orders_from_targets`, insert between the breaker gate (Task 6) and the net-cap comment:

```python
    # C3 (spec 2026-09-12, ruling R3): T-1..T macro-event entry block. All
    # regimes; benchmark included unless OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1.
    # SHADOW (line only) unless OPENCLAW_EVENT_GATE=1.
    target_usd = _apply_macro_event_gate(target_usd, broker, bench_tkrs=bench_tkrs)
```

- [ ] **Step 5** — Run the new tests; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_macro_event_gate.py tests/execution/test_account_breaker_sizer_gate.py -q
```

- [ ] **Step 6** — Run the touching module's existing sizer tests; expect PASS (shadow returns the input dict unchanged; the extra INFO line is harmless):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/execution/test_regime_blended_sizer.py tests/execution/test_entry_hygiene_gate.py tests/execution/test_sizer_benchmark_cap_exemptions.py tests/execution/test_sizer_flatten_zero_conviction.py tests/execution/test_trade_weight_factor_flag.py -q
```

- [ ] **Step 7** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/execution/regime_blended_sizer.py tests/execution/test_macro_event_gate.py
git commit -F - <<'MSG'
feat(sizer): T-1..T macro-event entry block, shadow-first (C3, R3)

Drops every NEW open/add on the session before and the session of a
FOMC_DECISION / CPI / NFP release, via the shared _clamp_to_held primitive so
exits, reductions and orphan closes stay structurally unblockable. All regimes.
The benchmark sleeve is INCLUDED by default; OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1
is the operator switch.

Placed in the emission tail beside the entry-hygiene gate rather than "before
rule C" as the spec sketch had it: opens vs adds are undecidable before the
broker book is loaded, and shave-don't-redistribute matches the caps.

OPENCLAW_EVENT_GATE unset => the original targets are returned untouched and
only `[event_gate] shadow ...` is logged, on EVERY cycle including non-event
sessions.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 11 — C3d: the backtest mirror in `_per_bar_simulate`

**Files:**
- `src/backtest/unified_backtest.py` (extend)
- `tests/backtest/test_event_gate_backtest_mirror.py` (new)

**Interfaces:**

Consumes:
- `lib.macro_events.gated_sessions(start, end) -> dict[date, list[str]]` (Task 8)
- `_per_bar_simulate(instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt, *, strategy_id=None, resolver=None, param_override=None, max_hold_days=…, fill_model=None, slippage_bps=0.0, cost_bps_by_ticker=None, asset_gate=None, restrict_universe_to_panel=False) -> dict` — `src/backtest/unified_backtest.py:731-748`
- the counter/loop/return sites: `entries_asset_gated = 0` (`:829`), `_dt_priority = …` (`:844`), `for current_date in oos_dates:` (`:859`), `days_with_signals += 1` (`:951`), the `_log` block (`:1108-1110`), the return dict (`:1112-1124`), and the `json.dumps({...})` provenance literal (`:1439-1470`)

Produces:
- env flag `OPENCLAW_BT_EVENT_GATE` (`'1'` arms)
- `entries_event_gated: int` in the `_per_bar_simulate` return dict
- `config_json` keys `event_gate` (`'on'|'off'`) and `entries_event_gated`

**Resolved ambiguity — the backtest gets its OWN flag, with no fallback to `OPENCLAW_EVENT_GATE`.** Spec §0 forbids stacking epochs and the atr_r fleet epoch runs to ≈ Tue 09-15. A shared flag would make the live arming of C3 silently re-epoch every subsequent fleet run — a live `.env` edit would change 156 strategies' stored Sharpes. So `unified_backtest` reads `OPENCLAW_BT_EVENT_GATE` only (matching the existing `OPENCLAW_BT_*` convention), both halves are flipped independently, and the backtest half enters through a post-atr_r drop-in exactly as spec §3 C3 requires ("include in the post-atr_r epoch drop-in ONLY if the operator confirms after seeing the shadow lines"). With both flags unset, every run is byte-identical to today's.

- [ ] **Step 1** — Write the failing test `tests/backtest/test_event_gate_backtest_mirror.py`:

```python
"""C3 backtest mirror: with OPENCLAW_BT_EVENT_GATE=1 the per-bar loop takes no
ENTRY on a T-1..T macro-event session. Exits are untouched.

Self-contained: synthetic bars, a synthetic trading calendar and a synthetic
macro_events master in tmp_path. aux_data is stubbed so nothing reaches the DB
or data/.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub   # noqa: E402
from lib import macro_events as me            # noqa: E402
from lib import trading_calendar as tc        # noqa: E402

DATES = pd.date_range('2026-09-01', periods=20, freq='B')


@pytest.fixture(autouse=True)
def _no_aux(monkeypatch):
    """load_aux_data is imported INSIDE _per_bar_simulate, so patching the
    source module is what takes effect."""
    import strategies.aux_data_loader as adl
    monkeypatch.setattr(adl, 'load_aux_data',
                        lambda *a, **k: {'options': {}}, raising=False)


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in DATES]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions',
                        lambda a, b: (_ for _ in ()).throw(AssertionError('no alpaca probe')))
    yield
    tc.clear_cache()


def _events(tmp_path, monkeypatch, sessions):
    rows = [{'event': 'CPI',
             'scheduled_at': pd.Timestamp(dt.datetime(d.year, d.month, d.day, 12), tz='UTC'),
             'session_date': d, 'source': 'test',
             'ingested_at': pd.Timestamp('2026-09-01T00:00:00Z')}
            for d in sessions]
    p = tmp_path / 'macro_events.parquet'
    pd.DataFrame(rows, columns=me.COLUMNS).to_parquet(p, index=False)
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(p))


def _dataset():
    closes = [100.0 + i for i in range(len(DATES))]
    close_wide = pd.DataFrame({'AAA': closes}, index=DATES)
    close_wide.index.name = 'date'
    bars = {'AAA': pd.DataFrame(
        {'open': [c - 0.5 for c in closes], 'high': [c + 0.2 for c in closes],
         'low': [c - 0.2 for c in closes], 'close': closes},
        index=pd.DatetimeIndex(DATES, name='date'))}
    regimes = pd.Series(['LOW_VOL'] * len(DATES), index=DATES)
    return close_wide, bars, regimes


def _instance():
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_event_gate'
        min_lookback = 1
        MAX_SIGNALS = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            close = float(prices['AAA'].iloc[-1])
            return [Signal(ticker='AAA', direction='LONG', entry_price=close,
                           stop_loss=close * 0.5, target_1=close * 1.5,
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED')]

    return Stub()


def _sim():
    close_wide, bars, regimes = _dataset()
    return ub._per_bar_simulate(_instance(), close_wide, bars, regimes,
                                DATES[0], DATES[-1], strategy_id='stub_event_gate',
                                fill_model='same_close')


def test_flag_unset_is_byte_identical(tmp_path, monkeypatch, calendar):
    _events(tmp_path, monkeypatch, [DATES[10].date()])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    out = _sim()
    assert out['entries_event_gated'] == 0
    assert len(out['trades']) > 0


def test_gate_skips_entries_on_t_minus_one_and_t(tmp_path, monkeypatch, calendar):
    gated = DATES[10].date()
    _events(tmp_path, monkeypatch, [gated])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    base = _sim()
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    out = _sim()
    assert out['entries_event_gated'] == 2          # T-1 and T, one signal each
    assert len(out['trades']) == len(base['trades']) - 2
    entered = {t['entry_date'] for t in out['trades']}
    assert gated not in entered
    assert DATES[9].date() not in entered


def test_a_calendar_with_no_events_gates_nothing(tmp_path, monkeypatch, calendar):
    _events(tmp_path, monkeypatch, [])
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_missing_master_is_inert(tmp_path, monkeypatch, calendar):
    monkeypatch.setenv(me.MASTER_PATH_ENV, str(tmp_path / 'nope.parquet'))
    monkeypatch.setenv('OPENCLAW_BT_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_the_live_flag_alone_does_not_arm_the_backtest(tmp_path, monkeypatch, calendar):
    """Spec §0: never stack epochs. Arming C3 live must not silently change
    every fleet run."""
    _events(tmp_path, monkeypatch, [DATES[10].date()])
    monkeypatch.delenv('OPENCLAW_BT_EVENT_GATE', raising=False)
    monkeypatch.setenv('OPENCLAW_EVENT_GATE', '1')
    assert _sim()['entries_event_gated'] == 0


def test_config_json_literal_carries_the_event_gate_keys():
    src = (ROOT / 'src' / 'backtest' / 'unified_backtest.py').read_text()
    assert "'event_gate':" in src
    assert "'entries_event_gated':" in src
```

- [ ] **Step 2** — Run; expect `KeyError: 'entries_event_gated'`:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_event_gate_backtest_mirror.py -q
```

- [ ] **Step 3** — Edit `src/backtest/unified_backtest.py`, four sites inside `_per_bar_simulate`.

(a) beside `entries_asset_gated = 0` (`:829`):

```python
    entries_event_gated = 0
```

(b) after `_dt_priority = os.environ.get('OPENCLAW_BT_DOUBLE_TOUCH', 'stop')` (`:844`):

```python
    # C3 (spec 2026-09-12): the live T-1..T macro-event entry block must have a
    # backtest twin — the backtest side is authoritative, so a live gate with no
    # backtest counterpart is forbidden. DELIBERATELY a separate flag from the
    # live OPENCLAW_EVENT_GATE and with NO fallback to it: spec §0 forbids
    # stacking epochs, and a shared flag would let a live .env edit silently
    # re-epoch all 156 strategies mid-fleet. The backtest half enters via the
    # post-atr_r drop-in, on operator confirmation. Empty dict = inert.
    _event_gate_sessions: dict = {}
    if os.environ.get('OPENCLAW_BT_EVENT_GATE') == '1':
        try:
            from lib.macro_events import gated_sessions
            _event_gate_sessions = gated_sessions(start_dt.date(), end_dt.date())
        except Exception as _e:  # noqa: BLE001
            print(f'[WARN] event gate calendar unreadable ({type(_e).__name__}: '
                  f'{_e}) — entries NOT gated', file=sys.stderr)
            _event_gate_sessions = {}
```

(c) immediately after `days_with_signals += 1` (`:951`), before `for sig in signals[:instance.MAX_SIGNALS]:`:

```python
        if _event_gate_sessions:
            _cd_gate = current_date.date() if hasattr(current_date, 'date') else current_date
            if _cd_gate in _event_gate_sessions:
                # ENTRIES only: the open-book exit walk at the top of this loop
                # and every simulate_trade already in flight are untouched.
                entries_event_gated += len(signals[:instance.MAX_SIGNALS])
                continue
```

(d) beside the `entries_asset_gated` log (`:1108`) and in the return dict (`:1112-1124`):

```python
    if entries_event_gated:
        _log(f'event gate: skipped {entries_event_gated} entries on '
             f'{len(_event_gate_sessions)} macro-event sessions (T-1..T of '
             f'FOMC_DECISION/CPI/NFP)')
```

```python
        'entries_event_gated': entries_event_gated,
```

- [ ] **Step 4** — Add the provenance keys to the `json.dumps({...})` literal (`:1439-1470`), next to `'double_touch'`:

```python
                # C3 (spec 2026-09-12): T-1..T macro-event entry block. 'off'
                # unless OPENCLAW_BT_EVENT_GATE=1 — a SEPARATE flag from the
                # live OPENCLAW_EVENT_GATE so arming the live gate can never
                # silently re-epoch the fleet.
                'event_gate': ('on' if os.environ.get('OPENCLAW_BT_EVENT_GATE') == '1'
                               else 'off'),
                'entries_event_gated': int(sim.get('entries_event_gated', 0)),
```

- [ ] **Step 5** — Run; expect PASS:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_event_gate_backtest_mirror.py -q
```

- [ ] **Step 6** — Run the touching module's existing per-bar tests (NOT `test_regime_stratified_backtest`, and not the whole `tests/backtest` directory while the fleet runs):

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m pytest tests/backtest/test_backtest_fill_model.py tests/backtest/test_open_book.py tests/backtest/test_backtest_oracles.py -q
```

- [ ] **Step 7** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add src/backtest/unified_backtest.py tests/backtest/test_event_gate_backtest_mirror.py
git commit -F - <<'MSG'
feat(backtest): mirror the T-1..T macro-event entry block (C3)

_per_bar_simulate skips the entry loop on gated sessions when
OPENCLAW_BT_EVENT_GATE=1, counts entries_event_gated, and stamps
event_gate / entries_event_gated into strategy_backtest_runs.config_json.
Exits — the open-book walk and in-flight simulate_trade brackets — are
untouched.

The backtest reads its OWN flag with NO fallback to the live
OPENCLAW_EVENT_GATE: spec §0 forbids stacking epochs, and a shared flag would
let a live .env edit silently re-epoch all 156 strategies mid-fleet. Both
unset => byte-identical runs.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

---

### Task 12 — changelog + stream verification

**Files:**
- `docs/archive/changelog.md` (modify — newest first, per spec §0)

**Interfaces:** none. Documentation + the spec §7 per-stream verification pass.

- [ ] **Step 1** — Run the stream's system checks and the quick doctor (spec §7). Neither reads `data/` masters heavily; skip if the fleet backtest is mid-run and note it:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 -m system_checks --tag storage
```

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions && python3 src/maintenance/doctor.py --quick
```

- [ ] **Step 2** — Prepend a Stream C entry at the TOP of `docs/archive/changelog.md` (newest first), filling in the bracketed values from the actual run:

```markdown
## 2026-09-13 — QuantDinger Stream C: risk (C1-C4)

Spec `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` §3; plan
`docs/superpowers/plans/2026-09-12-qd-stream-c-risk.md`. Rulings R2 (no regime
exemption) and R3 (block entries, sleeve not exempt by default) are binding.

- **C4 (item 16, pure bug fix, no flag)** — `run_premarket_scan` now reads the
  per-ticker social row from `ticker_sentiment_daily` (newest row within
  `OPENCLAW_PREMARKET_SOCIAL_MAX_AGE_DAYS`, default 3) and passes
  `social_posts_24h` / `social_bear_ratio` into `ScoreInputs` and the Sonnet
  confirmer. New per-ticker log line carries `social_source=` (either
  `ticker_sentiment_daily:<date>` or `absent`). The scorer's
  `news_count_window < 1 => 0.0` precondition is deliberately unchanged.
- **C2 (ruling R2, pure bug fix, no flag)** — the HIGH_VOL/CRISIS skip had
  already been removed from `position_circuit_breaker.main()` on 2026-05-16;
  only the module docstring still advertised it, which is what the QuantDinger
  review read. Docstring corrected + a regression net (no regime literal or
  `if regime_state` in `main()`; all four regimes seeded with a positive
  `position_circuit_breaker_pct`).
- **C1 (flag `OPENCLAW_ACCOUNT_BREAKER`, SHADOW)** — new
  `src/execution/account_breaker.py` + migration 157
  (`account_breaker_state`, `account_daily_open`). Drawdown on ALPHA NAV
  (`equity − benchmark market value`) vs a persisted rolling peak at −10 %;
  daily loss on TOTAL equity vs the session's opening equity at −3 %; all
  regimes. Rides the existing `*/5 9-16 * * 1-5` cron as a second spawn — no
  new schedule. On breach (armed) it flattens every non-benchmark equity
  position through `regime_liquidator._close_symbol`, journals to
  `circuit_breaker_fires` (so `open_reconcile.reconcile_broker_closes` and the
  sizer's risk-exit cooldown pick it up for free), posts `#trade-reports`, and
  the sizer refuses alpha opens/adds while `halted`. Re-arm is operator-only:
  `OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso>`. Benchmark-ticker lookup
  fails CLOSED. Shadow line: `[account_breaker] shadow … rule=none breach=0`,
  emitted every tick. Clean shadow days: [DATE1], [DATE2]. Armed: [DATE or "not yet"].
- **C3 (flags `OPENCLAW_EVENT_GATE` live / `OPENCLAW_BT_EVENT_GATE` backtest,
  both SHADOW/off)** — new master `data/master/macro_events.parquet`
  (append-only, dedup `(event, scheduled_at)`) fed by
  `src/ingestion/ingest_macro_events.py` (Fed / BLS / BEA, keyless, tag-stripped
  text parsers, `--from-file` escape hatch), read by `src/lib/macro_events.py`.
  Monthly `openclaw-macro-events.timer` (2nd, 07:00 UTC). Sizer gate drops NEW
  opens/adds from T-1 through the release session of FOMC_DECISION / CPI / NFP,
  all regimes, benchmark included unless `OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1`;
  backtest twin in `_per_bar_simulate` with `event_gate` +
  `entries_event_gated` in `config_json`. The two flags are INDEPENDENT on
  purpose (spec §0: never stack epochs). New system check `macro_events_fresh`
  (forward coverage ≥ 30 d); `macro_events.parquet` is in
  `master_freshness._COVERED_ELSEWHERE`, not `_CADENCES`. Backfill achieved:
  [START]→[END]. Shadow line: `[event_gate] shadow session=… events=none …`,
  emitted every cycle. Clean shadow days: [DATE1], [DATE2].

Deviations from the spec sketch, recorded deliberately:
- C2 was a docstring fix, not a code removal — the branch was already gone.
- The C3 gate runs in the emission tail beside `_apply_entry_hygiene_gate`, not
  "before rule C": opens vs adds are undecidable before the broker book is
  loaded. Blocked conviction is shaved, not redistributed to SPY.
- The backtest mirror uses `OPENCLAW_BT_EVENT_GATE` with no fallback to the
  live flag.
- The Fed/BLS/BEA parsers were authored against hand-written fixtures; the
  operator validation gate (plan Task 8 Step 12) is what proved them against
  live HTML on [DATE].
```

- [ ] **Step 3** — Commit:

```bash
cd /root/openclaw/.claude/worktrees/qd-adoptions
git add docs/archive/changelog.md
git commit -F - <<'MSG'
docs(changelog): QuantDinger Stream C — risk (C1-C4)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
MSG
```

- [ ] **Step 4** — **OPERATOR-RUN arming sequence** (nothing above flips a flag). In order, and only after the stated evidence exists:

1. Merge the branch to `main` once the whole stream is green (spec §0: never leave `main` half-edited across a timer boundary — merge outside the 15:00 ET chain and the weekend fleet window).
2. Restart user-scope johnbot so migration 157 applies and the cron picks up the new spawn:
   `systemctl --user restart johnbot`
3. Watch two consecutive RTH sessions:
   `grep -h '\[account_breaker\]' /root/openclaw/logs/account_breaker_*.log | tail -40`
   `grep -h '\[event_gate\]' /root/openclaw/logs/*.log | tail -20`
   `grep -h 'social_source=' /root/openclaw/logs/*premarket*.log | tail -20`
   Both breaker and gate lines must appear on every expected tick/cycle with no
   `failed`/`ERROR` token.
4. Only then, and only with operator sign-off, set `OPENCLAW_ACCOUNT_BREAKER=1`
   and/or `OPENCLAW_EVENT_GATE=1` in `/root/openclaw/.env` and restart
   user-scope johnbot. Record the two clean dates in the changelog entry.
5. `OPENCLAW_BT_EVENT_GATE=1` is a SEPARATE, LATER decision that belongs in a
   post-atr_r fleet drop-in — never set it while a fleet epoch is in flight.

---

## Self-review

### Spec coverage

| Spec item | Requirement | Task(s) |
|---|---|---|
| C1 | alpha NAV = equity − benchmark market value; rolling peak persisted | Task 3 (`alpha_nav`, `evaluate`), Task 4 (`account_breaker_state.peak_alpha_nav`) |
| C1 | opening-equity snapshot with `estimated` | Task 3 (`account_daily_open`), Task 4 (`opening_equity`, sources `stored\|ohlc\|equity`) |
| C1 | dd ≤ −0.10 OR daily ≤ −0.03 | Task 3 (`DD_LIMIT`, `DAILY_LIMIT`, `evaluate`) |
| C1 | evaluated by the existing 5-min RTH cron, no new thread | Task 7 (second spawn in the `*/5 9-16 * * 1-5` block) |
| C1 | ALL regimes | Task 3 (`test_module_never_reads_the_regime`) |
| C1 | flatten non-benchmark positions via liquidator primitives, RTH-only, poll-to-terminal | Task 5 (`flatten_alpha` → `_close_symbol`) |
| C1 | `#trade-reports` post | Task 7 (`_post('trade-reports', …)`) |
| C1 | sizer refuses alpha opens/adds while halted | Task 6 (`_apply_account_breaker_gate`) |
| C1 | failed submits retry next tick, state `pending_flatten` | Task 5 (`pending`), Task 7 (halted-branch retry) |
| C1 | `OPENCLAW_ACCOUNT_BREAKER=1` vs `[account_breaker] shadow …` | Task 4 (`format_line`), Task 7 (`main`) |
| C1 | re-arm token `OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso>`; peak resets | Task 4 (`rearm_requested`, `clear_halt`) |
| C1 | table numbering 157+ (155/156 reserved by Stream B) | Task 3 (`157_account_breaker.sql`) |
| C2 | remove the HIGH_VOL/CRISIS skip + docstring + tests | Task 2 (docstring; the branch was already gone — drift, below) |
| C3 | master `macro_events.parquet`, dedup `(event, scheduled_at)`, `session_date` via trading_calendar | Task 8 (`ingest_macro_events`, `session_date_for`, `KEY_COLS`) |
| C3 | Fed / BLS / BEA keyless parsers | Task 8 (`parse_fed`, `parse_titled` × BLS/BEA) |
| C3 | backfill 2017→2027 | Task 8 Step 13 (OPERATOR-RUN `--backfill`) |
| C3 | monthly `openclaw-macro-events.timer` | Task 9 (unit files + install commands) |
| C3 | system check `macro_events_fresh` (next event ≥ 30 d) | Task 9 (`MIN_HORIZON_DAYS = 30`) |
| C3 | sizer gate T-1..T for FOMC_DECISION/CPI/NFP, entries only, all regimes | Task 10 (`_apply_macro_event_gate`) |
| C3 | bench included unless `OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1` | Task 10 |
| C3 | `OPENCLAW_EVENT_GATE=1` vs `[event_gate] shadow …` | Task 10 |
| C3 | backtest mirror + `event_gate` in `config_json` | Task 11 |
| C4 | read the latest `ticker_sentiment_daily` row within N days per scan ticker | Task 1 (`_social_rows_from_cursor`, `_load_social_for_tickers`) |
| C4 | pass `social_posts_24h` / `social_bear_ratio` through | Task 1 (`ScoreInputs`, persisted row, confirmer input) |
| C4 | absent ⇒ 0 + `social_source=absent` in the log line | Task 1 |
| C4 | tests with a fake cursor | Task 1 (`FakeCursor`) |
| §0 | changelog entry, newest first | Task 12 |
| §7 | per-stream `system_checks --tag` + `doctor.py --quick` | Task 12 Step 1 |

Every C1-C4 sub-requirement maps to at least one task; no task exists without a spec line.

### Placeholder scan

No `TODO`, `FIXME`, `...`, `<fill in>` or elided function body appears in any code block. Every referenced symbol either exists in the tree (verified in the "Verified ground truth" table) or is produced by an earlier task in this plan:

- produced then consumed: `alpha_nav`/`evaluate` (3→7), `load_state`/`save_state`/`opening_equity`/`rearm_requested`/`clear_halt`/`format_line` (4→7), `bench_tickers`/`flatten_alpha`/`rule_threshold`/`rule_magnitude` (5→7), `_clamp_to_held` (6→10), `lib.macro_events.gating_event`/`gated_sessions`/`master_path`/`load_events`/`HIGH_IMPORTANCE`/`COLUMNS` (8→9, 8→10, 8→11), `parse_fed`/`parse_titled`/`merge_into_master`/`session_date_for`/`BLS_TITLES`/`BEA_TITLES`/`COLUMNS` (8→8 tests).
- pre-existing and verified: `_close_symbol`, `_market_is_open`, `_load_broker_positions`, `_post_to_discord`, `_alpaca_session`, `_fetch_account_state`, `_is_occ_symbol`, `_apply_entry_hygiene_gate`, `_apply_asset_eligibility_gate`, `_apply_net_exposure_cap`, `_classify_position_deltas`, `_emit_orders_from_targets`, `append_dedup`, `row_count`, `is_session`, `next_session`, `prev_session`, `clear_cache`, `MASTER_PATH_ENV`, `score_news_for_tickers`, `ScoreInputs`, `panic_score`, `confirm_panic`, `PremarketConfirmerInput`, `@check`, `Status`, `_COVERED_ELSEWHERE`, `_per_bar_simulate`, `circuit_breaker_fires`, `ticker_sentiment_daily`, `regime_sizer_params`, `execution_signals`, `strategy_registry`.
- one flagged assumption: Task 9's registry-introspection test uses `from src.system_checks.registry import all_checks`. Step 2 tells the implementer to read `registry.py` and substitute the real accessor if that name differs — the README only guarantees the `@check` decorator.

### Signature consistency

- `flatten_alpha(..., journal=True)` — declared in Task 5's Interfaces, used with `journal=live` in Task 7, and covered by `test_journal_false_writes_nothing`.
- `format_line(mode, *, equity, bench_mv, alpha, st, open_equity, open_src, halted, flatten=None)` — the keyword set in Task 4's byte-exact tests matches every Task 7 call site.
- `_clamp_to_held(out, tkr, broker) -> str` returns `'blocked'|'unflipped'|'capped'|'none'`; Task 6 keeps all four buckets separate in its log, Task 10 folds `unflipped` into `blocked` for the line contract — stated in both.
- `_apply_account_breaker_gate(target_usd, broker, *, halted=None, bench_tkrs=None)` and `_apply_macro_event_gate(target_usd, broker, *, session=None, events=None, bench_tkrs=None)` both take `(target_usd, broker)` positionally, matching how `_emit_orders_from_targets` calls the existing two gates.
- `gated_sessions(start, end, events=HIGH_IMPORTANCE)` — Task 11 calls it positionally with two dates; Task 8's tests use the same shape.
- `session_date_for(scheduled_at_utc) -> date` takes a UTC datetime/Timestamp in both `_row` and Task 8's tests.
- `run(sources, *, from_file=None, backfill=False, start_year, end_year, master_path, dry_run=False)` — Task 8's tests pass `master_path=` and `from_file=`; `main()` passes all of them.
- `evaluate(alpha, peak, equity, opening_equity)` is positional in Task 3's tests and in Task 7's call.

### Line-number drift found (spec §3 vs the tree, 2026-09-13)

| Spec citation | Actual |
|---|---|
| `bench_realized.py:23,40-43` | `:23` correct; NAV loader is `:39-43` |
| `server.js:2546,2671` | store path constant `:2547`; the sampler that writes it is `samplePnlCandle` at `:2599-2626`, not `:2671` |
| `cron-schedule.js:810-820` | the 5-min cron block runs `:810-829` |
| `open_reconcile.flatten_signal_close` "~227" | `:254-279` (and it is a signal-ledger helper, not an order path — the order primitive is `regime_liquidator._close_symbol`) |
| `position_circuit_breaker.py:8-9` "remove the branch" | **no branch exists**; `:71-76` records its removal on 2026-05-16. `:8-9` is a stale docstring only |
| `run_sentiment_step.py:239-262` | the Reddit + StockTwits stages run `:239-264` |
| `run_premarket_scan.py:191-192,207-208,231` | all three confirmed exactly |
| latest migration | `154_bench_corr_removal.sql`; Stream B reserves 155/156, so C1 takes 157 |

### Probe results (three keyless HEAD requests, 2026-09-13)

| URL | Status | Content-Type |
|---|---|---|
| `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` | **200** | `text/html` |
| `https://www.bls.gov/schedule/news_release/cpi.htm` | **403** | (Akamai bot block on a bare HEAD — browser headers required, see `_headers()`) |
| `https://www.bea.gov/news/schedule` | **200** | `text/html; charset=UTF-8` |
