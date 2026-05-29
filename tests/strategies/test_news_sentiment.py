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
