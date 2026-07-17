"""tests/test_run_sentiment_step.py — orchestration coverage for the
daily sentiment pipeline step.

Lives at src.pipeline.run_sentiment_step (not scripts/) so that
``pipeline_orchestrator._resolve_script`` finds it without needing the
hard-coded resolver to learn about the scripts/ directory.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_run_sentiment_step_happy_path():
    """Stub all upstream modules; verify orchestration produces upsert+parquet calls."""
    import importlib
    mod = importlib.import_module('src.pipeline.run_sentiment_step')

    fake_universe = ['AAPL', 'TSLA', 'MSFT']
    fake_reddit = [
        {
            'ticker_mentions': ['AAPL', 'AAPL', 'AAPL', 'MSFT'],
            'subreddit': 'wsb',
            'author': 'u1',
            'title': '',
            'body': '',
        }
    ] * 4
    fake_stocktwits_row = {
        'ticker': 'AAPL',
        'bull_count': 10,
        'bear_count': 4,
        'neutral_count': 6,
        'total_posts': 20,
        'authors': ['s1', 's2'],
    }
    fake_news_raw = [
        {'ticker': 'AAPL', 'headline': 'Apple beats earnings', 'summary': ''}
    ]
    fake_news_scored = [
        {
            'ticker': 'AAPL',
            'news_count_24h': 1,
            'news_finbert_pos': 1,
            'news_finbert_neu': 0,
            'news_finbert_neg': 0,
            'news_mean_score': 0.85,
            'news_top_headlines': ['Apple beats earnings'],
        }
    ]
    # social_rows the aggregator would emit (Reddit-derived ticker rows)
    fake_social_rows = [
        {
            'ticker': 'AAPL',
            'social_posts_24h': 4,
            'social_unique_authors': 1,
            'social_top_themes': {},
        },
        {
            'ticker': 'MSFT',
            'social_posts_24h': 4,
            'social_unique_authors': 1,
            'social_top_themes': {},
        },
    ]

    # Env gates pinned OFF so the test stays hermetic on the production VPS:
    #   ALPACA_NEWS_INGEST (stage 6b, added eab352b) and
    #   OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE (_widen_with_resolver, added a3d4616)
    # must not leak in from the ambient environment and trigger live work.
    test_env = {
        'POSTGRES_URI': 'postgresql://test/test',
        'ALPACA_NEWS_INGEST': '0',
        'OPENCLAW_SENTIMENT_RESOLVER_UNIVERSE': '0',
    }
    # Stage 8's Discord summary (added a7f8eb4) does a deferred
    # `from src.execution.pipeline_orchestrator import post_channel` inside
    # main(); stub the module in sys.modules so no real webhook fires.
    fake_orchestrator = MagicMock()
    with patch.dict('os.environ', test_env), \
         patch.dict('sys.modules', {'src.execution.pipeline_orchestrator': fake_orchestrator}), \
         patch.object(mod, 'current_universe', return_value=fake_universe), \
         patch.object(mod, 'fetch_multiple_subreddits', return_value=fake_reddit), \
         patch.object(mod, 'select_sparse_tickers', return_value=['AAPL']), \
         patch.object(mod, 'fetch_many_tickers', return_value=[fake_stocktwits_row]), \
         patch.object(mod, 'aggregate_all', return_value=fake_social_rows), \
         patch.object(mod, '_load_todays_news', return_value=fake_news_raw), \
         patch.object(mod, 'score_news_rows', return_value=fake_news_scored), \
         patch.object(mod, 'upsert_postgres', return_value=2) as up_pg, \
         patch.object(mod, '_append_parquet', return_value=2) as up_pq:
        # b45b7cf (2026-06-08): sentiment_storage.append_parquet replaced by a
        # local _append_parquet shadow (pd.to_datetime cast keeps 'date' as
        # datetime64, compatible with the timestamp[us] parquet schema).
        # The module no longer imports/exports append_parquet.
        rc = mod.main(['--date', '2026-05-20'])

    assert rc == 0
    assert up_pg.call_count == 1
    assert up_pq.call_count == 1
    rows_arg = up_pg.call_args[0][0]
    assert {r['ticker'] for r in rows_arg} == {'AAPL', 'MSFT'}


def test_run_sentiment_step_universe_failure_aborts():
    import importlib
    mod = importlib.import_module('src.pipeline.run_sentiment_step')
    with patch.dict('os.environ', {'POSTGRES_URI': 'postgresql://test/test'}), \
         patch.object(mod, 'current_universe', side_effect=ConnectionError('db down')):
        rc = mod.main(['--date', '2026-05-20'])
    assert rc == 2
