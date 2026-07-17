# Fincept-Imports — Phase 2 Master Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the architecture-upgrade tier of imports — IC approval gate (Renaissance pattern), DataHub pub/sub schema in Redis, Jaccard headline dedup, multi-source quote-monitor fan-out, and a clean-room StrategyCoder code-gen template based on backtesting.py's API contract — without disturbing the live regime-blended sizer, the Saturday brain, or the daily Alpaca pipeline.

**Architecture:** Each project lands as an additive module behind a default-OFF gate, mirrors at least one existing in-repo pattern (correlation_adjusted_sidecar for non-fatal sidecars; mastermind-chat.service for new HTTP services; STEPS-list insertion for orchestrator changes), and ships a fixture-based test suite that runs in <5s. The DataHub refactor (2B) wraps existing Redis publishers in a thin facade *without* deleting them, so any caller that hasn't migrated keeps working.

**Tech Stack:** Python 3.13, Node 20, PostgreSQL 16, Redis 7, systemd, Discord.

**Trigger to start:** Phase 1 closure complete (✓ as of 2026-05-15) + one weekly-cycle pass clean (Saturday 2026-05-16 brain run + Monday 2026-05-18 daily pipeline). Phase 2 work can plan during the wait but should not merge any production-touching project (2A, 2B, 2D) until both passes are observed clean.

---

## Plan Scope Note

Phase 2 covers five projects across four subsystems. Per the writing-plans skill rule on multi-subsystem work, these are independent enough to each be its own plan — but Phase 1 demonstrated that a single master plan with strong sub-project separation works fine for execution and gives the operator a single document to scan. Each project below is self-contained; they can be checked out in any order after the foundational fixes (#9 and #14 from Phase 1's tracked follow-ups) are addressed. Project 2E is the only one with no production-code touch; start there if waiting on the weekly-cycle gate.

---

## Phase 2 Project Order — by risk × value

| # | Project | Production-touch | Risk | Value | Start when |
|---|---------|------------------|------|-------|-----------|
| 2C | Jaccard headline dedup | Low (write-side helper before corpus insert) | Low | High (kills duplicate headlines from RSS multi-source) | Anytime |
| 2E | StrategyCoder code-gen template | None (future strategies only) | None | High (every future strategy benefits) | Anytime |
| 2A | Renaissance IC approval gate | High (orchestrator critical path) | Medium | High (formalizes signal triage) | After weekly-cycle gate |
| 2D | Multi-source quote-monitor fan-out | High (data ingestion plumbing) | High | Medium (cleaner code, better resilience) | After 2A stable |
| 2B | DataHub pub/sub schema in Redis | High (broad refactor) | High | Medium (formalizes scattered patterns) | Last; after 2A and 2D land |

---

# PROJECT 2C — Jaccard Headline Dedup (start here)

**Goal:** Drop duplicate headlines from the news ingest before they enter `research_corpus` or any downstream NLP path. Conceptually lifted from FinceptTerminal's `NewsClusterService.cpp` (~50 LOC clean-room rewrite — no AGPL code copied).

**Why now:** Pure helper, isolated, immediate quality win. Prevents MasterMind / FinBERT (1D) / future SentimentJohn (Phase 4) from re-evaluating the same headline 3-5 times per cycle when a story shows up on Reuters + Bloomberg + Yahoo + the wire feed.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/research/headline_dedup.py` | Create | Pure functions: `tokenize`, `jaccard`, `dedup_within_window` |
| `tests/test_headline_dedup.py` | Create | 4 tests: tokenize, threshold, cross-category, time window |
| `src/ingestion/news_ingest.py` (or wherever news lands) | Modify | Wrap existing insert in `dedup_within_window` |
| `src/database/migrations/097_research_corpus_dedup_metadata.sql` | Create | Add `dedup_group_id UUID NULL` and `dedup_dropped BOOLEAN` columns to `research_corpus` for forensics |

## Tasks

- [ ] **C2.1: Locate the news-ingest entry point**

Run: `grep -rn "research_corpus" /root/openclaw/src/ --include='*.py' | grep -i "insert\|INSERT" | head -10`
Note the file + line where headlines are inserted into `research_corpus`. Likely candidates: `src/ingestion/openalex_discovery.py`, `src/ingestion/expanded_sources.py`, or a sibling. Confirm the headline column name (`title`, `headline`, etc.) and the source column name (`source`, `source_url`, etc.).

- [ ] **C2.2: Write the failing dedup tests**

Create `/root/openclaw/tests/test_headline_dedup.py`:

```python
"""Phase 2C — Jaccard headline dedup, isolated pure-function tests."""
from datetime import datetime, timedelta, timezone


def test_tokenize_lowercases_and_drops_stopwords():
    from src.research.headline_dedup import tokenize
    toks = tokenize("Apple Beats Earnings, the Stock Soars")
    assert toks == {"apple", "beats", "earnings", "stock", "soars"}


def test_jaccard_identical_is_one_disjoint_is_zero():
    from src.research.headline_dedup import jaccard
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a"}, {"b"}) == 0.0


def test_dedup_within_window_drops_near_duplicate_inside_24h():
    from src.research.headline_dedup import dedup_within_window
    now = datetime.now(tz=timezone.utc)
    items = [
        {"id": "a", "title": "Apple beats Q1 earnings, raises guidance",  "source": "reuters",  "ts": now},
        {"id": "b", "title": "Apple beats Q1 earnings; raises guidance",  "source": "bloomberg", "ts": now + timedelta(hours=1)},
        {"id": "c", "title": "Tesla Cybertruck recall affects 50,000 units", "source": "wsj",     "ts": now + timedelta(hours=2)},
    ]
    kept = dedup_within_window(items, threshold=0.25, window=timedelta(hours=24))
    assert {k["id"] for k in kept} == {"a", "c"}


def test_dedup_keeps_duplicate_outside_window():
    from src.research.headline_dedup import dedup_within_window
    now = datetime.now(tz=timezone.utc)
    items = [
        {"id": "a", "title": "Apple beats Q1 earnings, raises guidance", "source": "reuters",  "ts": now},
        {"id": "b", "title": "Apple beats Q1 earnings, raises guidance", "source": "bloomberg", "ts": now + timedelta(days=2)},
    ]
    kept = dedup_within_window(items, threshold=0.25, window=timedelta(hours=24))
    assert {k["id"] for k in kept} == {"a", "b"}
```

- [ ] **C2.3: Run test → fail on import**

`cd /root/openclaw && python3 -m pytest tests/test_headline_dedup.py -v` → ImportError.

- [ ] **C2.4: Implement the helper**

Create `/root/openclaw/src/research/headline_dedup.py`:

```python
"""Phase 2C — Jaccard headline dedup.

Concept-lifted from FinceptTerminal NewsClusterService (clean-room reimplementation;
no AGPL code copied).  Drops near-duplicate headlines within a configurable time
window using Jaccard similarity over a tokenized + stopword-stripped title.

Default threshold 0.25 fires aggressively; pass 0.20 when source-category is shared
to avoid over-collapsing intra-category coverage."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "by", "as", "at", "from", "is", "are", "was", "were", "be",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(headline: str) -> set[str]:
    """Lowercase, regex-extract alphanumeric tokens, drop stopwords."""
    return {t for t in _TOKEN_RE.findall(headline.lower()) if t not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def dedup_within_window(
    items: list[dict],
    threshold: float = 0.25,
    same_category_threshold: float = 0.20,
    window: timedelta = timedelta(hours=24),
) -> list[dict]:
    """Returns items with near-duplicates removed.  Earlier item wins; later
    duplicate is dropped.  Items must carry: id, title, source, ts (datetime).

    Optional 'category' field tightens the threshold when sources share a
    category (e.g., both 'wire-service'), preventing over-collapse of
    legitimate independent coverage."""
    items_sorted = sorted(items, key=lambda x: x["ts"])
    kept: list[dict] = []
    kept_tokens: list[tuple[set[str], dict]] = []

    for it in items_sorted:
        toks = tokenize(it["title"])
        is_dup = False
        for prior_toks, prior in kept_tokens:
            if it["ts"] - prior["ts"] > window:
                continue
            same_cat = (it.get("category") and prior.get("category") and
                        it["category"] == prior["category"])
            t = same_category_threshold if same_cat else threshold
            if jaccard(toks, prior_toks) >= (1.0 - t):
                is_dup = True
                break
        if not is_dup:
            kept.append(it)
            kept_tokens.append((toks, it))

    return kept
```

**Note**: the spec's threshold semantics — `0.25` means "drop when 75%+ token overlap" (since `jaccard >= 1.0 - 0.25 = 0.75`). FinceptTerminal docs use `0.25` as the *threshold under which to keep*; we invert so the threshold parameter reads as "looseness". Document inline.

- [ ] **C2.5: Run tests → 4 PASS**

`cd /root/openclaw && python3 -m pytest tests/test_headline_dedup.py -v`

- [ ] **C2.6: Write the migration**

Create `/root/openclaw/src/database/migrations/097_research_corpus_dedup_metadata.sql`:

```sql
-- Phase 2C — dedup forensics on research_corpus.
-- Append-only: ADD COLUMN.  No data dropped.

ALTER TABLE research_corpus
    ADD COLUMN IF NOT EXISTS dedup_group_id UUID,
    ADD COLUMN IF NOT EXISTS dedup_dropped BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_research_corpus_dedup_group ON research_corpus (dedup_group_id);
```

Apply: `cd /root/openclaw && npm run db:migrate`. Verify via psycopg2.

- [ ] **C2.7: Wire dedup into the news-ingest insert**

In whichever ingest module C2.1 surfaced, before the `INSERT INTO research_corpus`:

```python
from src.research.headline_dedup import dedup_within_window
from datetime import timedelta
import uuid

# Pull last 24h of recently-inserted titles for the dedup window
cur.execute("SELECT id, title, source, fetched_at FROM research_corpus "
            "WHERE fetched_at > NOW() - INTERVAL '24 hours' "
            "AND dedup_dropped = FALSE")
recent = [dict(zip(["id", "title", "source", "ts"], r)) for r in cur.fetchall()]

# Tag the new batch with a dedup_group_id and append to the recent set
group_id = str(uuid.uuid4())
new_items = [{"id": "_new_" + str(i), "title": t, "source": s,
              "ts": datetime.now(tz=timezone.utc), "category": cat}
             for i, (t, s, cat) in enumerate(new_titles_to_insert)]

kept = dedup_within_window(recent + new_items, threshold=0.25,
                           same_category_threshold=0.20)
kept_ids = {k["id"] for k in kept}
dropped_new = [n for n in new_items if n["id"] not in kept_ids]

# Insert kept new items normally; insert dropped ones with dedup_dropped=TRUE for forensics
```

The exact column names + INSERT shape depend on what C2.1 found. Surgical insert; do not refactor the surrounding code.

- [ ] **C2.8: Smoke + commit**

Run a manual ingest cycle (or wait for the next Saturday brain) and confirm the dropped-count is non-zero on a typical multi-source day. Verify via:

```sql
SELECT COUNT(*) FILTER (WHERE dedup_dropped) AS dropped,
       COUNT(*) FILTER (WHERE NOT dedup_dropped) AS kept
FROM research_corpus
WHERE fetched_at > NOW() - INTERVAL '7 days';
```

Commit:
```bash
git commit -m "feat(research): Jaccard headline dedup before research_corpus insert

24h-window Jaccard dedup (threshold 0.25, 0.20 if same source-category)
strips near-duplicate headlines before they reach the corpus.  Concept-lifted
from FinceptTerminal NewsClusterService (clean-room; no AGPL code copied).

Migration 097 adds dedup_group_id + dedup_dropped columns for forensics
(append-only; dropped headlines are preserved with the flag set).

Phase 2C of the Fincept-imports master plan."
```

---

# PROJECT 2E — StrategyCoder Code-Gen Template

**Goal:** Adopt backtesting.py's `Strategy.init/next` contract + `commission=callable(size, price)` hook as the canonical template StrategyCoderJohn emits, with our own clean-room base class. No AGPL dep; just the API shape.

**Why now:** Touches no production code — only StrategyCoder's prompt + a new base class for *future* strategies. Existing 91 strategies untouched.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/strategies/strategy_template_base.py` | Create | Clean-room ABC with init/next contract + commission callable |
| `tests/test_strategy_template_base.py` | Create | 5 tests: warmup detection, indicator slicing, fee callable, no-overlap signals, EOD close |
| `src/agent/prompts/subagents/strategycoder.md` | Modify | Document the new template; show one canonical example |
| `src/strategies/implementations/_template_example.py` | Create | One reference strategy demonstrating the template |

## Tasks

- [ ] **E2.1: Read current StrategyCoder prompt + an existing strategy**

Open `src/agent/prompts/subagents/strategycoder.md` and `src/strategies/implementations/<any>.py` to understand today's contract. The new template adds, doesn't replace.

- [ ] **E2.2: Failing tests for the base class**

Create `/root/openclaw/tests/test_strategy_template_base.py`:

```python
"""Phase 2E — clean-room StrategyTemplate ABC tests.

Contract-equivalent to backtesting.py Strategy.init/next API but written from
scratch (no AGPL code copied).  Tests fix the contract so future
StrategyCoder-emitted strategies have an oracle."""
import pandas as pd
import pytest


def _ohlcv(n=20, base=100.0):
    idx = pd.date_range("2026-01-02", periods=n, freq="1D", tz="America/New_York")
    return pd.DataFrame({
        "open": base, "high": base + 1, "low": base - 1, "close": base + 0.5, "volume": 1000,
    }, index=idx)


def test_init_runs_once_and_next_runs_per_bar():
    from src.strategies.strategy_template_base import StrategyTemplate, run
    init_calls = []
    next_calls = []

    class S(StrategyTemplate):
        def init(self):
            init_calls.append(1)
        def next(self):
            next_calls.append(self.bar_idx)

    run(S, _ohlcv(5))
    assert sum(init_calls) == 1
    assert next_calls == [0, 1, 2, 3, 4]


def test_indicator_warmup_detection():
    from src.strategies.strategy_template_base import StrategyTemplate, run
    seen_lengths = []

    class S(StrategyTemplate):
        def init(self):
            self.sma3 = self.I(lambda c: c.rolling(3).mean(), self.data.close)
        def next(self):
            seen_lengths.append((self.bar_idx, pd.notna(self.sma3.iloc[self.bar_idx])))

    run(S, _ohlcv(6))
    # SMA(3) is NaN until bar 2; valid from bar 2 onward
    assert seen_lengths == [(0, False), (1, False), (2, True), (3, True), (4, True), (5, True)]


def test_commission_callable_invoked_per_fill():
    from src.strategies.strategy_template_base import StrategyTemplate, run
    fees_paid = []

    def fee(size, price):
        fees_paid.append((size, price))
        return abs(size) * 0.005  # half-cent per share

    class S(StrategyTemplate):
        def init(self): pass
        def next(self):
            if self.bar_idx == 1:
                self.buy(qty=100)
            elif self.bar_idx == 3:
                self.sell(qty=100)

    result = run(S, _ohlcv(5), commission=fee)
    assert len(fees_paid) == 2
    assert result["total_commission"] == pytest.approx(1.0)


def test_no_overlap_signals_when_position_held():
    from src.strategies.strategy_template_base import StrategyTemplate, run

    class S(StrategyTemplate):
        def init(self): pass
        def next(self):
            self.buy(qty=10)  # same signal every bar — should only fire once

    result = run(S, _ohlcv(5))
    assert result["fills"] == 1


def test_eod_close_flushes_position():
    from src.strategies.strategy_template_base import StrategyTemplate, run

    class S(StrategyTemplate):
        def init(self): pass
        def next(self):
            if self.bar_idx == 0:
                self.buy(qty=10)

    result = run(S, _ohlcv(3), close_at_eod=True)
    assert result["fills"] == 2  # entry + EOD exit
    assert result["position_at_end"] == 0
```

- [ ] **E2.3: Run → fail on import**

`pytest tests/test_strategy_template_base.py -v`

- [ ] **E2.4: Implement the base class**

Create `/root/openclaw/src/strategies/strategy_template_base.py`:

```python
"""Phase 2E — clean-room StrategyTemplate ABC.

Contract-equivalent to backtesting.py Strategy.init/next API; written from
scratch.  Used as the code-gen template for StrategyCoderJohn-emitted strategies.

The ABC enforces:
  - init() runs once before the first bar.  Indicator declarations via self.I().
  - next() runs once per bar; self.bar_idx is the current row.
  - self.buy(qty) / self.sell(qty) place orders for the next bar's open.
  - commission callable: fn(size: int, price: float) -> float (USD)
  - close_at_eod=True forces flat at end of data.

NOT a backtest engine.  Use src/backtest/quick_backtest.run_single_bracket
or src/backtest/unified_backtest.py for production strategy evaluation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional
import pandas as pd


CommissionFn = Callable[[int, float], float]


class StrategyTemplate(ABC):
    def __init__(self, data: pd.DataFrame, commission: Optional[CommissionFn] = None):
        self.data = data
        self._commission = commission or (lambda size, price: 0.0)
        self.bar_idx = 0
        self._position = 0
        self._fills: list[dict] = []
        self._pending_orders: list[dict] = []

    def I(self, fn: Callable, *series) -> pd.Series:
        """Declare an indicator.  Computed once at init time."""
        return fn(*series)

    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def next(self) -> None: ...

    def buy(self, qty: int) -> None:
        if self._position != 0:
            return
        self._pending_orders.append({"side": "buy", "qty": qty, "bar": self.bar_idx + 1})

    def sell(self, qty: int) -> None:
        if self._position == 0:
            return
        self._pending_orders.append({"side": "sell", "qty": qty, "bar": self.bar_idx + 1})


def run(
    cls: type[StrategyTemplate],
    data: pd.DataFrame,
    commission: Optional[CommissionFn] = None,
    close_at_eod: bool = False,
) -> dict:
    """Drive a StrategyTemplate subclass through `data`.  Returns:
       {fills, total_commission, position_at_end, fills_log}"""
    s = cls(data, commission=commission)
    s.init()
    total_commission = 0.0

    for i in range(len(data)):
        s.bar_idx = i
        # Fill pending orders at this bar's open
        new_pending = []
        for o in s._pending_orders:
            if o["bar"] == i:
                price = float(data.iloc[i].open)
                size = o["qty"] if o["side"] == "buy" else -o["qty"]
                s._position += size
                fee = s._commission(size, price)
                total_commission += fee
                s._fills.append({"bar": i, "side": o["side"], "qty": o["qty"],
                                 "price": price, "fee": fee})
            else:
                new_pending.append(o)
        s._pending_orders = new_pending
        s.next()

    if close_at_eod and s._position != 0:
        last_close = float(data.iloc[-1].close)
        size = -s._position
        fee = s._commission(size, last_close)
        total_commission += fee
        s._fills.append({"bar": len(data) - 1, "side": "sell" if size < 0 else "buy",
                         "qty": abs(size), "price": last_close, "fee": fee})
        s._position = 0

    return {
        "fills":            len(s._fills),
        "total_commission": total_commission,
        "position_at_end":  s._position,
        "fills_log":        s._fills,
    }
```

- [ ] **E2.5: Run tests → 5 PASS**

`pytest tests/test_strategy_template_base.py -v`

- [ ] **E2.6: Reference example strategy**

Create `/root/openclaw/src/strategies/implementations/_template_example.py`:

```python
"""Phase 2E — reference strategy demonstrating StrategyTemplate.

NOT registered for live trading.  The leading underscore in the filename is
a deliberate signal to lifecycle.py to skip this file during strategy discovery.

Pattern: SMA crossover with ATR-based exit and per-share commission."""
from src.strategies.strategy_template_base import StrategyTemplate


class SMACrossoverExample(StrategyTemplate):
    def init(self):
        self.sma_fast = self.I(lambda c: c.rolling(10).mean(), self.data.close)
        self.sma_slow = self.I(lambda c: c.rolling(30).mean(), self.data.close)

    def next(self):
        if self.bar_idx < 30:
            return
        if self.sma_fast.iloc[self.bar_idx] > self.sma_slow.iloc[self.bar_idx]:
            self.buy(qty=100)
        elif self.sma_fast.iloc[self.bar_idx] < self.sma_slow.iloc[self.bar_idx]:
            self.sell(qty=100)


# Standard half-cent + SEC/TAF fees a la Alpaca
def alpaca_fee(size: int, price: float) -> float:
    notional = abs(size) * price
    sec_fee = notional * 0.0000278  # SEC §31 fee, sells only — simplified to both
    taf_fee = abs(size) * 0.000166   # FINRA TAF, sells only — simplified
    return abs(size) * 0.005 + sec_fee + taf_fee
```

- [ ] **E2.7: Update StrategyCoder prompt**

Append to `/root/openclaw/src/agent/prompts/subagents/strategycoder.md`:

```markdown
## Strategy template (as of 2026-05-15)

For new strategies, prefer the `StrategyTemplate` ABC at
`src/strategies/strategy_template_base.py`.  See
`src/strategies/implementations/_template_example.py` for a canonical
SMA-crossover example with the Alpaca fee model.

The template enforces:
- `init()` declares indicators via `self.I(fn, *series)` — runs once
- `next()` runs per bar; place orders via `self.buy(qty)` / `self.sell(qty)`
- Orders fill at next bar's open (no look-ahead by construction)
- Commission is a callable `fn(size, price) -> float` — pass `alpaca_fee`
  from the example for production-shaped fee modeling
- `close_at_eod=True` flattens at end-of-data (good for intraday strategies)

Existing strategies under `src/strategies/implementations/` use the older
contract; do NOT migrate them as part of new-strategy work.
```

- [ ] **E2.8: Commit**

```bash
git add src/strategies/strategy_template_base.py \
        src/strategies/implementations/_template_example.py \
        src/agent/prompts/subagents/strategycoder.md \
        tests/test_strategy_template_base.py
git commit -m "feat(strategies): clean-room StrategyTemplate ABC for code-gen

Concept-lifted from backtesting.py's Strategy.init/next API + commission
callable hook (no AGPL code copied; behavior contract reproduced).

Becomes the canonical code-gen target StrategyCoderJohn emits for new
strategies.  Existing strategies under src/strategies/implementations/
keep using the older contract — no migration.

Phase 2E of the Fincept-imports master plan."
```

---

# PROJECT 2A — Renaissance IC Approval Gate

**Goal:** Insert an explicit IC (Investment Committee) approval state into the orchestrator between `signals` and `handoff`. Live-eligible strategies auto-approve; staging-tier signals trigger a Discord prompt that the operator can approve/veto/scale.

**Why staged:** This is the most production-touching project of Phase 2 — the orchestrator's critical path. Default-OFF gate; ramp in shadow first.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/execution/ic_gate.py` | Create | Pure-function classifier: per-signal → AUTO_APPROVE / IC_REQUIRED / VETO + reason |
| `src/execution/ic_gate_runner.py` | Create | Orchestrator step entry: reads signals, applies classifier, prompts Discord on IC_REQUIRED, writes decisions to a new `ic_decisions` table |
| `src/database/migrations/098_ic_decisions.sql` | Create | `ic_decisions(id, run_date, strategy_id, ticker, classification, reason, decided_by, decided_at, scaled_size_pct)` |
| `tests/test_ic_gate.py` | Create | 6 tests: live auto-approve, staging IC-prompt, deprecated veto, malformed signal handling, scale-down clamp, default-OFF behavior |
| `src/execution/pipeline_orchestrator.py` | Modify | Insert `ic_gate` step between `signals` and `handoff` (default-OFF behind `OPENCLAW_IC_GATE=1`) |
| `src/agent/prompts/subagents/botjohn.md` | Modify | Document IC approval expectations |

## Tasks

- [ ] **A2.1: Read existing handoff to understand the signal record shape**

Same investigation as 1G's G.2 — re-confirm the per-signal dict shape that flows from `signals` step to `handoff` step. The IC gate operates on the signals list before handoff serializes them.

- [ ] **A2.2: Migration**

```sql
-- Phase 2A — IC approval decisions, append-only.
CREATE TABLE IF NOT EXISTS ic_decisions (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    strategy_id     TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    classification  TEXT NOT NULL CHECK (classification IN ('AUTO_APPROVE','IC_REQUIRED','APPROVED','VETOED','SCALED','TIMED_OUT')),
    reason          TEXT,
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    scaled_size_pct DOUBLE PRECISION,
    raw_signal      JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ic_decisions_run ON ic_decisions (run_date, strategy_id);
```

Apply: `npm run db:migrate`. Verify.

- [ ] **A2.3: Failing classifier tests**

```python
# tests/test_ic_gate.py — 6 tests
# 1. live-eligible strategy → AUTO_APPROVE
# 2. staging-tier strategy → IC_REQUIRED
# 3. deprecated strategy → VETO with reason
# 4. malformed signal (missing fields) → VETO with reason
# 5. scale-down request clamps to 0..1 size_pct
# 6. is_enabled() reflects OPENCLAW_IC_GATE env var
```

(Spell out exact test bodies during implementation; pattern matches Phase 1 1A/1B test files.)

- [ ] **A2.4: Pure-function classifier (`src/execution/ic_gate.py`)**

Reads strategy lifecycle state from the manifest (`src/strategies/manifest.json`), classifies each signal:
- `lifecycle == 'live'` → `AUTO_APPROVE`
- `lifecycle in ('staging', 'paper')` → `IC_REQUIRED`
- `lifecycle in ('deprecated', 'archived')` → `VETOED` with reason
- Missing `strategy_id` or `ticker` → `VETOED` with reason "malformed_signal"
- Returns dict per signal: `{classification, reason, scaled_size_pct (None unless scaled)}`

Pure function; no I/O.

- [ ] **A2.5: Runner with Discord prompting (`src/execution/ic_gate_runner.py`)**

Entry: `def main()`. Reads today's signals, runs classifier, persists to `ic_decisions`. For `IC_REQUIRED` rows:
- Posts a single consolidated Discord message to `#ic-approvals` channel: tabular summary, one row per IC-required signal, with `approve {n}` / `veto {n}` / `scale {n} {pct}` slash-commands the operator types back.
- Polls the `ic_decisions` table every 30s for up to `IC_TIMEOUT_SECONDS` (default 600s) for status transitions from `IC_REQUIRED` → `APPROVED` / `VETOED` / `SCALED`.
- After timeout, any still-`IC_REQUIRED` row is re-classified `TIMED_OUT` (and treated as VETO downstream — fail-safe).

Default-OFF: early-exit on `OPENCLAW_IC_GATE != "1"`.

- [ ] **A2.6: Discord-side handler**

Add `src/channels/discord/ic_handler.js` to listen for `approve N`, `veto N`, `scale N pct` messages in `#ic-approvals`, write the decision to `ic_decisions` (UPDATE `decided_by`, `decided_at`, `classification`, optionally `scaled_size_pct`).

- [ ] **A2.7: Wire into orchestrator**

Insert `ic_gate` step BETWEEN `signals` and `handoff` (different from where 1G's pyportfolioopt_shadow lives). Step is non-fatal: if `ic_gate_runner.py` raises, log + continue with all signals AUTO_APPROVED (fail-open per the existing `tradejohn_confirmer` pattern in `regime_blended_sizer_live.py`).

- [ ] **A2.8: Default-OFF smoke + commit**

Verify with `OPENCLAW_IC_GATE` unset: `signals → handoff` flow is byte-identical to today (no IC gate runs).
Verify with `OPENCLAW_IC_GATE=1` and a synthetic staging-tier signal: Discord prompt fires, operator approves, `ic_decisions` row updates, signal flows to handoff.

Commit message references the Renaissance workflow phase model from the FinceptTerminal concept-lift.

---

# PROJECT 2D — Multi-Source Quote-Monitor Fan-Out

**Goal:** Refactor the scattered Polygon + FMP + Alpaca + Yahoo quote-fetching into a single `QuoteMonitor` orchestrator with partition-by-source fan-out + 3s ack timeout. Concept-lifted from achannarasappa/ticker's `internal/monitor/monitor.go`.

**Why later in Phase 2:** Touches the daily `collect` step's data ingestion plumbing. High blast radius if the partitioning logic mishandles a source. Default-OFF gate, run side-by-side with the existing path for a week before flipping the switch.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/ingestion/quote_monitor.py` | Create | `QuoteMonitor.set_symbols(tickers)` orchestrator |
| `src/ingestion/quote_sources/__init__.py` | Create | Source registry |
| `src/ingestion/quote_sources/polygon.py` | Create | Polygon adapter (wraps existing path) |
| `src/ingestion/quote_sources/fmp.py` | Create | FMP adapter |
| `src/ingestion/quote_sources/alpaca.py` | Create | Alpaca adapter |
| `src/ingestion/quote_sources/yahoo.py` | Create | Yahoo Tier-2 adapter |
| `tests/test_quote_monitor.py` | Create | 8 tests: per-source partition, fan-out, 3s ack timeout, source failure isolation, ticker → source routing, multi-source price reconciliation, env-gated source selection, dedup-on-rapid-update |

## Tasks (compressed; each step expands during implementation)

- [ ] **D2.1**: Survey all current quote-fetching sites: `grep -rn "polygon\|fmp\|alpaca.*quote\|yahoo" /root/openclaw/src/ingestion/`. Inventory which paths fetch quotes vs. ingest other data.
- [ ] **D2.2**: Define the `QuoteSource` protocol — methods `fetch(tickers) -> dict[str, Quote]`, `name`, `priority`.
- [ ] **D2.3**: Write failing tests for the orchestrator (8 tests above).
- [ ] **D2.4**: Implement `QuoteMonitor` with `asyncio.gather` for fan-out + per-source 3s timeout. Sources race; first non-stale price wins per ticker.
- [ ] **D2.5**: Adapt each source under `src/ingestion/quote_sources/` to the protocol — wrap, do not rewrite, the existing fetch logic.
- [ ] **D2.6**: Default-OFF gate `OPENCLAW_UNIFIED_QUOTES=1`. The existing `collect` step keeps using the legacy paths until the gate flips.
- [ ] **D2.7**: Side-by-side parity smoke: with the gate ON in a separate process, fetch the daily ticker universe via both paths, diff outputs, log any divergence to a new `quote_monitor_parity` table for offline review.
- [ ] **D2.8**: Run for 5 trading days; review parity table; if clean, set the gate ON in production.
- [ ] **D2.9**: Commit at each stage (orchestrator + sources, then gate flip after parity-clean).

---

# PROJECT 2B — DataHub Pub/Sub Schema in Redis

**Goal:** Formalize the topic-publishing pattern across the codebase. Today: scattered `redis.publish(channel, msg)` calls with no consistent key schema, no TTL discipline, no per-producer rate-limit. Concept-lifted from FinceptTerminal's `DATAHUB_ARCHITECTURE.md`.

**Why last:** Highest refactor blast radius — every sub-bot uses Redis for steering / status / rate-limiting. The new facade ships *alongside* the existing scatter; refactoring callers is a follow-on commit per caller.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/database/datahub.py` | Create | Pub/sub facade with topic schema, TTL, dedup, rate-limit |
| `src/database/datahub_topics.py` | Create | Topic constant registry (e.g., `T_AGENT_STATUS = "agent:status:{agent_id}"`) |
| `tests/test_datahub.py` | Create | 6 tests: topic-key validation, TTL expiry, dedup window, rate-limit window, wildcard subscribe, payload size cap |
| `docs/datahub.md` | Create | Topic schema documentation |
| (Caller migrations) | Modify | One commit per migrated caller (botjohn-direct, swarm.js, mastermind, etc.) |

## Tasks (compressed)

- [ ] **B2.1**: Inventory all `redis.publish` / `redis.set` usage. Group by purpose: status, steering, rate-limit, cache.
- [ ] **B2.2**: Define topic schema — `domain:subdomain:id` (e.g., `agent:status:tradejohn`, `pipeline:event:cycle-start`, `data:fetch:polygon-quote`).
- [ ] **B2.3**: Write `DataHub.publish(topic, payload, ttl=None, dedup_window=None)` and `DataHub.subscribe(topic_pattern, callback)`.
- [ ] **B2.4**: Per-producer rate limit (`fail_soft` if exceeded; emit a `datahub:rate-limited` topic event for observability).
- [ ] **B2.5**: Migrate the simplest caller first (e.g., `botjohn-direct.js` status pings). Verify behavior unchanged via existing tests.
- [ ] **B2.6**: Migrate the next caller. Repeat until all callers use DataHub.
- [ ] **B2.7**: Delete the legacy scatter only after every caller migrated. Final commit.

---

# Cross-Phase 2 Closure

After all 5 projects merged:

- [ ] Full pytest run against `feat/fincept-imports-phase-2`
- [ ] One Saturday brain pass clean (validates 2C dedup doesn't drop legitimate distinct headlines)
- [ ] One full weekday pass with `OPENCLAW_IC_GATE=1` + `OPENCLAW_UNIFIED_QUOTES=1` (validates 2A + 2D under live conditions)
- [ ] Update CLAUDE.md with Phase 2 completion line
- [ ] Final whole-branch code review

# Phase 3 trigger

Phase 3 (productionize spikes) starts when:
- 4 weeks of `pyportfolioopt_shadow_runs` data accumulated (1G output) — promotion decision on PyPortfolioOpt sizer
- 2C dedup demonstrated effective (>10% headlines dropped on average; no false-positives flagged by MasterMind)
- 1F DBnomics + Polymarket clients show ≥3 series each with material correlation to next-day moves

---

# Self-Review

**Spec coverage:** All 5 Phase-2 projects from the Phase 1 master plan have a dedicated section. ✓

**Risk ordering preserved:** 2C (lowest risk) first; 2B (highest blast radius) last. ✓

**Default-OFF discipline:** Every production-touching project (2A, 2B, 2D) ships behind a default-OFF env gate. ✓

**No live-path edits without parity:** 2A fail-open, 2D side-by-side parity table, 2B refactor-alongside-then-delete pattern. ✓

**Engineer-judgment branch points** (not placeholders — explicit decisions the executor must make):
- 2A: where to insert the IC gate step (between `signals` and `handoff` is the spec; between `handoff` and `trade` is also defensible if the handoff serializer needs to know about veto'd signals).
- 2D: which source becomes the "primary" for parity diff (Polygon today; should the unified path treat all sources symmetrically and let the recency-stamp arbitrate, or preserve a fixed priority order?).
- 2B: hard call between (a) facade-then-migrate-callers-incrementally vs (b) atomic refactor of the whole Redis layer.

These are decisions the executor must make with knowledge of the live system state at execution time.

---

**Plan complete.**
