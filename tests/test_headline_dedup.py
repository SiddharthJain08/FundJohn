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
