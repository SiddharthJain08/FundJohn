"""Tests for src/execution/sentiment_aux.build_sentiment_aux.

The live engine must reproduce the backtest aux_data_loader._sentiment_day_slice
semantics exactly (point-in-time, latest news-bearing row per ticker, 7-day
staleness cap), remapping live alpaca_news_* columns to the news_* dict keys the
strategy/scorer expect.
"""
from datetime import date

from src.execution.sentiment_aux import build_sentiment_aux


def _row(ticker, d, count, mean, pos=0, neu=0, neg=0):
    return {
        'ticker': ticker, 'date': d,
        'alpaca_news_count_24h': count,
        'alpaca_news_mean_score': mean,
        'alpaca_news_finbert_pos': pos,
        'alpaca_news_finbert_neu': neu,
        'alpaca_news_finbert_neg': neg,
    }


def test_remaps_alpaca_columns_to_news_keys():
    rows = [_row('AAPL', date(2026, 5, 29), 4, 0.42, pos=3, neu=0, neg=1)]
    out = build_sentiment_aux(rows, as_of=date(2026, 5, 29))
    assert out['AAPL'] == {
        'news_count_24h': 4,
        'news_mean_score': 0.42,
        'news_finbert_pos': 3,
        'news_finbert_neu': 0,
        'news_finbert_neg': 1,
    }


def test_forward_fill_skips_zero_count_today_keeps_older_news():
    """Live writes a count=0 row for every symbol every day. That row must NOT
    shadow an older news-bearing row within the staleness window."""
    rows = [
        _row('MSFT', date(2026, 5, 26), 5, 0.3),   # news 3 days ago
        _row('MSFT', date(2026, 5, 29), 0, None),  # today, no news
    ]
    out = build_sentiment_aux(rows, as_of=date(2026, 5, 29))
    assert out['MSFT']['news_count_24h'] == 5
    assert out['MSFT']['news_mean_score'] == 0.3


def test_latest_news_bearing_row_wins():
    rows = [
        _row('NVDA', date(2026, 5, 25), 2, 0.1),
        _row('NVDA', date(2026, 5, 28), 6, -0.5),
    ]
    out = build_sentiment_aux(rows, as_of=date(2026, 5, 29))
    assert out['NVDA']['news_mean_score'] == -0.5
    assert out['NVDA']['news_count_24h'] == 6


def test_staleness_cap_drops_rows_older_than_max_age():
    rows = [_row('TSLA', date(2026, 5, 21), 3, 0.9)]  # 8 days before as_of
    out = build_sentiment_aux(rows, as_of=date(2026, 5, 29), max_age_days=7)
    assert 'TSLA' not in out


def test_staleness_cap_keeps_rows_at_the_boundary():
    rows = [_row('TSLA', date(2026, 5, 22), 3, 0.9)]  # exactly 7 days before
    out = build_sentiment_aux(rows, as_of=date(2026, 5, 29), max_age_days=7)
    assert out['TSLA']['news_count_24h'] == 3


def test_ignores_future_rows():
    rows = [_row('AMZN', date(2026, 5, 31), 4, 0.7)]  # after as_of
    out = build_sentiment_aux(rows, as_of=date(2026, 5, 29))
    assert 'AMZN' not in out


def test_accepts_iso_string_dates():
    rows = [_row('AAPL', '2026-05-29', 4, 0.42)]
    out = build_sentiment_aux(rows, as_of='2026-05-29')
    assert out['AAPL']['news_count_24h'] == 4


def test_empty_and_all_zero_inputs():
    assert build_sentiment_aux([], as_of=date(2026, 5, 29)) == {}
    rows = [_row('AAPL', date(2026, 5, 29), 0, None)]
    assert build_sentiment_aux(rows, as_of=date(2026, 5, 29)) == {}
