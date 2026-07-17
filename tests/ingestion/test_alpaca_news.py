"""SP-1: tests for alpaca_news ingestion + FinBERT scoring + DB write."""
import json
from unittest.mock import patch, MagicMock
import pytest

from src.ingestion.alpaca_news import (
    ingest_alpaca_news,
    _chunk_symbols,
    _aggregate_per_ticker,
    _score_with_finbert,
)


def test_chunk_symbols_50_per_call():
    syms = [f'TKR{i}' for i in range(120)]
    chunks = list(_chunk_symbols(syms, chunk_size=50))
    assert [len(c) for c in chunks] == [50, 50, 20]


def test_aggregate_per_ticker_counts_multi_attribution():
    """An article tagged with [AAPL, MSFT] counts once for each ticker."""
    articles = [
        {'id': 1, 'symbols': ['AAPL', 'MSFT'], 'finbert_score': 0.4, 'finbert_label': 'positive', 'headline': 'A'},
        {'id': 2, 'symbols': ['AAPL'],         'finbert_score': -0.2, 'finbert_label': 'negative', 'headline': 'B'},
        {'id': 3, 'symbols': ['MSFT'],         'finbert_score': 0.0, 'finbert_label': 'neutral',  'headline': 'C'},
    ]
    agg = _aggregate_per_ticker(articles)
    assert agg['AAPL']['count_24h'] == 2
    assert agg['AAPL']['finbert_pos'] == 1
    assert agg['AAPL']['finbert_neg'] == 1
    assert agg['AAPL']['mean_score'] == pytest.approx((0.4 + -0.2) / 2)
    assert agg['MSFT']['count_24h'] == 2
    assert agg['MSFT']['finbert_neu'] == 1


def test_top_headlines_keeps_top_3_by_abs_score():
    articles = [
        {'symbols': ['AAPL'], 'finbert_score': 0.9, 'headline': 'big positive'},
        {'symbols': ['AAPL'], 'finbert_score': -0.7, 'headline': 'big negative'},
        {'symbols': ['AAPL'], 'finbert_score': 0.05, 'headline': 'neutral filler'},
        {'symbols': ['AAPL'], 'finbert_score': 0.4, 'headline': 'medium positive'},
    ]
    agg = _aggregate_per_ticker(articles)
    headlines = agg['AAPL']['top_headlines']
    assert len(headlines) == 3
    assert headlines[0]['headline'] == 'big positive'
    assert headlines[1]['headline'] == 'big negative'
    assert headlines[2]['headline'] == 'medium positive'


def test_empty_response_writes_zero_count_row():
    """Empty news response still writes a row (zero counts, not skipped)."""
    with patch('src.ingestion.alpaca_news._fetch_news_chunk', return_value=[]), \
         patch('src.ingestion.alpaca_news._score_with_finbert', side_effect=lambda x: x), \
         patch('src.ingestion.alpaca_news._upsert_ticker_sentiment') as mock_upsert:
        ingest_alpaca_news(symbols=['AAPL'])
        mock_upsert.assert_called_once()
        rows = mock_upsert.call_args[0][0]
        assert len(rows) == 1
        assert rows[0]['alpaca_news_count_24h'] == 0


def test_rate_limit_429_retries_then_degrades():
    """3 consecutive 429s → graceful skip (returns empty list, logs warning)."""
    from src.ingestion.alpaca_news import _fetch_news_chunk
    with patch('src.ingestion.alpaca_news._call_alpaca_cli') as mock_cli, \
         patch('src.ingestion.alpaca_news.time') as mock_time:
        mock_cli.side_effect = [
            RuntimeError('429 rate limit'),
            RuntimeError('429 rate limit'),
            RuntimeError('429 rate limit'),
        ]
        out = _fetch_news_chunk(['AAPL'], start='2026-05-20T00:00:00Z')
        assert out == []


def test_fetch_news_chunk_paginates_until_token_exhausted():
    """A chunk with more than one page accumulates ALL pages via next_page_token.

    The live ingest previously took only the first --limit 50 page and dropped the
    rest, starving busy 50-symbol chunks (live ~80 tickers/day vs the paginating
    backfill's ~187). It must now follow next_page_token like the backfill does.
    """
    from src.ingestion.alpaca_news import _fetch_news_chunk
    page1 = json.dumps({'news': [{'id': 1, 'symbols': ['AAPL']},
                                 {'id': 2, 'symbols': ['MSFT']}],
                        'next_page_token': 'PAGE2'})
    page2 = json.dumps({'news': [{'id': 3, 'symbols': ['NVDA']}],
                        'next_page_token': ''})
    with patch('src.ingestion.alpaca_news._call_alpaca_cli') as mock_cli:
        mock_cli.side_effect = [page1, page2]
        out = _fetch_news_chunk(['AAPL', 'MSFT', 'NVDA'], start='2026-05-29T00:00:00Z')
    assert [a['id'] for a in out] == [1, 2, 3]
    # second call must carry the page token from page 1
    second_args = mock_cli.call_args_list[1][0][0]
    assert '--page-token' in second_args
    assert second_args[second_args.index('--page-token') + 1] == 'PAGE2'


def test_fetch_news_chunk_single_page_makes_one_call():
    """Empty next_page_token on the first page → exactly one CLI call (no over-fetch)."""
    from src.ingestion.alpaca_news import _fetch_news_chunk
    page1 = json.dumps({'news': [{'id': 1, 'symbols': ['AAPL']}], 'next_page_token': ''})
    with patch('src.ingestion.alpaca_news._call_alpaca_cli') as mock_cli:
        mock_cli.side_effect = [page1]
        out = _fetch_news_chunk(['AAPL'], start='2026-05-29T00:00:00Z')
    assert [a['id'] for a in out] == [1]
    assert mock_cli.call_count == 1


def test_fetch_news_chunk_caps_pages():
    """A never-ending token stream is capped (no infinite loop) but still returns data."""
    from src.ingestion.alpaca_news import _fetch_news_chunk
    forever = json.dumps({'news': [{'id': 9, 'symbols': ['AAPL']}], 'next_page_token': 'MORE'})
    with patch('src.ingestion.alpaca_news._call_alpaca_cli') as mock_cli:
        mock_cli.side_effect = [forever] * 100
        out = _fetch_news_chunk(['AAPL'], start='2026-05-29T00:00:00Z')
    assert len(out) >= 1
    assert mock_cli.call_count <= 25  # bounded by max_pages safety cap


def test_score_with_finbert_label_lowercased_and_signed():
    """The FinBERT adapter must lowercase 'Positive' → 'positive' and apply signed polarity."""
    with patch('src.services.finbert.client.FinbertClient') as MockClient:
        # Mock returns capitalized label + unsigned score
        instance = MockClient.return_value
        instance.score.return_value = {'label': 'Positive', 'score': 0.8}
        articles = [{'headline': 'Up', 'summary': ''}]
        result = _score_with_finbert(articles)
    assert result[0]['finbert_label'] == 'positive'
    assert result[0]['finbert_score'] == pytest.approx(0.8)


def test_score_with_finbert_negative_label_inverts_sign():
    """A negative label produces a negative signed score."""
    with patch('src.services.finbert.client.FinbertClient') as MockClient:
        instance = MockClient.return_value
        instance.score.return_value = {'label': 'Negative', 'score': 0.7}
        result = _score_with_finbert([{'headline': 'Down', 'summary': ''}])
    assert result[0]['finbert_label'] == 'negative'
    assert result[0]['finbert_score'] == pytest.approx(-0.7)
