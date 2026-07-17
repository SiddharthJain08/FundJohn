"""tests/test_news_finbert_scorer.py"""
from __future__ import annotations
from unittest.mock import patch, MagicMock


NEWS_ROWS = [
    {'ticker': 'AAPL', 'headline': 'Apple beats earnings', 'summary': '',
     'published_at': '2026-05-20T08:00:00Z'},
    {'ticker': 'AAPL', 'headline': 'Apple recalls iPhone batteries', 'summary': '',
     'published_at': '2026-05-20T09:00:00Z'},
    {'ticker': 'AAPL', 'headline': 'Apple to launch new product', 'summary': '',
     'published_at': '2026-05-20T10:00:00Z'},
    {'ticker': 'TSLA', 'headline': 'Tesla recalls 200k vehicles', 'summary': '',
     'published_at': '2026-05-20T07:00:00Z'},
]


# FinBERT mock: returns positive for "beats" + "launch", negative for "recall",
# neutral otherwise.
def mock_finbert_score(text):
    text_l = text.lower()
    if 'recall' in text_l:
        return {'label': 'Negative', 'score': 0.92}
    if 'beats' in text_l or 'launch' in text_l:
        return {'label': 'Positive', 'score': 0.85}
    return {'label': 'Neutral', 'score': 0.60}


def test_scorer_aggregates_per_ticker():
    from src.ingestion.news_finbert_scorer import score_news_rows
    with patch('src.ingestion.news_finbert_scorer.FinbertClient') as MC:
        MC.return_value.score = MagicMock(side_effect=mock_finbert_score)
        result = score_news_rows(NEWS_ROWS)
    aapl = next(r for r in result if r['ticker'] == 'AAPL')
    assert aapl['news_count_24h']     == 3
    assert aapl['news_finbert_pos']   == 2  # "beats" + "launch"
    assert aapl['news_finbert_neg']   == 1  # "recall"
    assert aapl['news_finbert_neu']   == 0
    # signed mean: (2 * +0.85 + 1 * -0.92 + 0 * 0) / 3 ≈ +0.26
    assert abs(aapl['news_mean_score'] - ((2*0.85 - 0.92) / 3)) < 1e-6
    # top headlines: highest |score| first → recall (|0.92|), then beats/launch (|0.85|)
    assert aapl['news_top_headlines'][0].startswith('Apple recalls')


def test_scorer_handles_finbert_error_returns_zeros():
    from src.ingestion.news_finbert_scorer import score_news_rows
    with patch('src.ingestion.news_finbert_scorer.FinbertClient') as MC:
        MC.return_value.score = MagicMock(side_effect=RuntimeError('service down'))
        result = score_news_rows(NEWS_ROWS)
    # On error, every ticker gets zeros + None mean
    for r in result:
        assert r['news_count_24h']    == 0
        assert r['news_finbert_pos']  == 0
        assert r['news_mean_score'] is None


def test_scorer_returns_empty_list_when_no_news():
    from src.ingestion.news_finbert_scorer import score_news_rows
    with patch('src.ingestion.news_finbert_scorer.FinbertClient') as MC:
        MC.return_value.score = MagicMock(return_value={'label': 'Neutral', 'score': 0.5})
        assert score_news_rows([]) == []
