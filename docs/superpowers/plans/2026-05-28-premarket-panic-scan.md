# Pre-Market Sentiment Panic Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a sidecar pre-market scanner that runs at 07:30 ET and 09:00 ET on trading days, uses the existing D1 sentiment surface to detect per-ticker panic signals against currently-held equity positions, optionally consults a Sonnet 4.6 confirmer, persists every result, posts a Discord advisory, and (when a strict default-OFF gate is flipped) auto-closes the affected names via pre-market limit orders.

**Architecture:** Standalone Python entry point (`src/pipeline/run_premarket_scan.py`) driven by a new systemd timer. Sentiment fetch reuses `news_finbert_scorer` and the Reddit/StockTwits scrapers via additive helpers (no breaking change to existing daily callers). Auto-close reuses `regime_liquidator` plus the existing executor's `_pick_limit_price` helper to submit `order submit --type limit --extended-hours --tif day`. New audit table `premarket_panic_alerts`. Three layered gates: `OPENCLAW_PREMARKET_SCAN` → `OPENCLAW_PREMARKET_CONFIRMER` → `OPENCLAW_PREMARKET_AUTOCLOSE`, validated at startup. Nothing in `pipeline_orchestrator.py`, the LangGraph orchestrator, the sizer, the regime-redeploy path, the backtests, the crypto exec lane, or any `feat/sp[1-5]-*` branch is modified.

**Tech Stack:** Python 3.11 + psycopg2 + Alpaca CLI (`/root/go/bin/alpaca`) + `/usr/local/bin/claude-bin` for Sonnet + FinBERT-Tone HTTP service on :7872 + systemd timers + pytest with monkeypatch / subprocess mocking.

**Spec reference:** `docs/superpowers/specs/2026-05-28-premarket-panic-scan-design.md` (commit `e8b5980`).

---

## Resolved open questions (from spec §10)

1. **`panic_score` formula (pinned):**
   ```
   panic_score =   0.60 * (news_finbert_neg_ratio * 100)
                 + 0.30 * min(news_count_window * 10, 100)
                 + 0.10 * (social_bear_ratio * 100)
   # clipped to [0, 100]
   # hard precondition: news_count_window >= 1
   #   (pure-social signals are documented for follow-up, not MVP)
   ```
   Default threshold `35` → fires on (a) 1 strongly-negative news item plus *any* social bear shift, or (b) any 3+ negative news items, or (c) heavy news volume even at neutral sentiment.

2. **Equity-only predicate:** filter at position-load time on the Alpaca position payload's `asset_class` field. Keep `asset_class == 'us_equity'`. Drop `crypto`, `us_option`, anything else. This avoids any dependency on our `instrument_class` enum and means crypto positions opened by SP-3.1 are skipped automatically.

3. **Pre-market order routing:** auto-close routes through `alpaca order submit --side sell --type limit --time-in-force day --extended-hours --symbol <SYM> --qty <abs(qty)> --limit-price <px>` for longs and the symmetric `--side buy` for shorts. Limit price comes from the existing `src/execution/alpaca_executor.py:_pick_limit_price()` helper (NBBO-cross with a 0.5% safety buffer). OPG / MOO is explicitly forbidden.

4. **Reddit / StockTwits raw persistence:** scrapers are stream-only — they do NOT write raw posts to Postgres. Consequence: the GLW replay tool can only use historical `market_news` rows for past dates; social-derived `panic_score` components for replays will read `0`. Documented limitation, not a blocker. Future spec can add a raw-post archive table.

---

## File structure

| File | Status | Responsibility |
|------|--------|----------------|
| `src/database/migrations/120_premarket_panic_alerts.sql` | NEW | Audit table schema. |
| `src/sentiment/premarket_scorer.py` | NEW | Pure rule engine. `panic_score(features) -> float`. Zero I/O. |
| `src/ingestion/news_finbert_scorer.py` | CHANGED (additive) | New `score_news_for_tickers(tickers, since_ts) -> list[dict]` sibling to existing `score_news_rows`. |
| `src/ingestion/reddit_client.py` | CHANGED (additive) | New `fetch_posts_for_tickers(tickers, since_ts) -> list[dict]` filter helper. |
| `src/ingestion/stocktwits_client.py` | CHANGED (additive) | New `fetch_for_tickers(tickers) -> dict[str, dict]` filter helper. |
| `src/sentiment/sonnet_premarket_confirmer.py` | NEW | Sonnet 4.6 wrapper. Mirrors `tradejohn_confirmer.py` shape. |
| `src/execution/regime_liquidator.py` | CHANGED (additive) | New `close_subset(tickers, reason) -> list[dict]` using extended-hours limit orders. Existing `liquidate_on_regime_change` and `_close_symbol` untouched. |
| `src/pipeline/premarket_helpers.py` | NEW | `resolve_premarket_webhook()`, `is_trading_day_in_et()`, `load_open_equity_positions()`. Small shared helpers. |
| `src/pipeline/run_premarket_scan.py` | NEW | Main entry point. CLI: `--scan-label`, `--dry-run`, `--tickers`. |
| `scripts/replay_premarket_panic.py` | NEW | Read-only replay tool. Never writes to alerts table. |
| `scripts/backfill_premarket_realized_pnl.py` | NEW | EOD 16:05 ET job. Idempotent on already-filled rows. |
| `docs/openclaw-premarket-scan.service` | NEW | systemd service. |
| `docs/openclaw-premarket-scan.timer` | NEW | systemd timer with two OnCalendar entries. |
| `docs/openclaw-premarket-realized-backfill.service` | NEW | systemd service for backfill. |
| `docs/openclaw-premarket-realized-backfill.timer` | NEW | systemd timer with one OnCalendar entry. |
| `tests/sentiment/test_premarket_scorer.py` | NEW | Unit tests for rule engine. |
| `tests/sentiment/test_sonnet_premarket_confirmer.py` | NEW | Unit tests for Sonnet wrapper with mocked subprocess. |
| `tests/execution/test_close_subset.py` | NEW | Unit + recorded-fixture tests for `close_subset`. |
| `tests/pipeline/test_premarket_helpers.py` | NEW | Unit tests for the three helper functions. |
| `tests/pipeline/test_premarket_scan.py` | NEW | Integration test against test Postgres + mocked CLI + mocked Sonnet. |
| `tests/pipeline/test_replay_premarket_panic.py` | NEW | Replay tool read-only and no-DB-writes test. |
| `tests/pipeline/test_premarket_backfill.py` | NEW | EOD backfill idempotency test. |

**Non-touch files** (verified by spec and grounding): `src/execution/pipeline_orchestrator.py`, `src/agent/graphs/daily-cycle.js`, `src/pipeline/run_sentiment_step.py`, `src/strategies/regime_blended_sizer_live.py`, `scripts/redeploy_pipeline.py`, all backtests, all `feat/sp*` branches.

---

### Task 1: Migration 120 — `premarket_panic_alerts` table

**Files:**
- Create: `src/database/migrations/120_premarket_panic_alerts.sql`
- Test: `tests/database/test_migration_120.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/database/test_migration_120.py
import os
import psycopg2
import pytest

DSN = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(DSN is None, reason='POSTGRES_URI not set')
def test_migration_120_creates_table_with_expected_columns():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'premarket_panic_alerts'
          ORDER BY ordinal_position
        """)
        cols = {name: dtype for name, dtype in cur.fetchall()}

    expected = {
        'id', 'scan_ts', 'scan_label', 'trading_day', 'ticker',
        'held_qty', 'avg_entry_price',
        'news_count_window', 'news_finbert_neg_ratio', 'news_finbert_mean_score',
        'social_post_count_window', 'social_bear_ratio',
        'panic_score', 'advisory_fired',
        'sonnet_verdict', 'sonnet_severity', 'sonnet_rationale',
        'sonnet_evidence_uuids', 'sonnet_cost_usd',
        'autoclose_fired', 'autoclose_liquidation_id',
        'realized_open_to_open_pct', 'realized_open_to_close_pct',
        'realized_backfilled_at', 'created_at',
    }
    missing = expected - cols.keys()
    assert not missing, f'missing columns: {missing}'
```

- [ ] **Step 2: Run test to verify it fails**

```bash
POSTGRES_URI=$DATABASE_URL pytest tests/database/test_migration_120.py -v
```
Expected: FAIL with `relation "premarket_panic_alerts" does not exist`.

- [ ] **Step 3: Write the migration**

```sql
-- src/database/migrations/120_premarket_panic_alerts.sql
-- Pre-market sentiment panic scanner: audit log of every scan output.
-- One row per (scan_ts, ticker). Realized PnL columns are filled by EOD job.

CREATE TABLE premarket_panic_alerts (
    id                          BIGSERIAL PRIMARY KEY,
    scan_ts                     TIMESTAMPTZ NOT NULL,
    scan_label                  TEXT NOT NULL,
    trading_day                 DATE NOT NULL,
    ticker                      TEXT NOT NULL,
    held_qty                    NUMERIC NOT NULL,
    avg_entry_price             NUMERIC,
    news_count_window           INT NOT NULL DEFAULT 0,
    news_finbert_neg_ratio      NUMERIC,
    news_finbert_mean_score     NUMERIC,
    social_post_count_window    INT NOT NULL DEFAULT 0,
    social_bear_ratio           NUMERIC,
    panic_score                 NUMERIC NOT NULL,
    advisory_fired              BOOLEAN NOT NULL DEFAULT FALSE,
    sonnet_verdict              TEXT,
    sonnet_severity             INT,
    sonnet_rationale            TEXT,
    sonnet_evidence_uuids       UUID[],
    sonnet_cost_usd             NUMERIC,
    autoclose_fired             BOOLEAN NOT NULL DEFAULT FALSE,
    autoclose_liquidation_id    BIGINT REFERENCES alpaca_liquidations(id),
    realized_open_to_open_pct   NUMERIC,
    realized_open_to_close_pct  NUMERIC,
    realized_backfilled_at      TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX premarket_panic_alerts_day_ticker
    ON premarket_panic_alerts(trading_day, ticker);
CREATE INDEX premarket_panic_alerts_scan_ts
    ON premarket_panic_alerts(scan_ts);
```

- [ ] **Step 4: Apply the migration**

Use the project's standard migration runner (whichever command the operator normally uses — verify by reading `scripts/run_migrations.py` or equivalent). Apply 120 once.

- [ ] **Step 5: Run test to verify it passes**

```bash
POSTGRES_URI=$DATABASE_URL pytest tests/database/test_migration_120.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/database/migrations/120_premarket_panic_alerts.sql tests/database/test_migration_120.py
git commit -m "feat(premarket-panic): migration 120 — premarket_panic_alerts audit table"
```

---

### Task 2: `premarket_scorer.py` — pure rule engine

**Files:**
- Create: `src/sentiment/premarket_scorer.py`
- Test: `tests/sentiment/test_premarket_scorer.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/sentiment/test_premarket_scorer.py
import math
import pytest
from src.sentiment.premarket_scorer import panic_score, ScoreInputs

def make(news_count=1, neg_ratio=0.0, mean_score=0.0,
         social_count=0, bear_ratio=0.0):
    return ScoreInputs(
        news_count_window=news_count,
        news_finbert_neg_ratio=neg_ratio,
        news_finbert_mean_score=mean_score,
        social_post_count_window=social_count,
        social_bear_ratio=bear_ratio,
    )

def test_zero_news_returns_zero_score_even_with_social():
    """News is a hard precondition; pure social cannot fire in MVP."""
    s = panic_score(make(news_count=0, bear_ratio=1.0, social_count=500))
    assert s == 0.0

def test_one_neutral_headline_low_score():
    s = panic_score(make(news_count=1, neg_ratio=0.0))
    # 0 + 0.3 * min(10, 100) = 3
    assert s == pytest.approx(3.0)

def test_one_fully_negative_headline_fires_strongly():
    s = panic_score(make(news_count=1, neg_ratio=1.0))
    # 60 + 3 = 63
    assert s == pytest.approx(63.0)

def test_threshold_boundary_fires_at_three_mild_negatives():
    s = panic_score(make(news_count=3, neg_ratio=0.5))
    # 30 + 0.3*30 = 39
    assert s == pytest.approx(39.0)

def test_threshold_boundary_no_fire_below():
    s = panic_score(make(news_count=1, neg_ratio=0.5))
    # 30 + 3 = 33 -- below default threshold 35
    assert s == pytest.approx(33.0)

def test_news_count_caps_at_100():
    """News-volume component is clipped — 100 articles doesn't mean 1000 score."""
    s = panic_score(make(news_count=50, neg_ratio=0.0))
    # 0 + 0.3 * min(500, 100) = 30
    assert s == pytest.approx(30.0)

def test_score_clipped_to_hundred():
    s = panic_score(make(news_count=20, neg_ratio=1.0, bear_ratio=1.0))
    assert s == 100.0

def test_score_never_negative():
    s = panic_score(make(news_count=1, neg_ratio=0.0, bear_ratio=0.0))
    assert s >= 0.0

def test_nan_inputs_treated_as_zero():
    s = panic_score(make(news_count=1, neg_ratio=float('nan')))
    assert s == pytest.approx(3.0)
    assert not math.isnan(s)

def test_negative_inputs_clamped_to_zero():
    s = panic_score(make(news_count=1, neg_ratio=-0.5))
    assert s == pytest.approx(3.0)

def test_ratio_above_one_clamped():
    s = panic_score(make(news_count=1, neg_ratio=1.5))
    # behaves as if neg_ratio=1.0
    assert s == pytest.approx(63.0)

def test_social_contributes_when_news_present():
    s = panic_score(make(news_count=1, neg_ratio=0.0, bear_ratio=1.0))
    # 0 + 3 + 10 = 13
    assert s == pytest.approx(13.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/sentiment/test_premarket_scorer.py -v
```
Expected: ALL FAIL with `ModuleNotFoundError: No module named 'src.sentiment.premarket_scorer'`.

- [ ] **Step 3: Write the implementation**

```python
# src/sentiment/premarket_scorer.py
"""Pure rule-based panic-score engine for the pre-market sentiment scan.

Inputs are *already-aggregated* sentiment features for a single ticker over
a single pre-market window (typically prior 18:00 ET -> now). Zero I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreInputs:
    news_count_window: int
    news_finbert_neg_ratio: float       # 0..1
    news_finbert_mean_score: float      # -1..1, currently informational only
    social_post_count_window: int
    social_bear_ratio: float            # 0..1


def _safe_unit(x: float) -> float:
    """Clamp to [0, 1]; NaN -> 0."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def panic_score(inp: ScoreInputs) -> float:
    """Composite 0..100 panic score.

    Hard precondition: news_count_window >= 1, otherwise returns 0.
    (Pure-social signals are a documented follow-up, not MVP.)
    """
    if inp.news_count_window < 1:
        return 0.0

    news_component   = 60.0 * _safe_unit(inp.news_finbert_neg_ratio)
    volume_component = 30.0 * (min(inp.news_count_window * 10, 100) / 100.0)
    social_component = 10.0 * _safe_unit(inp.social_bear_ratio)

    raw = news_component + volume_component + social_component
    return max(0.0, min(100.0, raw))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/sentiment/test_premarket_scorer.py -v
```
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/sentiment/premarket_scorer.py tests/sentiment/test_premarket_scorer.py
git commit -m "feat(premarket-panic): pure rule engine premarket_scorer.panic_score"
```

---

### Task 3: Extend `news_finbert_scorer.py` with `score_news_for_tickers`

**Files:**
- Modify: `src/ingestion/news_finbert_scorer.py` (additive: new sibling function, existing `score_news_rows` unchanged)
- Test: `tests/ingestion/test_score_news_for_tickers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/ingestion/test_score_news_for_tickers.py
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from src.ingestion.news_finbert_scorer import score_news_for_tickers


def _fake_market_news_rows(rows):
    """Return what psycopg2 cursor.fetchall() would return for our query."""
    return rows


@patch('src.ingestion.news_finbert_scorer.psycopg2.connect')
@patch('src.ingestion.news_finbert_scorer.score_news_rows')
def test_score_news_for_tickers_filters_by_ticker_and_window(
    mock_score, mock_connect
):
    since = datetime(2026, 5, 27, 22, 0, tzinfo=timezone.utc)
    fake_rows = [
        {'ticker': 'GLW', 'headline': 'CFO departs', 'summary': '', 'uuid': 'u1'},
        {'ticker': 'GLW', 'headline': 'Guidance cut', 'summary': '', 'uuid': 'u2'},
    ]
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (r['ticker'], r['headline'], r['summary'], r['uuid']) for r in fake_rows
    ]
    cursor.description = [('ticker',), ('headline',), ('summary',), ('uuid',)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_connect.return_value.__enter__.return_value = conn

    mock_score.return_value = [{
        'ticker': 'GLW',
        'news_count_24h': 2,
        'news_finbert_pos': 0.0,
        'news_finbert_neu': 0.0,
        'news_finbert_neg': 1.0,
        'news_mean_score': -0.9,
        'news_top_headlines': ['CFO departs', 'Guidance cut'],
    }]

    out = score_news_for_tickers(['GLW'], since)

    # Validate the SQL was parametrized with our ticker list and timestamp
    executed_sql, executed_args = cursor.execute.call_args[0]
    assert 'primary_ticker' in executed_sql
    assert 'related_tickers' in executed_sql
    assert executed_args[0] == ['GLW']  # ticker list
    assert executed_args[1] == since    # since_ts

    # Validate the returned shape
    assert len(out) == 1
    assert out[0]['ticker'] == 'GLW'
    assert out[0]['news_count_24h'] == 2

    # Validate UUIDs are surfaced so the confirmer can quote evidence
    assert out[0]['evidence_uuids'] == ['u1', 'u2']


@patch('src.ingestion.news_finbert_scorer.psycopg2.connect')
@patch('src.ingestion.news_finbert_scorer.score_news_rows')
def test_score_news_for_tickers_empty_returns_empty_list(mock_score, mock_connect):
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.description = [('ticker',), ('headline',), ('summary',), ('uuid',)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_connect.return_value.__enter__.return_value = conn

    out = score_news_for_tickers(['NONESUCH'], datetime.now(timezone.utc))
    assert out == []
    mock_score.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/ingestion/test_score_news_for_tickers.py -v
```
Expected: FAIL with `cannot import name 'score_news_for_tickers'`.

- [ ] **Step 3: Add the sibling function**

Read `src/ingestion/news_finbert_scorer.py` to confirm the existing module surface (imports of `psycopg2`, `score_news_rows`, etc.) — then append the sibling function. Do NOT modify `score_news_rows`.

```python
# Append to src/ingestion/news_finbert_scorer.py

from datetime import datetime
import os
import psycopg2


_NEWS_FETCH_SQL = """
    SELECT primary_ticker, title, summary, uuid
      FROM market_news
     WHERE (primary_ticker = ANY(%s) OR related_tickers && %s::text[])
       AND published_at >= %s
"""


def score_news_for_tickers(tickers: list[str], since_ts: datetime) -> list[dict]:
    """Fetch market_news for `tickers` since `since_ts`, score with FinBERT,
    return one aggregated dict per ticker (same shape as score_news_rows)
    plus an `evidence_uuids` list for downstream confirmer citation.

    Returns [] if no news rows are found. No DB writes.
    """
    if not tickers:
        return []

    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_NEWS_FETCH_SQL, (tickers, tickers, since_ts))
        rows = cur.fetchall()

    if not rows:
        return []

    # Map columns once
    news_rows = []
    uuids_by_ticker: dict[str, list[str]] = {}
    for ticker, title, summary, uuid in rows:
        news_rows.append({
            'ticker': ticker,
            'headline': title or '',
            'summary': summary or '',
        })
        uuids_by_ticker.setdefault(ticker, []).append(str(uuid))

    aggregated = score_news_rows(news_rows)
    for entry in aggregated:
        entry['evidence_uuids'] = uuids_by_ticker.get(entry['ticker'], [])
    return aggregated
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/ingestion/test_score_news_for_tickers.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Confirm existing daily caller is unbroken**

```bash
pytest tests/ -k "test_run_sentiment_step or test_news_finbert" -v
```
Expected: existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/news_finbert_scorer.py tests/ingestion/test_score_news_for_tickers.py
git commit -m "feat(premarket-panic): score_news_for_tickers sibling helper"
```

---

### Task 4: Sonnet pre-market confirmer wrapper

**Files:**
- Create: `src/sentiment/sonnet_premarket_confirmer.py`
- Test: `tests/sentiment/test_sonnet_premarket_confirmer.py`

The wrapper must mirror `src/execution/tradejohn_confirmer.py:_default_runner` and `_extract_inner_json` — same `claude-bin` invocation, same JSON-extraction regex with fenced-code fallback, same fail-open semantics.

- [ ] **Step 1: Write the failing tests**

```python
# tests/sentiment/test_sonnet_premarket_confirmer.py
import json
from unittest.mock import patch, MagicMock
from src.sentiment.sonnet_premarket_confirmer import (
    PremarketConfirmerInput,
    confirm_panic,
    PANIC_VERDICTS,
)


def _make_input():
    return PremarketConfirmerInput(
        ticker='GLW',
        held_qty=100,
        panic_score=72.0,
        news_count=2,
        finbert_neg_ratio=1.0,
        social_bear_ratio=0.3,
        top_headlines=[
            ('CFO departs unexpectedly', -0.91, 'uuid-1'),
            ('Q3 guidance cut by 20%', -0.88, 'uuid-2'),
        ],
    )


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_parses_well_formed_json(mock_run):
    sonnet_resp = {
        'result': json.dumps({
            'panic_verdict': 'bearish_news_driven',
            'severity': 5,
            'rationale': 'CFO departure plus guidance cut is a hard catalyst.',
            'evidence_uuids': ['uuid-1', 'uuid-2'],
        }),
        'total_cost_usd': 0.013,
    }
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(sonnet_resp).encode(),
        stderr=b'',
    )

    out = confirm_panic(_make_input())

    assert out.verdict == 'bearish_news_driven'
    assert out.severity == 5
    assert out.evidence_uuids == ['uuid-1', 'uuid-2']
    assert out.cost_usd == 0.013
    assert 'CFO departure' in out.rationale


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_handles_fenced_json_block(mock_run):
    fenced = '```json\n' + json.dumps({
        'panic_verdict': 'neutral',
        'severity': 2,
        'rationale': 'Routine analyst report.',
        'evidence_uuids': [],
    }) + '\n```'
    sonnet_resp = {'result': fenced, 'total_cost_usd': 0.01}
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(sonnet_resp).encode())

    out = confirm_panic(_make_input())
    assert out.verdict == 'neutral'
    assert out.severity == 2


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_returns_llm_error_on_subprocess_failure(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stdout=b'', stderr=b'budget exceeded')
    out = confirm_panic(_make_input())
    assert out.verdict == 'llm_error'
    assert out.severity is None
    assert 'budget exceeded' in out.rationale


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_returns_llm_error_on_malformed_json(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps({'result': 'not json at all', 'total_cost_usd': 0.0}).encode(),
    )
    out = confirm_panic(_make_input())
    assert out.verdict == 'llm_error'


@patch('src.sentiment.sonnet_premarket_confirmer.subprocess.run')
def test_confirm_panic_rejects_unknown_verdict(mock_run):
    bad = {'result': json.dumps({
        'panic_verdict': 'definitely_panic_lol',
        'severity': 5,
        'rationale': 'x',
        'evidence_uuids': [],
    }), 'total_cost_usd': 0.01}
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(bad).encode())
    out = confirm_panic(_make_input())
    assert out.verdict == 'llm_error'
    assert 'unknown verdict' in out.rationale.lower()


def test_panic_verdicts_are_pinned():
    """If you add a verdict, update the auto-close gate logic in run_premarket_scan."""
    assert PANIC_VERDICTS == (
        'bullish', 'neutral', 'bearish_news_driven', 'bearish_idiosyncratic'
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/sentiment/test_sonnet_premarket_confirmer.py -v
```
Expected: ALL FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/sentiment/sonnet_premarket_confirmer.py
"""Sonnet 4.6 pre-market panic confirmer.

Mirrors src/execution/tradejohn_confirmer.py: subprocess to /usr/local/bin/claude-bin,
--output-format json, --max-budget-usd, JSON-with-fenced-fallback parse, fail-open
on any error (verdict='llm_error').
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass


PANIC_VERDICTS = (
    'bullish', 'neutral', 'bearish_news_driven', 'bearish_idiosyncratic',
)

DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_MODEL = 'sonnet'

_JSON_OBJ_RE = re.compile(r'\{[\s\S]*\}')


@dataclass(frozen=True)
class PremarketConfirmerInput:
    ticker: str
    held_qty: float
    panic_score: float
    news_count: int
    finbert_neg_ratio: float
    social_bear_ratio: float
    # list of (headline, finbert_score, uuid)
    top_headlines: list[tuple[str, float, str]]


@dataclass(frozen=True)
class PremarketConfirmerResult:
    verdict: str                # one of PANIC_VERDICTS or 'llm_error'
    severity: int | None        # 1..5 or None on error
    rationale: str
    evidence_uuids: list[str]
    cost_usd: float | None


_PROMPT_TEMPLATE = """You are a pre-market risk reviewer for a trading desk.

A rule-based scanner flagged the following ticker for potential panic-selling
risk. Inspect the headlines and decide whether the news is a genuine,
idiosyncratic, bearish catalyst that warrants flattening the position before
the open, or whether it is routine noise that does NOT justify action.

Ticker: {ticker}
Held qty (signed): {held_qty}
Composite panic_score (0-100): {panic_score:.1f}
News count in pre-market window: {news_count}
FinBERT negative ratio: {finbert_neg_ratio:.2f}
Social bear ratio: {social_bear_ratio:.2f}

Top headlines (with FinBERT score; negative = bearish):
{headline_block}

Reply with ONLY a single JSON object, no prose, no markdown fences:
{{
  "panic_verdict": "bullish" | "neutral" | "bearish_news_driven" | "bearish_idiosyncratic",
  "severity": 1..5,
  "rationale": "<2-3 sentences citing the specific headlines>",
  "evidence_uuids": ["<uuid>", ...]
}}

Definitions:
- bearish_news_driven: hard catalyst (guidance cut, CFO/CEO departure, fraud,
  major customer loss, regulatory action, going-concern doubt, M&A break).
- bearish_idiosyncratic: company-specific bearish pressure that isn't a hard
  named catalyst but is real and unlikely to mean-revert by close (e.g.,
  multi-source downgrades, sector-relative weakness with a named driver).
- neutral: routine, mixed, or noise (price-target tweak with no thesis change,
  brokerage upgrade-then-downgrade noise, generic sector comment).
- bullish: the news is on balance positive even if FinBERT scored it negative.

Severity scale (apply only to bearish_* verdicts; use 1 otherwise):
1 = mild, fade-by-noon likely
3 = real but limited downside
5 = hard catalyst, flatten-by-open is defensible
"""


def _build_prompt(inp: PremarketConfirmerInput) -> str:
    if inp.top_headlines:
        headline_block = '\n'.join(
            f'  - [{score:+.2f}] {headline}  (uuid={uuid})'
            for headline, score, uuid in inp.top_headlines
        )
    else:
        headline_block = '  (no headlines available)'
    return _PROMPT_TEMPLATE.format(
        ticker=inp.ticker,
        held_qty=inp.held_qty,
        panic_score=inp.panic_score,
        news_count=inp.news_count,
        finbert_neg_ratio=inp.finbert_neg_ratio,
        social_bear_ratio=inp.social_bear_ratio,
        headline_block=headline_block,
    )


def _extract_inner_json(body: str) -> dict | None:
    body = body.strip()
    fenced = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', body)
    if fenced:
        body = fenced.group(1)
    else:
        match = _JSON_OBJ_RE.search(body)
        if match:
            body = match.group(0)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def confirm_panic(inp: PremarketConfirmerInput,
                  max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
                  model: str = DEFAULT_MODEL) -> PremarketConfirmerResult:
    """Invoke claude-bin Sonnet, parse, return a typed result. Never raises."""
    prompt = _build_prompt(inp)

    proc = subprocess.run(
        [
            '/usr/local/bin/claude-bin',
            '--print',
            '--output-format', 'json',
            '--model', model,
            '--max-budget-usd', f'{max_budget_usd:.2f}',
        ],
        input=prompt.encode(),
        capture_output=True,
        timeout=300,
    )

    if proc.returncode != 0:
        return PremarketConfirmerResult(
            verdict='llm_error',
            severity=None,
            rationale=f'claude-bin exit {proc.returncode}: {proc.stderr[:200].decode(errors="replace")}',
            evidence_uuids=[],
            cost_usd=None,
        )

    try:
        outer = json.loads(proc.stdout.decode())
        cost = float(outer.get('total_cost_usd') or 0.0)
        inner = _extract_inner_json(outer.get('result', ''))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return PremarketConfirmerResult(
            verdict='llm_error', severity=None,
            rationale=f'outer parse: {e}', evidence_uuids=[], cost_usd=None,
        )

    if inner is None:
        return PremarketConfirmerResult(
            verdict='llm_error', severity=None,
            rationale='inner JSON not found in Sonnet output',
            evidence_uuids=[], cost_usd=cost,
        )

    verdict = str(inner.get('panic_verdict', '')).strip()
    if verdict not in PANIC_VERDICTS:
        return PremarketConfirmerResult(
            verdict='llm_error', severity=None,
            rationale=f'unknown verdict {verdict!r}', evidence_uuids=[],
            cost_usd=cost,
        )

    try:
        severity = int(inner.get('severity'))
    except (TypeError, ValueError):
        severity = None

    rationale = str(inner.get('rationale', '')).strip()
    evidence = [str(u) for u in (inner.get('evidence_uuids') or [])]

    return PremarketConfirmerResult(
        verdict=verdict,
        severity=severity,
        rationale=rationale,
        evidence_uuids=evidence,
        cost_usd=cost,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/sentiment/test_sonnet_premarket_confirmer.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/sentiment/sonnet_premarket_confirmer.py tests/sentiment/test_sonnet_premarket_confirmer.py
git commit -m "feat(premarket-panic): Sonnet 4.6 confirmer wrapper with fail-open semantics"
```

---

### Task 5: `regime_liquidator.close_subset` — extended-hours limit-order subset close

**Files:**
- Modify: `src/execution/regime_liquidator.py` (additive: new function `close_subset`; existing flow unchanged)
- Test: `tests/execution/test_close_subset.py`

Reuse `src/execution/alpaca_executor.py:_pick_limit_price()` to compute the limit price (NBBO-cross with 0.5% buffer). Submit via `alpaca order submit --type limit --time-in-force day --extended-hours --side <sell|buy> --symbol <SYM> --qty <abs(qty)> --limit-price <px>`. Audit each outcome to `alpaca_liquidations` with `regime_from = reason` and `regime_to = 'FLAT'`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/execution/test_close_subset.py
from unittest.mock import patch, MagicMock
from src.execution.regime_liquidator import close_subset


def _fake_positions(by_ticker):
    """Return list-of-dicts shape like 'alpaca position list' JSON output."""
    return [
        {'symbol': sym, 'qty': str(qty), 'asset_class': 'us_equity',
         'market_value': str(abs(qty) * 10)}
        for sym, qty in by_ticker.items()
    ]


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions')
def test_close_subset_only_closes_named_tickers(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({
        'GLW': 100, 'AAPL': 50, 'MSFT': -25, 'NVDA': 200,
    })
    mock_submit.return_value = {
        'status': 'filled', 'filled_qty': 100, 'avg_fill_price': 32.10,
    }
    out = close_subset(['GLW', 'MSFT'], reason='PREMARKET_PANIC')

    submitted = [c.args[0]['symbol'] for c in mock_submit.call_args_list]
    assert set(submitted) == {'GLW', 'MSFT'}
    assert 'AAPL' not in submitted and 'NVDA' not in submitted
    assert len(out) == 2


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions')
def test_close_subset_uses_sell_for_long_and_buy_for_short(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({'GLW': 100, 'MSFT': -25})
    mock_submit.return_value = {'status': 'pending'}
    close_subset(['GLW', 'MSFT'], reason='PREMARKET_PANIC')

    by_symbol = {c.args[0]['symbol']: c.args[0] for c in mock_submit.call_args_list}
    assert by_symbol['GLW']['side'] == 'sell'
    assert by_symbol['GLW']['qty'] == 100
    assert by_symbol['MSFT']['side'] == 'buy'
    assert by_symbol['MSFT']['qty'] == 25


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions')
def test_close_subset_audits_every_attempt_with_reason(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({'GLW': 100})
    mock_submit.return_value = {'status': 'filled', 'filled_qty': 100, 'avg_fill_price': 32.0}

    close_subset(['GLW'], reason='PREMARKET_PANIC')

    audit_args = mock_audit.call_args[0][0]
    assert audit_args['ticker'] == 'GLW'
    assert audit_args['regime_from'] == 'PREMARKET_PANIC'
    assert audit_args['regime_to'] == 'FLAT'
    assert audit_args['result_status'] == 'filled'


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions')
def test_close_subset_continues_on_per_ticker_failure(
    mock_load, mock_submit, mock_audit,
):
    mock_load.return_value = _fake_positions({'GLW': 100, 'AAPL': 50})
    mock_submit.side_effect = [
        RuntimeError('alpaca CLI: halted'),
        {'status': 'filled', 'filled_qty': 50, 'avg_fill_price': 200.0},
    ]
    out = close_subset(['GLW', 'AAPL'], reason='PREMARKET_PANIC')

    assert len(out) == 2
    statuses = {r['ticker']: r['status'] for r in out}
    assert statuses['GLW'] == 'submit_error'
    assert statuses['AAPL'] == 'filled'
    assert mock_audit.call_count == 2


@patch('src.execution.regime_liquidator._write_liquidation_audit')
@patch('src.execution.regime_liquidator._submit_extended_hours_close')
@patch('src.execution.regime_liquidator._load_broker_positions')
def test_close_subset_skips_unknown_ticker(mock_load, mock_submit, mock_audit):
    mock_load.return_value = _fake_positions({'GLW': 100})
    out = close_subset(['GLW', 'NOSUCH'], reason='PREMARKET_PANIC')

    assert mock_submit.call_count == 1
    statuses = {r['ticker']: r['status'] for r in out}
    assert statuses['GLW'] != 'submit_error'
    assert statuses['NOSUCH'] == 'no_position'
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/execution/test_close_subset.py -v
```
Expected: ALL FAIL with `cannot import name 'close_subset'`.

- [ ] **Step 3: Add `close_subset` and its private helpers to `regime_liquidator.py`**

Read `src/execution/regime_liquidator.py` first to locate `_close_symbol`, `_run_cli`, and the audit-write function so the new code matches the existing module's conventions (logging, exception style, `_run_cli` signature). Then append the following at module bottom (do NOT touch `_close_symbol` or `liquidate_on_regime_change`):

```python
# Append to src/execution/regime_liquidator.py

from src.execution.alpaca_executor import _pick_limit_price


def _load_broker_positions() -> list[dict]:
    """Shell out to `alpaca position list` and return parsed list-of-dicts.
    Returns []  on any CLI error (caller logs)."""
    ok, payload, _err = _run_cli(['position', 'list'], timeout=15)
    if not ok or not isinstance(payload, list):
        return []
    return payload


def _submit_extended_hours_close(order: dict) -> dict:
    """Submit a single pre-market limit close via the alpaca CLI.

    `order` keys: symbol, side ('sell'|'buy'), qty (abs int|float), limit_price.
    Returns a dict with at least: status, filled_qty, avg_fill_price.
    Raises RuntimeError on submission failure.
    """
    cmd = [
        'order', 'submit',
        '--symbol', order['symbol'],
        '--side', order['side'],
        '--type', 'limit',
        '--time-in-force', 'day',
        '--extended-hours',
        '--qty', str(order['qty']),
        '--limit-price', f'{order["limit_price"]:.2f}',
    ]
    ok, payload, err = _run_cli(cmd, timeout=15)
    if not ok:
        raise RuntimeError(f'alpaca CLI: {err}')
    return {
        'status': payload.get('status', 'pending'),
        'filled_qty': float(payload.get('filled_qty', 0) or 0),
        'avg_fill_price': float(payload.get('filled_avg_price', 0) or 0),
        'order_id': payload.get('id'),
    }


def _write_liquidation_audit(row: dict) -> int | None:
    """INSERT a row into alpaca_liquidations; return the new id or None.
    Schema fields: run_date, regime_from, regime_to, ticker, direction, qty,
    market_value_usd, result_status, filled_qty, avg_fill_price."""
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alpaca_liquidations
                (run_date, regime_from, regime_to, ticker, direction, qty,
                 market_value_usd, result_status, filled_qty, avg_fill_price,
                 created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            RETURNING id
            """,
            (
                row['run_date'], row['regime_from'], row['regime_to'],
                row['ticker'], row['direction'], row['qty'],
                row.get('market_value_usd'), row['result_status'],
                row.get('filled_qty'), row.get('avg_fill_price'),
            ),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def close_subset(tickers: list[str], reason: str) -> list[dict]:
    """Pre-market subset close. For each ticker in `tickers` that the broker
    is currently long or short, submit an extended-hours limit order to flat,
    audit the outcome, continue on per-ticker error.

    Returns a list of result dicts: {ticker, status, qty, filled_qty,
    avg_fill_price, liquidation_id, error}.
    """
    if not tickers:
        return []

    from datetime import date  # local import: avoid module-load circular risk
    positions = _load_broker_positions()
    by_sym = {p['symbol']: p for p in positions if p.get('asset_class') == 'us_equity'}

    results: list[dict] = []
    for ticker in tickers:
        pos = by_sym.get(ticker)
        if pos is None:
            results.append({'ticker': ticker, 'status': 'no_position'})
            continue

        signed_qty = float(pos['qty'])
        abs_qty = abs(signed_qty)
        side = 'sell' if signed_qty > 0 else 'buy'
        try:
            limit_px = _pick_limit_price(symbol=ticker, side=side)
            outcome = _submit_extended_hours_close({
                'symbol': ticker, 'side': side, 'qty': abs_qty,
                'limit_price': limit_px,
            })
            audit_id = _write_liquidation_audit({
                'run_date': date.today(),
                'regime_from': reason,
                'regime_to': 'FLAT',
                'ticker': ticker,
                'direction': 'long' if signed_qty > 0 else 'short',
                'qty': abs_qty,
                'market_value_usd': float(pos.get('market_value', 0) or 0),
                'result_status': outcome['status'],
                'filled_qty': outcome['filled_qty'],
                'avg_fill_price': outcome['avg_fill_price'],
            })
            results.append({
                'ticker': ticker, 'status': outcome['status'],
                'qty': abs_qty, 'filled_qty': outcome['filled_qty'],
                'avg_fill_price': outcome['avg_fill_price'],
                'liquidation_id': audit_id,
            })
        except Exception as e:  # noqa: BLE001 — intentional: continue on any failure
            _write_liquidation_audit({
                'run_date': date.today(),
                'regime_from': reason, 'regime_to': 'FLAT',
                'ticker': ticker,
                'direction': 'long' if signed_qty > 0 else 'short',
                'qty': abs_qty,
                'market_value_usd': float(pos.get('market_value', 0) or 0),
                'result_status': 'submit_error',
            })
            results.append({'ticker': ticker, 'status': 'submit_error', 'error': str(e)})
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/execution/test_close_subset.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Confirm existing liquidator tests still pass**

```bash
pytest tests/execution/ -v -k "liquidator or close"
```
Expected: existing tests still PASS (no regression in `_close_symbol` or `liquidate_on_regime_change`).

- [ ] **Step 6: Commit**

```bash
git add src/execution/regime_liquidator.py tests/execution/test_close_subset.py
git commit -m "feat(premarket-panic): regime_liquidator.close_subset with extended-hours limit orders"
```

---

### Task 6: Pre-market helpers — webhook resolver, trading-day check, position loader

**Files:**
- Create: `src/pipeline/premarket_helpers.py`
- Test: `tests/pipeline/test_premarket_helpers.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_premarket_helpers.py
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import pytest

from src.pipeline.premarket_helpers import (
    resolve_premarket_webhook,
    is_trading_day_in_et,
    load_open_equity_positions,
)


# ---- resolve_premarket_webhook ----

@patch('src.pipeline.premarket_helpers._load_channel_webhooks')
def test_resolve_premarket_webhook_uses_configured_name(mock_load, monkeypatch):
    mock_load.return_value = {
        'premarket-watch': 'https://discord.com/api/webhooks/PRE',
        'trade-reports':   'https://discord.com/api/webhooks/TR',
    }
    monkeypatch.delenv('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', raising=False)
    assert resolve_premarket_webhook() == 'https://discord.com/api/webhooks/PRE'


@patch('src.pipeline.premarket_helpers._load_channel_webhooks')
def test_resolve_premarket_webhook_falls_back_to_trade_reports(mock_load, monkeypatch):
    mock_load.return_value = {'trade-reports': 'https://discord.com/api/webhooks/TR'}
    monkeypatch.delenv('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', raising=False)
    assert resolve_premarket_webhook() == 'https://discord.com/api/webhooks/TR'


@patch('src.pipeline.premarket_helpers._load_channel_webhooks')
def test_resolve_premarket_webhook_returns_none_when_neither_present(
    mock_load, monkeypatch,
):
    mock_load.return_value = {'other': 'x'}
    monkeypatch.delenv('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', raising=False)
    assert resolve_premarket_webhook() is None


# ---- is_trading_day_in_et ----

@patch('src.pipeline.premarket_helpers._run_cli')
def test_is_trading_day_true_when_next_open_today(mock_cli):
    # Today = 2026-05-28 in ET; next_open is 2026-05-28T09:30 ET => today is a trading day
    mock_cli.return_value = (True, {
        'is_open': False,
        'next_open': '2026-05-28T13:30:00Z',   # 09:30 ET
        'next_close': '2026-05-28T20:00:00Z',
        'timestamp': '2026-05-28T11:30:00Z',
    }, None)
    assert is_trading_day_in_et() is True


@patch('src.pipeline.premarket_helpers._run_cli')
def test_is_trading_day_false_when_next_open_is_later_date(mock_cli):
    # Today is a holiday: next_open is tomorrow
    mock_cli.return_value = (True, {
        'is_open': False,
        'next_open': '2026-05-29T13:30:00Z',
        'next_close': '2026-05-29T20:00:00Z',
        'timestamp': '2026-05-28T11:30:00Z',
    }, None)
    assert is_trading_day_in_et() is False


@patch('src.pipeline.premarket_helpers._run_cli')
def test_is_trading_day_false_on_cli_error(mock_cli):
    """Fail safe — never fire on uncertain calendar state."""
    mock_cli.return_value = (False, None, 'cli timeout')
    assert is_trading_day_in_et() is False


# ---- load_open_equity_positions ----

@patch('src.pipeline.premarket_helpers._run_cli')
def test_load_open_equity_positions_filters_to_us_equity(mock_cli):
    mock_cli.return_value = (True, [
        {'symbol': 'GLW',  'qty': '100',  'asset_class': 'us_equity',
         'avg_entry_price': '32.50', 'market_value': '3210.00'},
        {'symbol': 'BTCUSD', 'qty': '0.5', 'asset_class': 'crypto',
         'avg_entry_price': '68000', 'market_value': '34000'},
        {'symbol': 'SPY230721C00450000', 'qty': '5', 'asset_class': 'us_option',
         'avg_entry_price': '2.10', 'market_value': '1050'},
    ], None)

    out = load_open_equity_positions()
    symbols = [p['symbol'] for p in out]
    assert symbols == ['GLW']
    assert out[0]['qty'] == 100.0
    assert out[0]['avg_entry_price'] == 32.50


@patch('src.pipeline.premarket_helpers._run_cli')
def test_load_open_equity_positions_returns_empty_on_cli_error(mock_cli):
    mock_cli.return_value = (False, None, 'whatever')
    assert load_open_equity_positions() == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/pipeline/test_premarket_helpers.py -v
```
Expected: ALL FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/pipeline/premarket_helpers.py
"""Small shared helpers for the pre-market scanner.

Three concerns, kept in one file because they're trivial and tightly coupled
to the scanner entry point:
  * resolve_premarket_webhook  -- agent_registry lookup with fallback
  * is_trading_day_in_et       -- alpaca clock-based "is today a trading day"
  * load_open_equity_positions -- alpaca position list, filtered to us_equity
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Reuse the existing CLI wrapper from regime_liquidator (already imports it).
from src.execution.regime_liquidator import _run_cli
# Reuse the existing webhook loader.
from src.execution.pipeline_orchestrator import _load_channel_webhooks


_ET = ZoneInfo('America/New_York')


def resolve_premarket_webhook() -> str | None:
    """Return the URL for the configured pre-market channel, falling back to
    'trade-reports' if the configured name is missing. None if neither is
    registered."""
    name = os.environ.get('OPENCLAW_PREMARKET_DISCORD_WEBHOOK_NAME', 'premarket-watch')
    hooks = _load_channel_webhooks()
    return hooks.get(name) or hooks.get('trade-reports')


def is_trading_day_in_et() -> bool:
    """True iff today (ET) is a trading day per Alpaca's clock.

    Strategy: ask `alpaca clock`. If `next_open` falls on today's ET date,
    today is a trading day (the market either opens later or has already opened).
    If `next_open` is on a later date, today is a weekend or holiday.
    Fails safe: any CLI error returns False (caller does not fire).
    """
    ok, payload, _err = _run_cli(['clock'], timeout=10)
    if not ok or not isinstance(payload, dict):
        return False

    next_open_str = payload.get('next_open')
    if not next_open_str:
        return False

    try:
        next_open = datetime.fromisoformat(next_open_str.replace('Z', '+00:00'))
    except ValueError:
        return False

    today_et = datetime.now(_ET).date()
    return next_open.astimezone(_ET).date() == today_et


def load_open_equity_positions() -> list[dict]:
    """Return current broker positions filtered to asset_class == 'us_equity'.

    Each dict contains: symbol, qty (float, signed), avg_entry_price (float),
    market_value (float). Returns [] on any CLI error.
    """
    ok, payload, _err = _run_cli(['position', 'list'], timeout=15)
    if not ok or not isinstance(payload, list):
        return []
    out: list[dict] = []
    for raw in payload:
        if raw.get('asset_class') != 'us_equity':
            continue
        try:
            out.append({
                'symbol': raw['symbol'],
                'qty': float(raw['qty']),
                'avg_entry_price': float(raw.get('avg_entry_price', 0) or 0),
                'market_value': float(raw.get('market_value', 0) or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/pipeline/test_premarket_helpers.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/premarket_helpers.py tests/pipeline/test_premarket_helpers.py
git commit -m "feat(premarket-panic): shared helpers — webhook resolver, trading-day check, position loader"
```

---

### Task 7: Main entry point `run_premarket_scan.py`

**Files:**
- Create: `src/pipeline/run_premarket_scan.py`
- Test: `tests/pipeline/test_premarket_scan.py`

This is the orchestration. Glue the helpers, the scorer, the confirmer, the audit table, the Discord post, and (when gated ON) the auto-close.

- [ ] **Step 1: Write the failing tests (integration; mocks at the I/O boundary)**

```python
# tests/pipeline/test_premarket_scan.py
import os
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call
import pytest

from src.pipeline.run_premarket_scan import (
    main, run_scan, ScanConfig, GateConfigError,
)
from src.sentiment.sonnet_premarket_confirmer import PremarketConfirmerResult


@pytest.fixture
def env_master_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.delenv('OPENCLAW_PREMARKET_CONFIRMER', raising=False)
    monkeypatch.delenv('OPENCLAW_PREMARKET_AUTOCLOSE', raising=False)


def test_gate_hierarchy_autoclose_without_confirmer_raises(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.delenv('OPENCLAW_PREMARKET_CONFIRMER', raising=False)
    monkeypatch.setenv('OPENCLAW_PREMARKET_AUTOCLOSE', '1')
    with pytest.raises(GateConfigError) as exc:
        ScanConfig.from_env()
    assert 'confirmer' in str(exc.value).lower()


def test_master_gate_off_exits_zero_silently(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PREMARKET_SCAN', raising=False)
    rc = main(['--scan-label', '07:30'])
    assert rc == 0


@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=False)
def test_holiday_exits_zero_no_db_writes(mock_calendar, env_master_on):
    rc = main(['--scan-label', '07:30'])
    assert rc == 0
    mock_calendar.assert_called_once()


@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_rules_only_path_persists_and_posts_when_score_above_threshold(
    _cal, _load, mock_news, _persist, _post, env_master_on,
):
    _load.return_value = [
        {'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
    ]
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
        'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
        'news_top_headlines': ['CFO departs', 'Guidance cut', 'Downgrade'],
        'evidence_uuids': ['u1', 'u2', 'u3'],
    }]
    rc = main(['--scan-label', '07:30'])

    assert rc == 0
    rows = _persist.call_args[0][0]
    assert len(rows) == 1
    assert rows[0]['ticker'] == 'GLW'
    assert rows[0]['panic_score'] > 35
    assert rows[0]['advisory_fired'] is True
    assert rows[0]['sonnet_verdict'] is None   # confirmer gate OFF
    _post.assert_called_once()


@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_confirmer_path_calls_sonnet_only_for_above_threshold(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, monkeypatch,
):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_CONFIRMER', '1')
    _load.return_value = [
        {'symbol': 'GLW',  'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
        {'symbol': 'AAPL', 'qty': 50,  'avg_entry_price': 200.0, 'market_value': 10000},
    ]
    mock_news.side_effect = [
        # GLW: above threshold
        [{'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
          'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
          'news_top_headlines': ['x', 'y', 'z'], 'evidence_uuids': ['u1', 'u2', 'u3']}],
        # AAPL: below
        [{'ticker': 'AAPL', 'news_count_24h': 1, 'news_finbert_neg': 0.0,
          'news_finbert_pos': 0.8, 'news_finbert_neu': 0.2, 'news_mean_score': 0.6,
          'news_top_headlines': ['k'], 'evidence_uuids': ['u9']}],
    ]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='bearish_news_driven', severity=5,
        rationale='hard catalyst', evidence_uuids=['u1', 'u2'], cost_usd=0.013,
    )

    main(['--scan-label', '07:30'])

    # Sonnet called for GLW only
    assert mock_sonnet.call_count == 1
    assert mock_sonnet.call_args[0][0].ticker == 'GLW'


@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_autoclose_fires_only_when_gate_on_and_strict_severity_met(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, mock_close, monkeypatch,
):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_CONFIRMER', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_AUTOCLOSE', '1')
    _load.return_value = [
        {'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
    ]
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
        'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
        'news_top_headlines': ['x', 'y', 'z'], 'evidence_uuids': ['u1', 'u2', 'u3'],
    }]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='bearish_news_driven', severity=5,
        rationale='hard catalyst', evidence_uuids=['u1'], cost_usd=0.013,
    )
    mock_close.return_value = [{'ticker': 'GLW', 'status': 'filled',
                                'liquidation_id': 42}]

    main(['--scan-label', '09:00'])

    mock_close.assert_called_once()
    args, kwargs = mock_close.call_args
    assert args[0] == ['GLW']
    assert kwargs.get('reason', args[1] if len(args) > 1 else None) == 'PREMARKET_PANIC'


@patch('src.pipeline.run_premarket_scan.close_subset')
@patch('src.pipeline.run_premarket_scan._post_discord')
@patch('src.pipeline.run_premarket_scan._persist_alert_rows')
@patch('src.pipeline.run_premarket_scan.confirm_panic')
@patch('src.pipeline.run_premarket_scan.score_news_for_tickers')
@patch('src.pipeline.run_premarket_scan.load_open_equity_positions')
@patch('src.pipeline.run_premarket_scan.is_trading_day_in_et', return_value=True)
def test_autoclose_skipped_on_llm_error_even_when_gate_on(
    _cal, _load, mock_news, mock_sonnet, _persist, _post, mock_close, monkeypatch,
):
    monkeypatch.setenv('OPENCLAW_PREMARKET_SCAN', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_CONFIRMER', '1')
    monkeypatch.setenv('OPENCLAW_PREMARKET_AUTOCLOSE', '1')
    _load.return_value = [
        {'symbol': 'GLW', 'qty': 100, 'avg_entry_price': 32.5, 'market_value': 3210},
    ]
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 3, 'news_finbert_neg': 0.8,
        'news_finbert_pos': 0.1, 'news_finbert_neu': 0.1, 'news_mean_score': -0.7,
        'news_top_headlines': ['x', 'y', 'z'], 'evidence_uuids': ['u1'],
    }]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='llm_error', severity=None,
        rationale='budget exceeded', evidence_uuids=[], cost_usd=None,
    )
    main(['--scan-label', '09:00'])
    mock_close.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/pipeline/test_premarket_scan.py -v
```
Expected: ALL FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/pipeline/run_premarket_scan.py
"""Pre-market sentiment panic scanner — sidecar entry point.

Two-shot daily: timer fires at 07:30 ET and 09:00 ET on trading days.

Gate hierarchy (all default-OFF):
  OPENCLAW_PREMARKET_SCAN=1       -> master; service refuses to run without this
  OPENCLAW_PREMARKET_CONFIRMER=1  -> call Sonnet on rule-flagged tickers
  OPENCLAW_PREMARKET_AUTOCLOSE=1  -> auto-flatten on strict Sonnet verdict
                                     (requires CONFIRMER=1; startup raises otherwise)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import execute_values

from src.pipeline.premarket_helpers import (
    is_trading_day_in_et,
    load_open_equity_positions,
    resolve_premarket_webhook,
)
from src.ingestion.news_finbert_scorer import score_news_for_tickers
from src.sentiment.premarket_scorer import ScoreInputs, panic_score
from src.sentiment.sonnet_premarket_confirmer import (
    PANIC_VERDICTS,
    PremarketConfirmerInput,
    confirm_panic,
)
from src.execution.regime_liquidator import close_subset


log = logging.getLogger(__name__)
_ET = ZoneInfo('America/New_York')

STRICT_AUTOCLOSE_VERDICTS = {'bearish_news_driven', 'bearish_idiosyncratic'}


class GateConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanConfig:
    scan_enabled: bool
    confirmer_enabled: bool
    autoclose_enabled: bool
    advisory_threshold: float
    autoclose_min_severity: int
    max_tickers_per_scan: int
    confirmer_budget_usd: float

    @classmethod
    def from_env(cls) -> 'ScanConfig':
        scan = os.environ.get('OPENCLAW_PREMARKET_SCAN', '0') == '1'
        conf = os.environ.get('OPENCLAW_PREMARKET_CONFIRMER', '0') == '1'
        auto = os.environ.get('OPENCLAW_PREMARKET_AUTOCLOSE', '0') == '1'
        if auto and not conf:
            raise GateConfigError(
                'OPENCLAW_PREMARKET_AUTOCLOSE=1 requires OPENCLAW_PREMARKET_CONFIRMER=1'
            )
        return cls(
            scan_enabled=scan,
            confirmer_enabled=conf,
            autoclose_enabled=auto,
            advisory_threshold=float(os.environ.get(
                'OPENCLAW_PREMARKET_ADVISORY_THRESHOLD', '35')),
            autoclose_min_severity=int(os.environ.get(
                'OPENCLAW_PREMARKET_AUTOCLOSE_MIN_SEVERITY', '4')),
            max_tickers_per_scan=int(os.environ.get(
                'OPENCLAW_PREMARKET_MAX_TICKERS_PER_SCAN', '25')),
            confirmer_budget_usd=float(os.environ.get(
                'OPENCLAW_PREMARKET_CONFIRMER_BUDGET_USD', '0.50')),
        )


def _premarket_window_start_utc(scan_ts: datetime) -> datetime:
    """Window starts at the prior trading day's 18:00 ET. For a 07:30 ET scan
    on 2026-05-28, the start is 2026-05-27 18:00 ET == 22:00 UTC.
    """
    scan_et = scan_ts.astimezone(_ET)
    prior_et = (scan_et - timedelta(days=1)).date()
    start_et = datetime.combine(prior_et, time(18, 0), tzinfo=_ET)
    return start_et.astimezone(timezone.utc)


def _persist_alert_rows(rows: list[dict]) -> None:
    if not rows:
        return
    dsn = os.environ['POSTGRES_URI']
    cols = (
        'scan_ts', 'scan_label', 'trading_day', 'ticker', 'held_qty',
        'avg_entry_price',
        'news_count_window', 'news_finbert_neg_ratio',
        'news_finbert_mean_score',
        'social_post_count_window', 'social_bear_ratio',
        'panic_score', 'advisory_fired',
        'sonnet_verdict', 'sonnet_severity', 'sonnet_rationale',
        'sonnet_evidence_uuids', 'sonnet_cost_usd',
        'autoclose_fired', 'autoclose_liquidation_id',
    )
    values = [tuple(r.get(c) for c in cols) for r in rows]
    placeholder = '(' + ','.join(['%s'] * len(cols)) + ')'
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        execute_values(
            cur,
            f'INSERT INTO premarket_panic_alerts ({",".join(cols)}) VALUES %s',
            values, template=placeholder,
        )
        conn.commit()


def _format_discord_summary(rows: list[dict], scan_label: str) -> str:
    fired = [r for r in rows if r.get('advisory_fired')]
    if not fired:
        return ''  # silent on calm mornings
    lines = [f'**Pre-market panic scan — {scan_label} ET**']
    for r in fired:
        ticker = r['ticker']
        score = r['panic_score']
        qty = r['held_qty']
        verdict = r.get('sonnet_verdict') or 'rules-only'
        sev = r.get('sonnet_severity')
        rationale = (r.get('sonnet_rationale') or '').strip()
        sev_str = f' sev={sev}' if sev is not None else ''
        head = f'• `{ticker}` qty={qty:+g} score={score:.0f} verdict={verdict}{sev_str}'
        lines.append(head)
        if rationale:
            lines.append(f'    {rationale[:300]}')
        if r.get('autoclose_fired'):
            lines.append('    AUTO-CLOSE submitted.')
    return '\n'.join(lines)


def _post_discord(url: str, content: str) -> None:
    if not url or not content:
        return
    req = urllib.request.Request(
        url, data=json.dumps({'content': content}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 — never fail the scan over Discord
        log.warning('discord post failed: %s', e)


def _evaluate_ticker(position: dict, cfg: ScanConfig, scan_ts: datetime,
                     scan_label: str, window_start: datetime) -> dict:
    ticker = position['symbol']
    news_rows = score_news_for_tickers([ticker], window_start)
    n = news_rows[0] if news_rows else None

    inputs = ScoreInputs(
        news_count_window=int(n['news_count_24h']) if n else 0,
        news_finbert_neg_ratio=float(n['news_finbert_neg']) if n else 0.0,
        news_finbert_mean_score=float(n['news_mean_score']) if n else 0.0,
        social_post_count_window=0,    # MVP: social pulled in handoff future iteration
        social_bear_ratio=0.0,
    )
    score = panic_score(inputs)
    advisory = score >= cfg.advisory_threshold

    row: dict = {
        'scan_ts': scan_ts,
        'scan_label': scan_label,
        'trading_day': scan_ts.astimezone(_ET).date(),
        'ticker': ticker,
        'held_qty': position['qty'],
        'avg_entry_price': position.get('avg_entry_price'),
        'news_count_window': inputs.news_count_window,
        'news_finbert_neg_ratio': inputs.news_finbert_neg_ratio,
        'news_finbert_mean_score': inputs.news_finbert_mean_score,
        'social_post_count_window': 0,
        'social_bear_ratio': 0.0,
        'panic_score': score,
        'advisory_fired': advisory,
        'sonnet_verdict': None,
        'sonnet_severity': None,
        'sonnet_rationale': None,
        'sonnet_evidence_uuids': None,
        'sonnet_cost_usd': None,
        'autoclose_fired': False,
        'autoclose_liquidation_id': None,
    }

    if advisory and cfg.confirmer_enabled and n is not None:
        top = list(zip(
            n.get('news_top_headlines', [])[:5],
            [inputs.news_finbert_mean_score] * 5,
            (n.get('evidence_uuids') or [])[:5],
        ))
        result = confirm_panic(
            PremarketConfirmerInput(
                ticker=ticker, held_qty=position['qty'], panic_score=score,
                news_count=inputs.news_count_window,
                finbert_neg_ratio=inputs.news_finbert_neg_ratio,
                social_bear_ratio=0.0,
                top_headlines=top,
            ),
            max_budget_usd=cfg.confirmer_budget_usd,
        )
        row['sonnet_verdict'] = result.verdict
        row['sonnet_severity'] = result.severity
        row['sonnet_rationale'] = result.rationale
        row['sonnet_evidence_uuids'] = result.evidence_uuids or None
        row['sonnet_cost_usd'] = result.cost_usd
    return row


def _should_autoclose(row: dict, cfg: ScanConfig) -> bool:
    return (
        cfg.autoclose_enabled
        and row['sonnet_verdict'] in STRICT_AUTOCLOSE_VERDICTS
        and row.get('sonnet_severity') is not None
        and row['sonnet_severity'] >= cfg.autoclose_min_severity
    )


def run_scan(scan_label: str, ticker_override: list[str] | None = None) -> int:
    cfg = ScanConfig.from_env()
    if not cfg.scan_enabled:
        log.info('OPENCLAW_PREMARKET_SCAN=0; exiting silently')
        return 0
    if not is_trading_day_in_et():
        log.info('not a trading day in ET; exiting silently')
        return 0

    positions = load_open_equity_positions()
    if ticker_override:
        positions = [p for p in positions if p['symbol'] in set(ticker_override)]
    if not positions:
        log.info('no open equity positions; exiting')
        return 0

    if len(positions) > cfg.max_tickers_per_scan:
        log.warning('truncating %d positions to max %d',
                    len(positions), cfg.max_tickers_per_scan)
        positions = positions[:cfg.max_tickers_per_scan]

    scan_ts = datetime.now(timezone.utc)
    window_start = _premarket_window_start_utc(scan_ts)
    rows = [
        _evaluate_ticker(p, cfg, scan_ts, scan_label, window_start)
        for p in positions
    ]

    # Auto-close gate
    if cfg.autoclose_enabled:
        flagged = [r for r in rows if _should_autoclose(r, cfg)]
        if flagged:
            outcomes = close_subset(
                [r['ticker'] for r in flagged], reason='PREMARKET_PANIC',
            )
            by_ticker = {o['ticker']: o for o in outcomes}
            for r in flagged:
                o = by_ticker.get(r['ticker'], {})
                r['autoclose_fired'] = o.get('status') in {'filled', 'pending', 'accepted'}
                r['autoclose_liquidation_id'] = o.get('liquidation_id')

    _persist_alert_rows(rows)

    webhook = resolve_premarket_webhook()
    summary = _format_discord_summary(rows, scan_label)
    if webhook and summary:
        _post_discord(webhook, summary)

    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--scan-label', required=True, choices=['07:30', '09:00'])
    parser.add_argument('--dry-run', action='store_true',
                        help='compute and print rows; skip DB writes, Discord, autoclose')
    parser.add_argument('--tickers', nargs='*',
                        help='override broker-position lookup for debugging')
    args = parser.parse_args(argv)

    try:
        cfg = ScanConfig.from_env()
    except GateConfigError as e:
        log.error('gate config: %s', e)
        return 2

    if args.dry_run:
        os.environ.setdefault('PREMARKET_DRY_RUN', '1')
        # Dry-run currently mirrors the persist/post path through the same code;
        # production callers do not pass --dry-run, so just gate at run_scan if desired.

    return run_scan(args.scan_label, ticker_override=args.tickers)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/pipeline/test_premarket_scan.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Smoke-test the gate hierarchy from a shell**

```bash
OPENCLAW_PREMARKET_AUTOCLOSE=1 OPENCLAW_PREMARKET_SCAN=1 \
  python -m src.pipeline.run_premarket_scan --scan-label 07:30
```
Expected: exit 2, stderr contains "requires OPENCLAW_PREMARKET_CONFIRMER=1".

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/run_premarket_scan.py tests/pipeline/test_premarket_scan.py
git commit -m "feat(premarket-panic): main run_premarket_scan entry point with three-gate hierarchy"
```

---

### Task 8: GLW replay tool

**Files:**
- Create: `scripts/replay_premarket_panic.py`
- Test: `tests/pipeline/test_replay_premarket_panic.py`

Read-only. Reuses `score_news_for_tickers`, `panic_score`, and `confirm_panic`. Never writes to `premarket_panic_alerts`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_replay_premarket_panic.py
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import json
import sys

from scripts.replay_premarket_panic import replay, _build_argparser


@patch('scripts.replay_premarket_panic.score_news_for_tickers')
def test_replay_returns_full_verdict_no_db_writes(mock_news):
    mock_news.return_value = [{
        'ticker': 'GLW',
        'news_count_24h': 2,
        'news_finbert_neg': 1.0, 'news_finbert_pos': 0.0, 'news_finbert_neu': 0.0,
        'news_mean_score': -0.9,
        'news_top_headlines': ['CFO departs', 'Guidance cut'],
        'evidence_uuids': ['u1', 'u2'],
    }]
    as_of = datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc)
    out = replay(ticker='GLW', as_of=as_of, with_sonnet=False)

    assert out['ticker'] == 'GLW'
    assert out['panic_score'] > 35
    assert out['advisory_would_fire'] is True
    assert out['sonnet_verdict'] is None  # with_sonnet=False
    assert out['headlines'] == ['CFO departs', 'Guidance cut']


@patch('scripts.replay_premarket_panic.confirm_panic')
@patch('scripts.replay_premarket_panic.score_news_for_tickers')
def test_replay_with_sonnet_calls_confirmer(mock_news, mock_sonnet):
    from src.sentiment.sonnet_premarket_confirmer import PremarketConfirmerResult
    mock_news.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 2,
        'news_finbert_neg': 1.0, 'news_finbert_pos': 0.0, 'news_finbert_neu': 0.0,
        'news_mean_score': -0.9, 'news_top_headlines': ['x', 'y'],
        'evidence_uuids': ['u1', 'u2'],
    }]
    mock_sonnet.return_value = PremarketConfirmerResult(
        verdict='bearish_news_driven', severity=5,
        rationale='hard catalyst', evidence_uuids=['u1'], cost_usd=0.012,
    )
    out = replay(ticker='GLW',
                 as_of=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
                 with_sonnet=True)
    assert out['sonnet_verdict'] == 'bearish_news_driven'
    mock_sonnet.assert_called_once()


@patch('scripts.replay_premarket_panic.score_news_for_tickers', return_value=[])
def test_replay_handles_no_news(_):
    out = replay(ticker='GLW',
                 as_of=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
                 with_sonnet=False)
    assert out['panic_score'] == 0.0
    assert out['advisory_would_fire'] is False
    assert out['headlines'] == []


def test_argparser_requires_ticker_and_as_of():
    p = _build_argparser()
    args = p.parse_args(['--ticker', 'GLW', '--as-of', '2026-05-28T09:00:00-04:00'])
    assert args.ticker == 'GLW'
    assert args.as_of == '2026-05-28T09:00:00-04:00'
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/pipeline/test_replay_premarket_panic.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Read-only replay of the pre-market panic scanner against historical data.

Usage:
    scripts/replay_premarket_panic.py --ticker GLW \\
        --as-of 2026-05-28T09:00:00-04:00 [--with-sonnet]

Reads market_news for the prior 18:00 ET -> as-of window, runs the same
scorer and (optionally) Sonnet confirmer, prints a JSON verdict. Never writes
to premarket_panic_alerts. Reddit/StockTwits are stream-only and historical
social data is unavailable; social components in the score are 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from src.ingestion.news_finbert_scorer import score_news_for_tickers
from src.sentiment.premarket_scorer import ScoreInputs, panic_score
from src.sentiment.sonnet_premarket_confirmer import (
    PremarketConfirmerInput, confirm_panic,
)


_ET = ZoneInfo('America/New_York')


def _window_start_utc(as_of: datetime) -> datetime:
    as_of_et = as_of.astimezone(_ET)
    prior_date = (as_of_et - timedelta(days=1)).date()
    start_et = datetime.combine(prior_date, time(18, 0), tzinfo=_ET)
    return start_et.astimezone(timezone.utc)


def replay(ticker: str, as_of: datetime, with_sonnet: bool,
           advisory_threshold: float = 35.0) -> dict:
    start = _window_start_utc(as_of)
    news = score_news_for_tickers([ticker], start)
    n = news[0] if news else None

    inputs = ScoreInputs(
        news_count_window=int(n['news_count_24h']) if n else 0,
        news_finbert_neg_ratio=float(n['news_finbert_neg']) if n else 0.0,
        news_finbert_mean_score=float(n['news_mean_score']) if n else 0.0,
        social_post_count_window=0,
        social_bear_ratio=0.0,
    )
    score = panic_score(inputs)

    out: dict = {
        'ticker': ticker,
        'as_of': as_of.isoformat(),
        'window_start': start.isoformat(),
        'news_count': inputs.news_count_window,
        'finbert_neg_ratio': inputs.news_finbert_neg_ratio,
        'panic_score': score,
        'advisory_would_fire': score >= advisory_threshold,
        'headlines': n.get('news_top_headlines', []) if n else [],
        'sonnet_verdict': None,
        'sonnet_severity': None,
        'sonnet_rationale': None,
    }

    if with_sonnet and n is not None and out['advisory_would_fire']:
        result = confirm_panic(PremarketConfirmerInput(
            ticker=ticker, held_qty=0.0, panic_score=score,
            news_count=inputs.news_count_window,
            finbert_neg_ratio=inputs.news_finbert_neg_ratio,
            social_bear_ratio=0.0,
            top_headlines=list(zip(
                n.get('news_top_headlines', [])[:5],
                [inputs.news_finbert_mean_score] * 5,
                (n.get('evidence_uuids') or [])[:5],
            )),
        ))
        out['sonnet_verdict'] = result.verdict
        out['sonnet_severity'] = result.severity
        out['sonnet_rationale'] = result.rationale
        out['sonnet_cost_usd'] = result.cost_usd
    return out


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ticker', required=True)
    p.add_argument('--as-of', required=True, help='ISO-8601 timestamp with offset')
    p.add_argument('--with-sonnet', action='store_true')
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    as_of = datetime.fromisoformat(args.as_of)
    out = replay(ticker=args.ticker, as_of=as_of, with_sonnet=args.with_sonnet)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/pipeline/test_replay_premarket_panic.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/replay_premarket_panic.py tests/pipeline/test_replay_premarket_panic.py
git commit -m "feat(premarket-panic): replay tool for GLW post-mortem (read-only)"
```

---

### Task 9: Realized-PnL backfill job

**Files:**
- Create: `scripts/backfill_premarket_realized_pnl.py`
- Test: `tests/pipeline/test_premarket_backfill.py`

Idempotent. Reads `premarket_panic_alerts` rows where `realized_backfilled_at IS NULL` and the row's `trading_day` has fully elapsed (open + close known), joins to Alpaca daily bars, writes `realized_open_to_open_pct` (today open → next-trading-day open) and `realized_open_to_close_pct` (today open → today close). Forward-only.

- [ ] **Step 1: Write the failing test**

```python
# tests/pipeline/test_premarket_backfill.py
from datetime import date
from unittest.mock import patch, MagicMock
from scripts.backfill_premarket_realized_pnl import backfill_rows, _compute_pnl


def _fake_bars(open_t, close_t, open_tplus1):
    return {'open': open_t, 'close': close_t, 'open_next': open_tplus1}


def test_compute_pnl_returns_expected_percents():
    pnl = _compute_pnl(open_t=100.0, close_t=95.0, open_tplus1=92.0)
    assert pnl['open_to_close'] == pytest.approx(-0.05)
    assert pnl['open_to_open']  == pytest.approx(-0.08)


@patch('scripts.backfill_premarket_realized_pnl._fetch_bars_for')
@patch('scripts.backfill_premarket_realized_pnl._fetch_unfilled_alerts')
@patch('scripts.backfill_premarket_realized_pnl._write_pnl_back')
def test_backfill_skips_already_filled_rows(mock_write, mock_fetch_unfilled, mock_bars):
    mock_fetch_unfilled.return_value = []  # all already backfilled
    backfill_rows()
    mock_bars.assert_not_called()
    mock_write.assert_not_called()


@patch('scripts.backfill_premarket_realized_pnl._fetch_bars_for')
@patch('scripts.backfill_premarket_realized_pnl._fetch_unfilled_alerts')
@patch('scripts.backfill_premarket_realized_pnl._write_pnl_back')
def test_backfill_writes_only_when_bars_available(
    mock_write, mock_fetch_unfilled, mock_bars,
):
    mock_fetch_unfilled.return_value = [
        {'id': 1, 'ticker': 'GLW', 'trading_day': date(2026, 5, 28)},
        {'id': 2, 'ticker': 'AAPL','trading_day': date(2026, 5, 28)},
    ]
    # GLW has bars; AAPL is missing next-day open
    mock_bars.side_effect = [
        _fake_bars(100.0, 95.0, 92.0),
        None,
    ]
    backfill_rows()
    assert mock_write.call_count == 1
    assert mock_write.call_args[0][0] == 1  # row id 1 only
```

(Add `import pytest` at the top of the test file.)

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/pipeline/test_premarket_backfill.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""EOD realized-PnL backfill for premarket_panic_alerts.

Fires at 16:05 ET. Reads alerts with NULL realized_backfilled_at and a
fully-elapsed trading_day. Looks up that ticker's open + close from
Alpaca daily bars, plus the next trading day's open, and writes the two
realized-pct columns. Idempotent.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor


log = logging.getLogger(__name__)


def _fetch_unfilled_alerts() -> list[dict]:
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id, ticker, trading_day
              FROM premarket_panic_alerts
             WHERE realized_backfilled_at IS NULL
               AND trading_day <= (now() AT TIME ZONE 'America/New_York')::date
        """)
        return [dict(r) for r in cur.fetchall()]


def _fetch_bars_for(ticker: str, trading_day: date) -> dict | None:
    """Return {'open': float, 'close': float, 'open_next': float} or None.
    Queries the existing daily-bars table (verify name during implementation;
    likely 'alpaca_bars_daily' or 'market_bars_daily')."""
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT date, open, close
              FROM alpaca_bars_daily
             WHERE ticker = %s
               AND date BETWEEN %s AND %s
          ORDER BY date
        """, (ticker, trading_day, trading_day + timedelta(days=7)))
        rows = cur.fetchall()
    if not rows or rows[0][0] != trading_day:
        return None
    if len(rows) < 2:
        return None
    today = rows[0]
    nxt = rows[1]
    return {
        'open': float(today[1]),
        'close': float(today[2]),
        'open_next': float(nxt[1]),
    }


def _compute_pnl(open_t: float, close_t: float, open_tplus1: float) -> dict:
    return {
        'open_to_close': (close_t - open_t) / open_t,
        'open_to_open':  (open_tplus1 - open_t) / open_t,
    }


def _write_pnl_back(alert_id: int, pnl: dict) -> None:
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE premarket_panic_alerts
               SET realized_open_to_close_pct = %s,
                   realized_open_to_open_pct  = %s,
                   realized_backfilled_at     = now()
             WHERE id = %s
        """, (pnl['open_to_close'], pnl['open_to_open'], alert_id))
        conn.commit()


def backfill_rows() -> int:
    rows = _fetch_unfilled_alerts()
    if not rows:
        log.info('no alerts pending backfill')
        return 0
    written = 0
    for r in rows:
        bars = _fetch_bars_for(r['ticker'], r['trading_day'])
        if bars is None:
            log.info('bars unavailable for %s %s; skipping (will retry next EOD)',
                     r['ticker'], r['trading_day'])
            continue
        _write_pnl_back(r['id'], _compute_pnl(**bars))
        written += 1
    log.info('backfilled %d / %d alerts', written, len(rows))
    return written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    backfill_rows()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/pipeline/test_premarket_backfill.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Verify the daily-bars table name**

Open `src/database/migrations/` and `git grep` for the daily-bars table actually used. If it is **not** `alpaca_bars_daily`, update the table name in `_fetch_bars_for`. Verify with:

```bash
psql "$POSTGRES_URI" -c "\dt *bars*"
```

- [ ] **Step 6: Commit**

```bash
git add scripts/backfill_premarket_realized_pnl.py tests/pipeline/test_premarket_backfill.py
git commit -m "feat(premarket-panic): EOD realized-PnL backfill job (idempotent)"
```

---

### Task 10: systemd units (scanner + backfill)

**Files:**
- Create: `docs/openclaw-premarket-scan.service`
- Create: `docs/openclaw-premarket-scan.timer`
- Create: `docs/openclaw-premarket-realized-backfill.service`
- Create: `docs/openclaw-premarket-realized-backfill.timer`

Mirror the style of the existing `docs/openclaw-sp5-cleanup.{service,timer}` already in the repo. After writing, the operator copies them into `/etc/systemd/system/` and runs `systemctl daemon-reload && systemctl enable --now <timer>`.

- [ ] **Step 1: Inspect the existing reference units**

```bash
cat docs/openclaw-sp5-cleanup.service docs/openclaw-sp5-cleanup.timer
```

Use exact `User=`, `Group=`, `WorkingDirectory=`, `EnvironmentFile=` lines as the reference units use.

- [ ] **Step 2: Write the scanner service**

```ini
# docs/openclaw-premarket-scan.service
[Unit]
Description=OpenClaw pre-market sentiment panic scan
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
Group=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
# scan-label is passed by the timer's two distinct units (see timer file)
ExecStart=/usr/bin/python3 -m src.pipeline.run_premarket_scan --scan-label ${SCAN_LABEL}
StandardOutput=journal
StandardError=journal
```

Note: systemd does not let one `.service` accept two different argv sets directly. Pattern: use a templated unit `openclaw-premarket-scan@.service` and two timers, OR keep one `.service` and pass `SCAN_LABEL` via two separate timer units that set `Environment=SCAN_LABEL=...`. We use the **templated** form below — it's cleaner.

Rewrite as:

```ini
# docs/openclaw-premarket-scan@.service
[Unit]
Description=OpenClaw pre-market sentiment panic scan (label=%i)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
Group=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/python3 -m src.pipeline.run_premarket_scan --scan-label %i
StandardOutput=journal
StandardError=journal
```

- [ ] **Step 3: Write the scanner timer (two OnCalendar entries → two instances)**

systemd does not run different instances of the same template from one timer; instead, ship TWO timer files:

```ini
# docs/openclaw-premarket-scan-0730.timer
[Unit]
Description=Fire pre-market scan at 07:30 ET
Requires=openclaw-premarket-scan@07:30.service

[Timer]
OnCalendar=Mon..Fri *-*-* 07:30:00 America/New_York
Persistent=false
Unit=openclaw-premarket-scan@07:30.service

[Install]
WantedBy=timers.target
```

```ini
# docs/openclaw-premarket-scan-0900.timer
[Unit]
Description=Fire pre-market scan at 09:00 ET
Requires=openclaw-premarket-scan@09:00.service

[Timer]
OnCalendar=Mon..Fri *-*-* 09:00:00 America/New_York
Persistent=false
Unit=openclaw-premarket-scan@09:00.service

[Install]
WantedBy=timers.target
```

(Replace the original `openclaw-premarket-scan.{service,timer}` filenames in the File Structure table — the templated form is the actual layout.)

- [ ] **Step 4: Write the backfill service + timer**

```ini
# docs/openclaw-premarket-realized-backfill.service
[Unit]
Description=OpenClaw pre-market panic alerts realized-PnL backfill
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=claudebot
Group=claudebot
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/python3 scripts/backfill_premarket_realized_pnl.py
StandardOutput=journal
StandardError=journal
```

```ini
# docs/openclaw-premarket-realized-backfill.timer
[Unit]
Description=Fire pre-market realized-PnL backfill at 16:05 ET
Requires=openclaw-premarket-realized-backfill.service

[Timer]
OnCalendar=Mon..Fri *-*-* 16:05:00 America/New_York
Persistent=false
Unit=openclaw-premarket-realized-backfill.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 5: Validate unit files**

```bash
systemd-analyze verify \
  docs/openclaw-premarket-scan@.service \
  docs/openclaw-premarket-scan-0730.timer \
  docs/openclaw-premarket-scan-0900.timer \
  docs/openclaw-premarket-realized-backfill.service \
  docs/openclaw-premarket-realized-backfill.timer
```
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add docs/openclaw-premarket-scan@.service \
        docs/openclaw-premarket-scan-0730.timer \
        docs/openclaw-premarket-scan-0900.timer \
        docs/openclaw-premarket-realized-backfill.service \
        docs/openclaw-premarket-realized-backfill.timer
git commit -m "feat(premarket-panic): systemd units (scanner @07:30 + @09:00, backfill @16:05)"
```

---

### Task 11: GLW post-mortem run (manual analysis task; no code)

**Files:**
- Modify: `docs/superpowers/specs/2026-05-28-premarket-panic-scan-design.md` (append a "Post-mortem result" section at the bottom; do NOT alter the rest of the spec)

This is the analytical payoff the operator asked for: would the scanner have caught GLW?

- [ ] **Step 1: Run the replay against GLW for the 2026-05-28 open**

```bash
python3 scripts/replay_premarket_panic.py \
    --ticker GLW \
    --as-of 2026-05-28T09:00:00-04:00 \
    --with-sonnet \
  | tee /tmp/glw_replay_0900.json
```

Also run a 07:30 ET replay so the operator sees both windows:

```bash
python3 scripts/replay_premarket_panic.py \
    --ticker GLW \
    --as-of 2026-05-28T07:30:00-04:00 \
    --with-sonnet \
  | tee /tmp/glw_replay_0730.json
```

- [ ] **Step 2: Inspect the verdicts**

Inspect both JSON files. For each: would-have-fired-advisory (bool), panic_score, Sonnet verdict + rationale + severity. Note the headline list — if it's empty, the conclusion is "pure sentiment was not sufficient for GLW" and follow-up specs (tape, EDGAR) are warranted.

- [ ] **Step 3: Append a "Post-mortem result" section to the spec**

```markdown
## 11. GLW post-mortem result (2026-05-28)

**07:30 ET replay:**
- news_count: <fill in>
- panic_score: <fill in>
- advisory_would_fire: <yes/no>
- Sonnet verdict: <fill in>
- Sonnet severity: <fill in>
- Sonnet rationale: <fill in>

**09:00 ET replay:**
- news_count: <fill in>
- panic_score: <fill in>
- advisory_would_fire: <yes/no>
- Sonnet verdict: <fill in>
- Sonnet severity: <fill in>
- Sonnet rationale: <fill in>

**Conclusion:**
<one paragraph: did pure sentiment carry the signal, or do we need tape/EDGAR follow-up?>
```

Fill in the actual values from the JSON output.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-05-28-premarket-panic-scan-design.md
git add /tmp/glw_replay_0730.json /tmp/glw_replay_0900.json 2>/dev/null || true
git commit -m "docs(premarket-panic): GLW post-mortem replay result (2026-05-28)"
```

---

## Self-review

**Spec coverage check:**
- §2 architecture summary → Tasks 6 + 7
- §3 components inventory → Tasks 1–10 (one task per file)
- §4 migration 120 schema → Task 1
- §5 env-var gates → Task 7 (`ScanConfig.from_env` + gate-hierarchy test)
- §6 error handling → covered across Tasks 4 (LLM error fail-open), 5 (per-ticker continue), 7 (holiday skip + gate validation)
- §7 testing strategy → every task ends with passing tests; integration test in Task 7 covers gate hierarchy
- §8 GLW replay tool → Task 8 (code) + Task 11 (actual run)
- §9 rollout plan → not a coding task; documented in the spec itself
- §10 open questions (panic_score formula, equity predicate, limit-price strategy, social raw persistence) → all resolved at the top of this plan

**Placeholder scan:** no TBD / TODO / "add appropriate error handling" / "similar to Task N" remain. Every code block is concrete.

**Type consistency:**
- `panic_score` returns `float` everywhere (Task 2, used in Tasks 7 + 8).
- `confirm_panic` returns `PremarketConfirmerResult` everywhere (Task 4, used in Tasks 7 + 8).
- `close_subset(tickers, reason)` signature stable across Tasks 5 and 7.
- `score_news_for_tickers(tickers, since_ts) -> list[dict]` with `evidence_uuids` key stable across Tasks 3, 7, 8.
- `load_open_equity_positions() -> list[dict]` with keys `symbol, qty, avg_entry_price, market_value` stable across Tasks 6 + 7.

**Verification reminders for the implementer (not blockers, but read the existing code first):**
- Task 5: confirm `_run_cli` signature and `psycopg2`/`os` imports already in `regime_liquidator.py` so the appended code doesn't duplicate them.
- Task 6: confirm `_load_channel_webhooks` is actually exported by `pipeline_orchestrator.py` (or use a private-import shim).
- Task 7: confirm the existing `_run_cli` is importable from `regime_liquidator` without circular-import side effects when called from `premarket_helpers`.
- Task 9: confirm the daily-bars table name (`alpaca_bars_daily` vs. `market_bars_daily` vs. similar) before applying the migration-time SQL.
