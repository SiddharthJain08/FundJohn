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
