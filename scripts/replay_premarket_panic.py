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
