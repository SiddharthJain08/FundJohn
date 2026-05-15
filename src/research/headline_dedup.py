"""Phase 2C — Jaccard headline dedup.

Concept-lifted from FinceptTerminal NewsClusterService (clean-room
reimplementation; no AGPL code copied).  Drops near-duplicate headlines
within a configurable time window using Jaccard similarity over a tokenized
+ stopword-stripped title.

Threshold semantics (read carefully): the ``threshold`` parameter encodes
*looseness* — a higher value is more lenient and dedups fewer items.  The
match condition is::

    jaccard(tokens_a, tokens_b) >= 1.0 - threshold

So with the default ``threshold=0.25`` an item is dropped when token overlap
is ``>= 0.75``.  Pass ``same_category_threshold=0.20`` (tighter) when both
items share a source category to avoid over-collapsing legitimate
intra-category coverage; this raises the trigger to ``>= 0.80``.
"""
from __future__ import annotations

import re
from datetime import timedelta, timezone

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "by", "as", "at", "from", "is", "are", "was", "were", "be",
})
# Match Unicode word characters (so CJK/Cyrillic/accented Latin survive
# tokenization instead of collapsing to empty sets) AND preserve standalone
# '+' / '-' tokens (so 'Apple +5%' and 'Apple -5%' — a beat vs a miss —
# don't dedup to one survivor).
_TOKEN_RE = re.compile(r"\w+|[+\-]", re.UNICODE)


def tokenize(headline: str) -> set[str]:
    """Lowercase, regex-extract Unicode word tokens (plus bare +/- signs),
    drop stopwords."""
    return {t for t in _TOKEN_RE.findall(headline.lower()) if t not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    """Set-Jaccard.  Empty-empty returns 0.0 ('no information to compare ->
    distinct') rather than 1.0 — defends against headlines that tokenize to
    empty (CJK with old regex, bad-encoding, tag-only, etc.) silently
    colliding with each other."""
    if not a and not b:
        return 0.0
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
    """Returns items with near-duplicates removed.

    Earlier item wins; later duplicate is dropped.  Items must carry the
    keys ``id``, ``title``, ``source``, and ``ts`` (a timezone-aware
    ``datetime``).

    Optional ``category`` field tightens the threshold when two items share
    a category (e.g. both ``'wire-service'``), preventing over-collapse of
    legitimate independent coverage.

    Naive ``ts`` values are normalized to UTC at the boundary (so callers
    passing ``datetime.utcnow()`` from RSS feeds don't hit a cryptic
    ``TypeError`` on the first window check).  Caller's input list is not
    mutated — items with naive timestamps are shallow-copied.
    """
    # Normalize naive ts to UTC at the boundary; don't mutate caller's items.
    normalized = []
    for it in items:
        if it["ts"].tzinfo is None:
            it = {**it, "ts": it["ts"].replace(tzinfo=timezone.utc)}
        normalized.append(it)

    items_sorted = sorted(normalized, key=lambda x: x["ts"])
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
