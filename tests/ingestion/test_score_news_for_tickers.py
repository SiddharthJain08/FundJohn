# tests/ingestion/test_score_news_for_tickers.py
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from src.ingestion.news_finbert_scorer import score_news_for_tickers

_FAKE_DSN = 'postgresql://test:test@localhost/test'


@patch.dict('os.environ', {'POSTGRES_URI': _FAKE_DSN})
@patch('src.ingestion.news_finbert_scorer.psycopg2.connect')
@patch('src.ingestion.news_finbert_scorer.score_news_rows')
def test_score_news_for_tickers_filters_by_ticker_and_window(
    mock_score, mock_connect
):
    since = datetime(2026, 5, 27, 22, 0, tzinfo=timezone.utc)
    fake_rows = [
        {'ticker': 'GLW', 'headline': 'CFO departs', 'summary': '', 'uuid': 'u1'},
        {'ticker': 'GLW', 'headline': 'Guidance cut', 'summary': '', 'uuid': 'u2'},
    ]
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (r['ticker'], r['headline'], r['summary'], r['uuid']) for r in fake_rows
    ]
    cursor.description = [('ticker',), ('headline',), ('summary',), ('uuid',)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_connect.return_value.__enter__.return_value = conn

    mock_score.return_value = [{
        'ticker': 'GLW',
        'news_count_24h': 2,
        'news_finbert_pos': 0.0,
        'news_finbert_neu': 0.0,
        'news_finbert_neg': 1.0,
        'news_mean_score': -0.9,
        'news_top_headlines': ['CFO departs', 'Guidance cut'],
    }]

    out = score_news_for_tickers(['GLW'], since)

    executed_sql, executed_args = cursor.execute.call_args[0]
    assert 'primary_ticker' in executed_sql
    assert 'related_tickers' in executed_sql
    # SQL params are (tickers, tickers, since_ts) → indices 0, 1, 2
    assert executed_args[0] == ['GLW']
    assert executed_args[2] == since

    assert len(out) == 1
    assert out[0]['ticker'] == 'GLW'
    assert out[0]['news_count_24h'] == 2
    assert out[0]['evidence_uuids'] == ['u1', 'u2']


@patch.dict('os.environ', {'POSTGRES_URI': _FAKE_DSN})
@patch('src.ingestion.news_finbert_scorer.psycopg2.connect')
@patch('src.ingestion.news_finbert_scorer.score_news_rows')
def test_score_news_for_tickers_empty_returns_empty_list(mock_score, mock_connect):
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.description = [('ticker',), ('headline',), ('summary',), ('uuid',)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_connect.return_value.__enter__.return_value = conn

    out = score_news_for_tickers(['NONESUCH'], datetime.now(timezone.utc))
    assert out == []
    mock_score.assert_not_called()
