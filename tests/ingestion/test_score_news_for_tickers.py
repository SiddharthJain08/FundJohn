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
    cursor = MagicMock()
    # 5-tuples: (primary_ticker, related_tickers, title, summary, uuid)
    cursor.fetchall.return_value = [
        ('GLW', [], 'CFO departs', '', 'u1'),
        ('GLW', [], 'Guidance cut', '', 'u2'),
    ]
    cursor.description = [('primary_ticker',), ('related_tickers',), ('title',), ('summary',), ('uuid',)]
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
    cursor.description = [('primary_ticker',), ('related_tickers',), ('title',), ('summary',), ('uuid',)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_connect.return_value.__enter__.return_value = conn

    out = score_news_for_tickers(['NONESUCH'], datetime.now(timezone.utc))
    assert out == []
    mock_score.assert_not_called()


@patch('src.ingestion.news_finbert_scorer.psycopg2.connect')
@patch('src.ingestion.news_finbert_scorer.score_news_rows')
def test_related_tickers_match_is_attributed_to_queried_ticker(
    mock_score, mock_connect, monkeypatch,
):
    """A row whose primary_ticker is SPY but related_tickers includes GLW
    should count as GLW news when GLW was queried."""
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake')
    from datetime import datetime, timezone
    since = datetime(2026, 5, 27, 22, 0, tzinfo=timezone.utc)
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ('SPY', ['GLW', 'IWM'], 'Industrial slowdown: GLW guidance cut', '', 'u-related'),
        ('GLW', ['SPY'],        'GLW CFO departs',                       '', 'u-primary'),
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_connect.return_value.__enter__.return_value = conn
    mock_score.return_value = [{
        'ticker': 'GLW', 'news_count_24h': 2,
        'news_finbert_neg': 1.0, 'news_finbert_pos': 0.0,
        'news_finbert_neu': 0.0, 'news_mean_score': -0.9,
        'news_top_headlines': ['Industrial slowdown: GLW guidance cut', 'GLW CFO departs'],
    }]
    out = score_news_for_tickers(['GLW'], since)

    # Both rows should be passed to score_news_rows as GLW
    passed_rows = mock_score.call_args[0][0]
    tickers_passed = [r['ticker'] for r in passed_rows]
    assert tickers_passed == ['GLW', 'GLW']

    # Both uuids should be attached to GLW's evidence
    assert sorted(out[0]['evidence_uuids']) == ['u-primary', 'u-related']
