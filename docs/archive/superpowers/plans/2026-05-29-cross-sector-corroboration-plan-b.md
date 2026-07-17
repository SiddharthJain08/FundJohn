# Plan B — News Backfill + News-Sentiment Long/Short Strategy

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backfill years of historical news into a real point-in-time sentiment series, wire it into the backtest aux-data path, and build/backtest `S_news_sentiment_long_short` — a sentiment-primary equity strategy — before any promotion.

**Architecture:** A backfill script pages Alpaca/Benzinga news (verified back to 2018) per symbol-chunk per date-window, FinBERT-scores headlines (reusing `src/ingestion/alpaca_news.py` helpers), aggregates per `(ticker, date)`, and **appends** to the existing `data/master/sentiment.parquet` + `ticker_sentiment_daily` (append-only; idempotent on `(ticker,date)`). `aux_data_loader.py` gains a point-in-time `sentiment` panel mirroring its `options` panel. Strategy ③ reads `aux_data['sentiment']` to rank long/short. News-only signal (no historical Reddit/StockTwits — documented gap).

**Tech Stack:** Python 3, pandas, psycopg2, pytest. FinBERT service `finbert-sentiment.service` on `127.0.0.1:7872` (`src.services.finbert.client.FinbertClient`). Alpaca CLI `/root/go/bin/alpaca data news --symbols --start --end --page-token --limit --include-content`. Spec: `docs/superpowers/specs/2026-05-29-cross-sector-corroboration-strategies-design.md`. **Depends on Plan A** (`src/strategies/confirmation/` package + `momentum_base`).

**Conventions (verified in-repo):**
- `sentiment.parquet` columns: `ticker, date, social_posts_24h, social_bull_ratio, social_bear_ratio, social_unique_authors, social_top_themes, news_count_24h, news_finbert_pos, news_finbert_neu, news_finbert_neg, news_mean_score, news_top_headlines`. Backfill writes the `news_*` columns; `social_*` left null (news-only).
- Reuse `alpaca_news._score_with_finbert(articles)` (sets `finbert_label`/`finbert_score` signed) and `alpaca_news._aggregate_per_ticker(articles)` (→ `{count, finbert_pos/neu/neg, mean_score, top_headlines}`).
- **NEVER-DELETE invariant:** backfill only INSERTs/appends new `(ticker,date)` rows; never overwrites or deletes existing rows. Idempotent re-runs.
- No-lookahead: a `(ticker,date)` row aggregates articles by `published_at` calendar date; the backtest bar at day `t` (close-based) may read sentiment with `date <= t` (news on day `t` is public by its close) — mirror the options panel's `date <= as_of` slice exactly.
- Strategy file header: `sys.path.insert(.., '..','..')` then `from strategies.base import BaseStrategy, Signal` (Plan A convention).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/strategies/confirmation/news_flow.py` | Pure signed-sentiment scorer (shared) |
| `scripts/backfill_news_sentiment.py` | Page+score+aggregate+append historical news (the long pole) |
| `src/strategies/aux_data_loader.py` | Add point-in-time `sentiment` panel (mirror `options`) |
| `src/strategies/implementations/S_news_sentiment_long_short.py` | Strategy ③ |
| `src/strategies/implementations/S_news_sentiment_long_short.requirements.json` | ③ data reqs |
| `tests/strategies/test_news_sentiment.py` | Unit tests: news_flow, backfill aggregation/idempotency, aux panel, strategy |
| `src/strategies/registry.py` / `manifest.json` | Register ③ (Task 7, gated on metrics) |

---

## Task 1: news_flow scorer (pure)

**Files:**
- Create: `src/strategies/confirmation/news_flow.py`
- Test: `tests/strategies/test_news_sentiment.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_news_sentiment.py
import pytest
from strategies.confirmation import news_flow as nf


def test_score_positive_with_volume():
    s = nf.score({'news_mean_score': 0.6, 'news_count_24h': 5}, {'min_articles': 2})
    assert s > 0


def test_score_zero_below_min_articles():
    assert nf.score({'news_mean_score': 0.9, 'news_count_24h': 1}, {'min_articles': 2}) == 0.0


def test_score_negative_for_bearish_news():
    assert nf.score({'news_mean_score': -0.5, 'news_count_24h': 4}, {'min_articles': 2}) < 0


def test_score_missing_data_is_zero():
    assert nf.score(None, {'min_articles': 2}) == 0.0
    assert nf.score({}, {'min_articles': 2}) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k score -v`
Expected: FAIL — `ImportError: cannot import name 'news_flow'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/strategies/confirmation/news_flow.py
"""Pure signed-sentiment scorer for the news-sentiment-primary strategy.

Maps a per-ticker daily sentiment row to a signed score in [-1, 1]. Requires a minimum
article count so single-headline noise can't drive a position. Deterministic.
"""
from __future__ import annotations
from typing import Optional


def score(sent_row: Optional[dict], params: dict) -> float:
    """Signed sentiment in [-1,1]; 0.0 when missing or below the article-count floor."""
    if not sent_row:
        return 0.0
    n = sent_row.get('news_count_24h') or 0
    if n < params.get('min_articles', 2):
        return 0.0
    mean = sent_row.get('news_mean_score')
    if mean is None:
        return 0.0
    return max(min(float(mean), 1.0), -1.0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k score -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/confirmation/news_flow.py tests/strategies/test_news_sentiment.py
git commit -m "feat(confirmation): pure signed news-sentiment scorer"
```

---

## Task 2: News backfill script

**Files:**
- Create: `scripts/backfill_news_sentiment.py`
- Test: `tests/strategies/test_news_sentiment.py` (append — test the pure aggregation + idempotent-merge helpers, not the network calls)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/strategies/test_news_sentiment.py
import pandas as pd
import importlib.util, pathlib
_spec = importlib.util.spec_from_file_location(
    'backfill_news_sentiment',
    str(pathlib.Path(__file__).resolve().parents[2] / 'scripts' / 'backfill_news_sentiment.py'))
bns = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bns)


def test_articles_to_daily_rows_groups_by_ticker_date():
    articles = [
        {'symbols': ['AAA'], 'published_at': '2022-03-01T14:00:00Z', 'finbert_label': 'positive', 'finbert_score': 0.8, 'headline': 'h1'},
        {'symbols': ['AAA'], 'published_at': '2022-03-01T18:00:00Z', 'finbert_label': 'negative', 'finbert_score': -0.4, 'headline': 'h2'},
        {'symbols': ['BBB'], 'published_at': '2022-03-02T10:00:00Z', 'finbert_label': 'positive', 'finbert_score': 0.5, 'headline': 'h3'},
    ]
    rows = bns.articles_to_daily_rows(articles)
    aaa = [r for r in rows if r['ticker'] == 'AAA' and r['date'] == '2022-03-01'][0]
    assert aaa['news_count_24h'] == 2
    assert aaa['news_finbert_pos'] == 1 and aaa['news_finbert_neg'] == 1
    assert abs(aaa['news_mean_score'] - 0.2) < 1e-9


def test_merge_append_only_is_idempotent():
    existing = pd.DataFrame([{'ticker': 'AAA', 'date': '2022-03-01', 'news_count_24h': 2, 'news_mean_score': 0.2}])
    new = pd.DataFrame([
        {'ticker': 'AAA', 'date': '2022-03-01', 'news_count_24h': 99, 'news_mean_score': 9.9},  # dup → must NOT overwrite
        {'ticker': 'BBB', 'date': '2022-03-02', 'news_count_24h': 1, 'news_mean_score': 0.5},    # new → appended
    ])
    merged = bns.merge_append_only(existing, new)
    aaa = merged[(merged.ticker == 'AAA') & (merged.date == '2022-03-01')].iloc[0]
    assert aaa['news_count_24h'] == 2          # original preserved
    assert len(merged) == 2                     # BBB added, AAA not duplicated
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k "daily_rows or idempotent" -v`
Expected: FAIL — script module not found

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/backfill_news_sentiment.py
"""Backfill historical news → FinBERT → per-(ticker,date) sentiment (append-only).

Pages Alpaca/Benzinga news per symbol-chunk over a date window, scores headlines with the
local FinBERT service (reusing src/ingestion/alpaca_news helpers), aggregates per ticker per
calendar day, and APPENDS new (ticker,date) rows to data/master/sentiment.parquet and
ticker_sentiment_daily. Never overwrites existing rows (honors NEVER-DELETE). Resumable.

Usage:
  python3 scripts/backfill_news_sentiment.py --start 2018-01-01 --end 2024-12-31 \
      [--symbols AAPL,MSFT | --universe-active] [--chunk-days 30] [--dry-run]
"""
from __future__ import annotations
import argparse, json, subprocess, sys, os
from collections import defaultdict
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SENT_PATH = ROOT / 'data' / 'master' / 'sentiment.parquet'
ALPACA = '/root/go/bin/alpaca'
_SIGN = {'positive': 1, 'neutral': 0, 'negative': -1}

NEWS_COLS = ['ticker', 'date', 'news_count_24h', 'news_finbert_pos', 'news_finbert_neu',
             'news_finbert_neg', 'news_mean_score', 'news_top_headlines']


def articles_to_daily_rows(articles: list[dict]) -> list[dict]:
    """Group scored articles by (symbol, published-date) → daily sentiment rows.
    Each article must already carry finbert_label/finbert_score (see score_articles)."""
    groups: dict[tuple, list] = defaultdict(list)
    for a in articles:
        d = (a.get('published_at') or '')[:10]
        if not d:
            continue
        for sym in a.get('symbols', []) or []:
            groups[(sym, d)].append(a)
    rows = []
    for (sym, d), arts in groups.items():
        pos = sum(1 for a in arts if a.get('finbert_label') == 'positive')
        neu = sum(1 for a in arts if a.get('finbert_label') == 'neutral')
        neg = sum(1 for a in arts if a.get('finbert_label') == 'negative')
        scores = [float(a.get('finbert_score', 0.0)) for a in arts]
        mean = sum(scores) / len(scores) if scores else 0.0
        top3 = sorted(arts, key=lambda a: abs(a.get('finbert_score', 0.0)), reverse=True)[:3]
        rows.append({'ticker': sym, 'date': d, 'news_count_24h': len(arts),
                     'news_finbert_pos': pos, 'news_finbert_neu': neu, 'news_finbert_neg': neg,
                     'news_mean_score': mean,
                     'news_top_headlines': json.dumps([{'headline': a.get('headline', ''),
                                                        'score': a.get('finbert_score', 0.0)} for a in top3])})
    return rows


def merge_append_only(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Append only (ticker,date) rows not already present. Existing rows win."""
    if existing is None or existing.empty:
        return new.drop_duplicates(['ticker', 'date'])
    key = ['ticker', 'date']
    have = set(map(tuple, existing[key].astype(str).values.tolist()))
    fresh = new[~new[key].astype(str).apply(tuple, axis=1).isin(have)]
    return pd.concat([existing, fresh], ignore_index=True)


def score_articles(articles: list[dict]) -> list[dict]:
    """Reuse the live FinBERT scorer so backfill == live scoring (parity)."""
    from src.ingestion.alpaca_news import _score_with_finbert
    return _score_with_finbert(articles)


def _fetch_window(symbols: list[str], start: str, end: str) -> list[dict]:
    """Page `alpaca data news` for one symbol-chunk + date window (all pages)."""
    out, token = [], None
    while True:
        cmd = [ALPACA, 'data', 'news', '--symbols', ','.join(symbols),
               '--start', start, '--end', end, '--limit', '50', '--exclude-contentless']
        if token:
            cmd += ['--page-token', token]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            break
        data = json.loads(res.stdout or '{}')
        out.extend(data.get('news', []) or [])
        token = data.get('next_page_token')
        if not token:
            break
    return out


def _active_universe() -> list[str]:
    from src.strategies.universe_resolver import union_universe   # live universe envelope
    return sorted(union_universe(pd.Timestamp.utcnow().strftime('%Y-%m-%d'), ['live', 'candidate']))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--symbols')
    ap.add_argument('--universe-active', action='store_true')
    ap.add_argument('--chunk-days', type=int, default=30)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    symbols = (args.symbols.split(',') if args.symbols
               else _active_universe() if args.universe_active else [])
    if not symbols:
        sys.exit('no symbols (pass --symbols or --universe-active)')

    windows = pd.date_range(args.start, args.end, freq=f'{args.chunk_days}D')
    all_rows: list[dict] = []
    for i in range(len(windows)):
        w_start = windows[i].strftime('%Y-%m-%dT00:00:00Z')
        w_end = (windows[i + 1] if i + 1 < len(windows) else pd.Timestamp(args.end)).strftime('%Y-%m-%dT23:59:59Z')
        for j in range(0, len(symbols), 50):
            chunk = symbols[j:j + 50]
            arts = _fetch_window(chunk, w_start, w_end)
            if arts:
                all_rows.extend(articles_to_daily_rows(score_articles(arts)))
        print(f'[backfill] window {w_start[:10]} rows so far={len(all_rows)}', file=sys.stderr)

    new_df = pd.DataFrame(all_rows, columns=NEWS_COLS)
    if args.dry_run:
        print(f'[dry-run] would append {len(new_df)} (ticker,date) rows', file=sys.stderr)
        return
    existing = pd.read_parquet(SENT_PATH) if SENT_PATH.exists() else pd.DataFrame()
    merged = merge_append_only(existing, new_df)
    merged.to_parquet(SENT_PATH, index=False)
    print(f'[backfill] sentiment.parquet rows {len(existing)} -> {len(merged)}', file=sys.stderr)
    # (Optional) mirror new rows into ticker_sentiment_daily via the same append-only key —
    # see Task 2 note; keep the parquet authoritative for backtest.


if __name__ == '__main__':
    main()
```

> NOTE for the implementer: verify `_score_with_finbert`'s real return contract and `union_universe`'s real signature against the source before relying on them (grep both). They were confirmed to exist; match their actual parameters. `next_page_token` is the documented Alpaca news pagination field — confirm the exact JSON key from one live `alpaca data news` response (`--debug`) and adjust if it differs.

- [ ] **Step 4: Run the pure-helper tests**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k "daily_rows or idempotent" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add scripts/backfill_news_sentiment.py tests/strategies/test_news_sentiment.py
git commit -m "feat(backfill): append-only historical news->FinBERT sentiment backfill script"
```

---

## Task 3: Point-in-time `sentiment` panel in aux_data_loader

**Files:**
- Modify: `src/strategies/aux_data_loader.py` (add a sentiment panel mirroring the options panel)
- Test: `tests/strategies/test_news_sentiment.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/strategies/test_news_sentiment.py
from src.strategies import aux_data_loader as adl


def test_sentiment_panel_point_in_time(tmp_path, monkeypatch):
    df = pd.DataFrame([
        {'ticker': 'AAA', 'date': '2022-03-01', 'news_count_24h': 3, 'news_mean_score': 0.5,
         'news_finbert_pos': 2, 'news_finbert_neu': 1, 'news_finbert_neg': 0},
        {'ticker': 'AAA', 'date': '2022-03-05', 'news_count_24h': 2, 'news_mean_score': -0.4,
         'news_finbert_pos': 0, 'news_finbert_neu': 0, 'news_finbert_neg': 2},
    ])
    p = tmp_path / 'sentiment.parquet'; df.to_parquet(p)
    monkeypatch.setattr(adl, 'SENTIMENT_PATH', p, raising=False)
    monkeypatch.setattr(adl, '_SENT_DF', None, raising=False)
    aux = adl.load_aux_data('2022-03-03')          # between the two rows
    assert aux['sentiment']['AAA']['news_mean_score'] == 0.5   # uses 03-01, not future 03-05
    aux2 = adl.load_aux_data('2022-03-06')
    assert aux2['sentiment']['AAA']['news_mean_score'] == -0.4  # now 03-05 visible
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k panel -v`
Expected: FAIL — `KeyError: 'sentiment'` (panel not yet added)

- [ ] **Step 3: Write minimal implementation** — mirror the options panel exactly.

In `src/strategies/aux_data_loader.py`, near the other master-path constants (after `INSIDER_PATH`):
```python
SENTIMENT_PATH = ROOT / 'data' / 'master' / 'sentiment.parquet'
_SENT_DF = None
SENTIMENT_FIELDS = ['news_count_24h', 'news_mean_score', 'news_finbert_pos',
                    'news_finbert_neu', 'news_finbert_neg']
```
Add a loader + day-slice (mirror `_load_panel`/`_day_slice` — same `date <= ts` prior-fallback, NO future leakage):
```python
def _load_sentiment_panel() -> pd.DataFrame:
    global _SENT_DF
    if _SENT_DF is not None:
        return _SENT_DF
    if not SENTIMENT_PATH.exists():
        _SENT_DF = pd.DataFrame(); return _SENT_DF
    df = pd.read_parquet(SENTIMENT_PATH)
    df['date'] = pd.to_datetime(df['date'])
    _SENT_DF = df
    return df


def _sentiment_day_slice(date_str: str) -> dict[str, dict]:
    panel = _load_sentiment_panel()
    if panel.empty:
        return {}
    ts = pd.to_datetime(date_str)
    day = panel[panel['date'] <= ts]            # point-in-time: never future
    if day.empty:
        return {}
    # latest available row per ticker on/before ts
    latest = day.sort_values('date').drop_duplicates('ticker', keep='last')
    out = {}
    for row in latest.itertuples(index=False):
        d = {f: getattr(row, f) for f in SENTIMENT_FIELDS
             if hasattr(row, f) and getattr(row, f) is not None
             and not (isinstance(getattr(row, f), float) and pd.isna(getattr(row, f)))}
        if d:
            out[row.ticker] = d
    return out
```
Then in `load_aux_data(...)`, add the panel to the returned dict (alongside `'options'`):
```python
    result['sentiment'] = _sentiment_day_slice(str(current_date)[:10])
```
(Adapt to the function's actual local variable name for the result dict — read the function first and match it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k panel -v`
Expected: PASS
Run: `cd /root/openclaw && python3 -m pytest tests/ -k aux_data -v` (existing aux-loader tests still green)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/aux_data_loader.py tests/strategies/test_news_sentiment.py
git commit -m "feat(aux): point-in-time sentiment panel for backtests (mirrors options panel)"
```

---

## Task 4: Strategy ③ — S_news_sentiment_long_short

**Files:**
- Create: `src/strategies/implementations/S_news_sentiment_long_short.py`
- Create: `src/strategies/implementations/S_news_sentiment_long_short.requirements.json`
- Test: `tests/strategies/test_news_sentiment.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/strategies/test_news_sentiment.py
import numpy as np
from strategies.implementations.S_news_sentiment_long_short import NewsSentimentLongShort


def _flat_prices(tickers, n=40):
    idx = pd.bdate_range('2022-02-01', periods=n)
    return pd.DataFrame({t: np.full(n, 100.0) for t in tickers}, index=idx)


def test_sentiment_strategy_longs_positive_shorts_negative():
    s = NewsSentimentLongShort()
    aux = {'sentiment': {
        'AAA': {'news_mean_score': 0.7, 'news_count_24h': 5},
        'BBB': {'news_mean_score': -0.6, 'news_count_24h': 4},
        'CCC': {'news_mean_score': 0.05, 'news_count_24h': 5},   # too weak → no signal
    }}
    sigs = s.generate_signals(_flat_prices(['AAA', 'BBB', 'CCC']), {'state': 'LOW_VOL'},
                              ['AAA', 'BBB', 'CCC'], aux)
    dirs = {x.ticker: x.direction for x in sigs}
    assert dirs.get('AAA') == 'LONG'
    assert dirs.get('BBB') == 'SHORT'
    assert 'CCC' not in dirs


def test_sentiment_strategy_no_sentiment_no_signals():
    s = NewsSentimentLongShort()
    assert s.generate_signals(_flat_prices(['AAA']), {'state': 'LOW_VOL'}, ['AAA'], {'sentiment': {}}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k sentiment_strategy -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write minimal implementation**

```python
# src/strategies/implementations/S_news_sentiment_long_short.py
"""S_news_sentiment_long_short — sentiment-primary cross-sectional long/short.

Primary signal = FinBERT news sentiment (news_flow.score). LONG strongly-positive names,
SHORT strongly-negative names, requiring a minimum article count to suppress single-headline
noise. Reads aux_data['sentiment'] (historical backfill, point-in-time).

CAVEAT (documented in spec): backtest signal is NEWS-ONLY; the live signal additionally
includes Reddit/StockTwits social sentiment, which has no historical record to backtest.
Zero LLM tokens.
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from strategies.confirmation import news_flow as nf

INSTRUMENT_CLASS = 'equity'


class NewsSentimentLongShort(BaseStrategy):
    id = 'S_news_sentiment_long_short'
    name = 'News-Sentiment Long/Short'
    description = 'Sentiment-primary cross-sectional long/short from FinBERT news (news-only in backtest)'
    tier = 3
    signal_frequency = 'daily'
    min_lookback = 20
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    data_requirements = ['prices', 'sentiment']

    def default_parameters(self) -> dict:
        return {'min_articles': 2, 'long_thresh': 0.3, 'short_thresh': -0.3, 'max_each': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        sent = (aux_data or {}).get('sentiment', {})
        if not sent:
            return []
        p = self.parameters
        scale = self.position_scale(regime_state)

        scored = []
        for t in universe:
            if t not in prices.columns:
                continue
            sc = nf.score(sent.get(t), p)
            if sc >= p['long_thresh']:
                scored.append((t, 'LONG', sc))
            elif sc <= p['short_thresh']:
                scored.append((t, 'SHORT', sc))

        longs = sorted([x for x in scored if x[1] == 'LONG'], key=lambda x: x[2], reverse=True)[:p['max_each']]
        shorts = sorted([x for x in scored if x[1] == 'SHORT'], key=lambda x: x[2])[:p['max_each']]

        signals: List[Signal] = []
        for t, direction, sc in longs + shorts:
            ts = prices[t].dropna()
            if len(ts) < 2:
                continue
            cur = float(ts.iloc[-1])
            if cur <= 0:
                continue
            stops = self.compute_stops_and_targets(ts, direction, cur, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=cur,
                stop_loss=stops['stop'], target_1=stops['t1'],
                target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=round((1.0 / max(p['max_each'], 1)) * scale, 4),
                confidence='MED',
                signal_params={'sentiment': round(float(sc), 4),
                               'news_count': (sent.get(t) or {}).get('news_count_24h')},
            ))
        return signals[:self.MAX_SIGNALS]
```

```json
// src/strategies/implementations/S_news_sentiment_long_short.requirements.json
{
  "strategy_id": "S_news_sentiment_long_short",
  "required": ["prices", "sentiment"],
  "optional": []
}
```

- [ ] **Step 4: Run test + validate_strategy**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -k sentiment_strategy -v`
Expected: PASS (2 passed)
Run: `cd /root/openclaw && python3 src/strategies/validate_strategy.py src/strategies/implementations/S_news_sentiment_long_short.py`
Expected: JSON `{"ok": true, ...}`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/implementations/S_news_sentiment_long_short.py src/strategies/implementations/S_news_sentiment_long_short.requirements.json tests/strategies/test_news_sentiment.py
git commit -m "feat(strategy): S_news_sentiment_long_short (sentiment-primary long/short)"
```

---

## Task 5: Run the backfill (operational — the long pole)

**Files:** none (produces data; `sentiment.parquet` grows append-only).

- [ ] **Step 1: Smoke a single chunk (dry-run)**

Run:
```bash
cd /root/openclaw && python3 scripts/backfill_news_sentiment.py \
  --symbols AAPL,MSFT --start 2022-01-01 --end 2022-02-01 --dry-run 2>&1 | tail -10
```
Expected: `[dry-run] would append N (ticker,date) rows` with N > 0. If 0, debug fetch/pagination (`alpaca data news --debug`) before scaling.

- [ ] **Step 2: Backfill the active universe (background, resumable)**

Run (background — this is compute-heavy; FinBERT scores every article):
```bash
cd /root/openclaw && nohup python3 scripts/backfill_news_sentiment.py \
  --universe-active --start 2018-01-01 --end 2026-04-30 --chunk-days 30 \
  > logs/news_backfill.log 2>&1 &
```
Monitor `logs/news_backfill.log`. **Log any depth cap** if throughput forces shortening the window — no silent truncation (spec requirement).

- [ ] **Step 3: Verify depth after completion**

Run:
```bash
cd /root/openclaw && python3 -c "import pandas as pd; d=pd.read_parquet('data/master/sentiment.parquet'); d['date']=pd.to_datetime(d['date']); print('rows',len(d),'tickers',d.ticker.nunique(),'range',d.date.min(),d.date.max(),'distinct dates',d.date.nunique())"
```
Expected: multi-year range (target ≥ 2019), hundreds of tickers, thousands of dates. Record actual depth — it bounds Task 6's backtest window.

- [ ] **Step 4: Commit** the grown data file (append-only).

```bash
cd /root/openclaw
git add data/master/sentiment.parquet
git commit -m "data: backfill historical news sentiment (append-only, news-only)"
```

---

## Task 6: Backtest ③ + evaluate

**Files:** none (analysis).

- [ ] **Step 1: Backtest over the backfilled window**

Run (set `--start`/`--end` to the actual depth from Task 5 Step 3; example uses 2019-01-01):
```bash
cd /root/openclaw && python3 -m backtest.unified_backtest \
  --strategy-file src/strategies/implementations/S_news_sentiment_long_short.py \
  --start-date 2019-01-01 --end-date 2026-04-30 2>&1 | tail -5
```
Record Sharpe / MaxDD / return / trades + per-regime breakdown.

- [ ] **Step 2: Evaluate against the gate**

- Gate: **Sharpe ≥ 0.5 AND MaxDD ≤ 0.20**.
- Record verdict for Task 7. If failing, STOP and report to operator — the tuning levers are `long_thresh`/`short_thresh`/`min_articles`/`max_each`. Do NOT overfit silently; report what was tried.
- **Always report the news-only caveat** alongside the metrics: live performance will differ because live sentiment includes social sources absent from the backfill.

- [ ] **Step 3: No commit** (analysis only).

---

## Task 7: Conditional registration (only if ③ PASSED)

**Files:**
- Modify: `src/strategies/registry.py`, `src/strategies/manifest.json`

- [ ] **Step 1: Add `_IMPL_MAP` entry** (only if Task 6 passed the gate):

```python
    'S_news_sentiment_long_short': ('strategies.implementations.S_news_sentiment_long_short', 'NewsSentimentLongShort'),
```

- [ ] **Step 2: Verify it loads**

Run: `cd /root/openclaw && python3 -c "from src.strategies.registry import load_strategy_class as L; print(L('S_news_sentiment_long_short'))"`
Expected: prints the class (not None).

- [ ] **Step 3: Register as `candidate`** (same lifecycle API + signature-verification note as Plan A Task 9):

```bash
cd /root/openclaw && python3 - <<'PY'
from src.strategies.lifecycle import LifecycleStateMachine, StrategyState
lsm = LifecycleStateMachine.from_manifest('src/strategies/manifest.json')
sid = 'S_news_sentiment_long_short'
if sid not in lsm.manifest.get('strategies', {}):
    lsm.register(sid, canonical_file='S_news_sentiment_long_short.py',
                 class_name='NewsSentimentLongShort', instrument_class='equity',
                 state=StrategyState.CANDIDATE, actor='botjohn',
                 reason='news-sentiment-primary strategy — backtest passed equity gate (news-only caveat)')
lsm.save_manifest('src/strategies/manifest.json')
print('registered')
PY
```

- [ ] **Step 4: Regression**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_news_sentiment.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/registry.py src/strategies/manifest.json
git commit -m "feat(strategy): register S_news_sentiment_long_short as candidate

Backtest <Sharpe/MaxDD/trades over window>; news-only caveat noted. Operator-gated for live."
```

---

## Self-Review (completed by author)

- **Spec coverage:** §5 news_flow → Task 1. §8 backfill (append-only, T+1 no-lookahead, depth-cap logging) → Tasks 2/5. §9 aux-data extension + strategy ③ → Tasks 3/4. §10 validation (TDD, validate_strategy, backtest+per-regime, gate, operator-gated registration, news-only caveat) → Tasks 6/7.
- **Placeholder scan:** deferred items are explicit verify-against-source notes (`_score_with_finbert`/`union_universe` signatures, Alpaca pagination key, `register()` signature, the result-dict var name in `load_aux_data`) — concrete "confirm this real artifact" instructions, not invent-it gaps. Backtest window in Task 6 is parameterized on Task 5's measured depth (can't be known before the backfill runs) — this is correct, not a placeholder.
- **Type consistency:** `nf.score(sent_row, params)` matches Tasks 1/4; `articles_to_daily_rows`/`merge_append_only` match Tasks 2 tests; `SENTIMENT_PATH`/`_SENT_DF`/`SENTIMENT_FIELDS` + `load_aux_data(...)['sentiment']` match Tasks 3/4.
- **Dependency:** Task 1 imports the Plan-A `confirmation/` package; Plan B must run after Plan A Task 1 exists.
