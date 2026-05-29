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
    # Chunk windows overlap on the shared boundary day, so boundary-day articles
    # can be fetched in two adjacent windows -> duplicate (ticker,date) rows.
    new_df = new_df.drop_duplicates(['ticker', 'date'])
    if args.dry_run:
        print(f'[dry-run] would append {len(new_df)} (ticker,date) rows', file=sys.stderr)
        return
    existing = pd.read_parquet(SENT_PATH) if SENT_PATH.exists() else pd.DataFrame()
    merged = merge_append_only(existing, new_df)
    merged.to_parquet(SENT_PATH, index=False)
    print(f'[backfill] sentiment.parquet rows {len(existing)} -> {len(merged)}', file=sys.stderr)


if __name__ == '__main__':
    main()
