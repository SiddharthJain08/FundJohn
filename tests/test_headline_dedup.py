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


def test_same_category_threshold_tightens_dedup():
    """When two items share a category, threshold tightens to same_category_threshold.
    Default 0.25 fires at jaccard >= 0.75; same-cat 0.20 fires at >= 0.80.
    Construct headlines whose jaccard lands in the gap (0.75, 0.80):
      tokens(a) = {alpha, beta, gamma, delta, epsilon, zeta, eta, unique1}  (8)
      tokens(b) = {alpha, beta, gamma, delta, epsilon, zeta, eta, unique2}  (8)
      intersect = 7, union = 9, jaccard = 7/9 ≈ 0.778
      → 0.778 >= 0.75 (default fires → dedup)
      → 0.778 <  0.80 (same-cat does NOT fire → keep both)"""
    from src.research.headline_dedup import dedup_within_window
    now = datetime.now(tz=timezone.utc)
    items = [
        {"id": "a",
         "title": "alpha beta gamma delta epsilon zeta eta unique1",
         "source": "reuters", "category": "wire-service", "ts": now},
        {"id": "b",
         "title": "alpha beta gamma delta epsilon zeta eta unique2",
         "source": "bloomberg", "category": "wire-service",
         "ts": now + timedelta(hours=1)},
    ]
    # With shared category: tighter 0.20 threshold does NOT fire at 0.778 → keep both.
    kept = dedup_within_window(items, threshold=0.25, same_category_threshold=0.20)
    assert {k["id"] for k in kept} == {"a", "b"}, \
        f"same-category threshold should keep both at jaccard 0.778; got {[k['id'] for k in kept]}"

    # Without shared category: default 0.25 threshold fires at 0.778 → drop b.
    items_no_cat = [{**it, "category": None} for it in items]
    kept_no_cat = dedup_within_window(items_no_cat, threshold=0.25, same_category_threshold=0.20)
    assert {k["id"] for k in kept_no_cat} == {"a"}, \
        f"default threshold should drop b at jaccard 0.778; got {[k['id'] for k in kept_no_cat]}"


def test_dedup_handles_naive_datetimes():
    """Caller passes naive datetime — function normalizes to UTC at the boundary,
    does not crash, and does not mutate the caller's input."""
    from src.research.headline_dedup import dedup_within_window
    now_naive = datetime(2026, 5, 15, 12, 0, 0)  # no tzinfo
    items = [
        {"id": "a", "title": "Apple beats Q1 earnings", "source": "reuters", "ts": now_naive},
        {"id": "b", "title": "Apple beats Q1 earnings", "source": "bloomberg",
         "ts": now_naive + timedelta(hours=1)},
    ]
    kept = dedup_within_window(items)
    assert {k["id"] for k in kept} == {"a"}
    # Caller's items NOT mutated:
    assert items[0]["ts"].tzinfo is None
    assert items[1]["ts"].tzinfo is None


def test_signed_percentages_do_not_collapse():
    """'Apple +5%' and 'Apple -5%' should NOT dedup (a beat and a miss
    are not the same headline)."""
    from src.research.headline_dedup import dedup_within_window, tokenize

    a_toks = tokenize("Apple +5%")
    b_toks = tokenize("Apple -5%")
    assert a_toks != b_toks, f"signed percentages collapsed: {a_toks} == {b_toks}"

    now = datetime.now(tz=timezone.utc)
    items = [
        {"id": "beat", "title": "Apple +5%", "source": "reuters", "ts": now},
        {"id": "miss", "title": "Apple -5%", "source": "bloomberg",
         "ts": now + timedelta(hours=1)},
    ]
    kept = dedup_within_window(items)
    assert {k["id"] for k in kept} == {"beat", "miss"}


def test_cjk_headlines_do_not_all_collapse():
    """CJK headlines tokenize to a single CJK substring each — distinct ones
    must not dedup against each other (the old [a-z0-9]+ regex would have
    produced empty sets and collapsed them all)."""
    from src.research.headline_dedup import dedup_within_window
    now = datetime.now(tz=timezone.utc)
    items = [
        {"id": "toyota", "title": "トヨタ自動車の決算発表",  "source": "nikkei",  "ts": now},
        {"id": "honda",  "title": "ホンダの新型車発表",       "source": "asahi",   "ts": now + timedelta(hours=1)},
        {"id": "sony",   "title": "ソニーグループの業績予想", "source": "reuters", "ts": now + timedelta(hours=2)},
    ]
    kept = dedup_within_window(items)
    # All three should survive — they're distinct CJK headlines.
    assert {k["id"] for k in kept} == {"toyota", "honda", "sony"}
