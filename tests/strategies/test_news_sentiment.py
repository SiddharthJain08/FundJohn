import pandas as pd
import importlib.util, pathlib
_spec = importlib.util.spec_from_file_location(
    'backfill_news_sentiment',
    str(pathlib.Path(__file__).resolve().parents[2] / 'scripts' / 'backfill_news_sentiment.py'))
bns = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bns)


def test_articles_to_daily_rows_groups_by_ticker_date():
    articles = [
        {'symbols': ['AAA'], 'published_at': '2022-03-01T14:00:00Z', 'finbert_label': 'positive', 'finbert_score': 0.8, 'headline': 'h1'},
        {'symbols': ['AAA'], 'published_at': '2022-03-01T18:00:00Z', 'finbert_label': 'negative', 'finbert_score': -0.4, 'headline': 'h2'},
        {'symbols': ['BBB'], 'published_at': '2022-03-02T10:00:00Z', 'finbert_label': 'positive', 'finbert_score': 0.5, 'headline': 'h3'},
    ]
    rows = bns.articles_to_daily_rows(articles)
    aaa = [r for r in rows if r['ticker'] == 'AAA' and r['date'] == '2022-03-01'][0]
    assert aaa['news_count_24h'] == 2
    assert aaa['news_finbert_pos'] == 1 and aaa['news_finbert_neg'] == 1
    assert abs(aaa['news_mean_score'] - 0.2) < 1e-9


def test_merge_append_only_is_idempotent():
    existing = pd.DataFrame([{'ticker': 'AAA', 'date': '2022-03-01', 'news_count_24h': 2, 'news_mean_score': 0.2}])
    new = pd.DataFrame([
        {'ticker': 'AAA', 'date': '2022-03-01', 'news_count_24h': 99, 'news_mean_score': 9.9},
        {'ticker': 'BBB', 'date': '2022-03-02', 'news_count_24h': 1, 'news_mean_score': 0.5},
    ])
    merged = bns.merge_append_only(existing, new)
    aaa = merged[(merged.ticker == 'AAA') & (merged.date == '2022-03-01')].iloc[0]
    assert aaa['news_count_24h'] == 2
    assert len(merged) == 2


from strategies.confirmation import news_flow as nf


def test_score_positive_with_volume():
    s = nf.score({'news_mean_score': 0.6, 'news_count_24h': 5}, {'min_articles': 2})
    assert s > 0


def test_score_zero_below_min_articles():
    assert nf.score({'news_mean_score': 0.9, 'news_count_24h': 1}, {'min_articles': 2}) == 0.0


def test_score_negative_for_bearish_news():
    assert nf.score({'news_mean_score': -0.5, 'news_count_24h': 4}, {'min_articles': 2}) < 0


def test_score_missing_data_is_zero():
    assert nf.score(None, {'min_articles': 2}) == 0.0
    assert nf.score({}, {'min_articles': 2}) == 0.0


from src.strategies import aux_data_loader as adl


def test_sentiment_panel_point_in_time(tmp_path, monkeypatch):
    df = pd.DataFrame([
        {'ticker': 'AAA', 'date': '2022-03-01', 'news_count_24h': 3, 'news_mean_score': 0.5,
         'news_finbert_pos': 2, 'news_finbert_neu': 1, 'news_finbert_neg': 0},
        {'ticker': 'AAA', 'date': '2022-03-05', 'news_count_24h': 2, 'news_mean_score': -0.4,
         'news_finbert_pos': 0, 'news_finbert_neu': 0, 'news_finbert_neg': 2},
    ])
    p = tmp_path / 'sentiment.parquet'; df.to_parquet(p)
    monkeypatch.setattr(adl, 'SENTIMENT_PATH', p, raising=False)
    monkeypatch.setattr(adl, '_SENT_DF', None, raising=False)
    aux = adl.load_aux_data('2022-03-03')
    assert aux['sentiment']['AAA']['news_mean_score'] == 0.5    # uses 03-01, not future 03-05
    aux2 = adl.load_aux_data('2022-03-06')
    assert aux2['sentiment']['AAA']['news_mean_score'] == -0.4  # now 03-05 visible


import numpy as np
from strategies.implementations.S_news_sentiment_long_short import NewsSentimentLongShort


def _flat_prices(tickers, n=40):
    idx = pd.bdate_range('2022-02-01', periods=n)
    return pd.DataFrame({t: np.full(n, 100.0) for t in tickers}, index=idx)


def test_sentiment_strategy_longs_positive_shorts_negative():
    s = NewsSentimentLongShort()
    aux = {'sentiment': {
        'AAA': {'news_mean_score': 0.7, 'news_count_24h': 5},
        'BBB': {'news_mean_score': -0.6, 'news_count_24h': 4},
        'CCC': {'news_mean_score': 0.05, 'news_count_24h': 5},
    }}
    sigs = s.generate_signals(_flat_prices(['AAA', 'BBB', 'CCC']), {'state': 'LOW_VOL'},
                              ['AAA', 'BBB', 'CCC'], aux)
    dirs = {x.ticker: x.direction for x in sigs}
    assert dirs.get('AAA') == 'LONG'
    assert dirs.get('BBB') == 'SHORT'
    assert 'CCC' not in dirs


def test_sentiment_strategy_no_sentiment_no_signals():
    s = NewsSentimentLongShort()
    assert s.generate_signals(_flat_prices(['AAA']), {'state': 'LOW_VOL'}, ['AAA'], {'sentiment': {}}) == []
