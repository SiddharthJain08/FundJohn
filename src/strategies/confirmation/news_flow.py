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
