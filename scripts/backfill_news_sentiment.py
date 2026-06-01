"""Backfill historical news -> FinBERT -> per-(ticker,date) sentiment (append-only).

Pages Alpaca/Benzinga news per symbol-chunk over a date window, scores headlines with the
local FinBERT service (reusing src/ingestion/alpaca_news helpers), aggregates per ticker per
calendar day, and APPENDS new (ticker,date) rows to data/master/sentiment.parquet. Never
overwrites existing rows (honors NEVER-DELETE). Resumable.

Usage:
  python3 scripts/backfill_news_sentiment.py --start 2018-01-01 --end 2024-12-31 \
      [--symbols AAPL,MSFT | --universe-active] [--chunk-days 30] [--dry-run]
"""
from __future__ import annotations
import argparse, json, subprocess, sys, os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SENT_PATH = ROOT / 'data' / 'master' / 'sentiment.parquet'
ALPACA = '/root/go/bin/alpaca'

NEWS_COLS = ['ticker', 'date', 'news_count_24h', 'news_finbert_pos', 'news_finbert_neu',
             'news_finbert_neg', 'news_mean_score', 'news_top_headlines']


def articles_to_daily_rows(articles: list[dict]) -> list[dict]:
    """Group scored articles by (symbol, published-date) -> daily sentiment rows.
    Each article must already carry finbert_label/finbert_score."""
    groups: dict[tuple, list] = defaultdict(list)
    for a in articles:
        # Step A correction: live Alpaca articles carry `created_at`, not
        # `published_at`. The pure-helper tests pass `published_at` explicitly,
        # so prefer it when present and fall back to `created_at` for real data.
        d = (a.get('published_at') or a.get('created_at') or '')[:10]
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
    """Append only (ticker,date) rows not already present. Existing rows win.

    Normalizes `date` to datetime64 on BOTH sides before concat so the written parquet has a
    uniform dtype. Mixing new string dates ('2022-03-01') with the existing file's
    date/Timestamp objects produces an `object` column that pyarrow refuses to serialize
    (ArrowTypeError on `to_parquet`) — the bug that crashed the first backfill run.
    """
    if new is None or new.empty:
        return existing if (existing is not None and not existing.empty) else new
    new = new.copy()
    new['date'] = pd.to_datetime(new['date'])
    if existing is None or existing.empty:
        return new.drop_duplicates(['ticker', 'date'])
    existing = existing.copy()
    existing['date'] = pd.to_datetime(existing['date'])
    key = ['ticker', 'date']
    have = set(map(tuple, existing[key].astype(str).values.tolist()))
    fresh = new[~new[key].astype(str).apply(tuple, axis=1).isin(have)]
    return pd.concat([existing, fresh], ignore_index=True)


_SIGN = {'positive': 1, 'neutral': 0, 'negative': -1}


def _article_text(a: dict) -> str:
    """Identical text construction to alpaca_news._score_with_finbert (parity)."""
    headline = (a.get('headline') or '').strip()
    summary = (a.get('summary') or '')[:500]
    return (headline + ' ' + summary).strip() if summary else headline


def score_articles(articles: list[dict], batch_size: int = 64) -> list[dict]:
    """Score articles via the FinBERT BATCH endpoint (~5x faster, bit-identical scores);
    fall back to the live serial scorer if /score_batch isn't deployed yet (404) or errors.

    Sets each article's `finbert_label` (lowercase) + signed `finbert_score`, exactly matching
    src.ingestion.alpaca_news._score_with_finbert (same text, same sign map) — so the backfilled
    sentiment is identical to what the live pipeline produces, just produced faster.
    """
    if not articles:
        return articles
    from src.services.finbert.client import FinbertClient
    client = FinbertClient(base_url=os.environ.get('OPENCLAW_FINBERT_URL', 'http://127.0.0.1:7872'),
                           timeout=30.0)
    texts = [_article_text(a) for a in articles]
    try:
        results: list[dict] = []
        for j in range(0, len(texts), batch_size):
            results.extend(client.score_batch(texts[j:j + batch_size]))
        for a, txt, res in zip(articles, texts, results):
            if not txt:
                a['finbert_label'], a['finbert_score'] = 'neutral', 0.0
                continue
            label = (res.get('label') or 'Neutral').lower()
            a['finbert_label'] = label
            a['finbert_score'] = _SIGN.get(label, 0) * float(res.get('score', 0.0))
        return articles
    except Exception as e:
        # /score_batch unavailable (service predates it) or transient error → serial fallback.
        print(f'[backfill] batch scoring unavailable ({e}); falling back to serial scorer', file=sys.stderr)
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
            print(f'[backfill] alpaca news rc={res.returncode} stderr={res.stderr.strip()[:200]}',
                  file=sys.stderr)
            break
        data = json.loads(res.stdout or '{}')
        out.extend(data.get('news', []) or [])
        token = data.get('next_page_token')   # Step A confirmed: key is `next_page_token`
        if not token:
            break
    return out


def _active_universe() -> list[str]:
    # Step A correction: `union_universe` is a METHOD on UniverseResolver, not a
    # module-level function, and takes a datetime.date `as_of` + a tuple of states.
    # Construct the resolver exactly as the resolver's own __main__ block does.
    from datetime import date as _date
    from src.strategies.universe_resolver import UniverseResolver
    from src.strategies._db_adapters import PostgresMetadataDB, ParquetCoverage
    db = PostgresMetadataDB(os.environ['POSTGRES_URI'])
    cov = ParquetCoverage()
    def manifest_loader():
        with open('/root/openclaw/src/strategies/manifest.json') as f:
            return json.load(f)
    resolver = UniverseResolver(db=db, coverage=cov, manifest_loader=manifest_loader)
    return sorted(resolver.union_universe(as_of=_date.today(), states=('live', 'candidate')))


def _flush(new_rows: list[dict]) -> tuple[int, int]:
    """Append-only merge `new_rows` into SENT_PATH immediately. Returns (before, after).

    Called per-window so a long backfill is crash-safe, resumable (existing (ticker,date)
    rows win on re-run), and yields usable partial depth before the full run completes.
    """
    existing = pd.read_parquet(SENT_PATH) if SENT_PATH.exists() else pd.DataFrame()
    before = len(existing)
    if not new_rows:
        return before, before
    new_df = pd.DataFrame(new_rows, columns=NEWS_COLS).drop_duplicates(['ticker', 'date'])
    merged = merge_append_only(existing, new_df)
    # Atomic write: a daily-pipeline reader (aux_data_loader._load_sentiment_panel) may read
    # SENT_PATH mid-backfill — write to a temp file then rename so it never sees a partial parquet.
    tmp = SENT_PATH.with_suffix('.parquet.tmp')
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, SENT_PATH)
    return before, len(merged)


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
    dry_total = 0
    for i in range(len(windows)):
        w_start = windows[i].strftime('%Y-%m-%dT00:00:00Z')
        w_end = (windows[i + 1] if i + 1 < len(windows) else pd.Timestamp(args.end)).strftime('%Y-%m-%dT23:59:59Z')
        # Collect this window's rows across symbol-chunks, then flush incrementally.
        # Fetch is the bottleneck (Alpaca pagination, ~12.5 min/window serial). Fetch the
        # symbol-chunks CONCURRENTLY (pagination within a chunk stays serial → max ~4x), then
        # score all articles in one batched pass. This is the actual wall-clock lever, not
        # FinBERT batching (scoring wasn't the dominant cost for the multi-symbol case).
        chunks = [symbols[j:j + 50] for j in range(0, len(symbols), 50)]
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
            fetched = list(ex.map(lambda c: _fetch_window(c, w_start, w_end), chunks))
        all_arts = [a for lst in fetched for a in (lst or [])]
        window_rows: list[dict] = articles_to_daily_rows(score_articles(all_arts)) if all_arts else []
        if args.dry_run:
            uniq = (len(pd.DataFrame(window_rows, columns=NEWS_COLS).drop_duplicates(['ticker', 'date']))
                    if window_rows else 0)
            dry_total += uniq
            print(f'[dry-run] window {w_start[:10]}: +{uniq} (cum {dry_total})', file=sys.stderr)
        else:
            before, after = _flush(window_rows)
            print(f'[backfill] window {w_start[:10]} flushed: {before} -> {after} rows', file=sys.stderr)

    if args.dry_run:
        print(f'[dry-run] would append ~{dry_total} (ticker,date) rows total', file=sys.stderr)


if __name__ == '__main__':
    main()
