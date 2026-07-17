"""Tests for src/ingestion/news_rss_ingest.py — Phase 2C live wiring.

Covers:
  test_parse_rss_2_0_fixture         — RSS 2.0 sample → expected items
  test_parse_atom_fixture            — Atom 1.0 sample → expected items
  test_parse_pubdate_formats         — RFC 822 / ISO / GMT / EDT / garbage
  test_insert_into_corpus_split      — kept/dropped split + shared group_id
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.ingestion.news_rss_ingest import (
    _parse_pubdate,
    _parse_rss,
    insert_into_corpus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RSS_2_0_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Test Wire</title>
    <item>
      <title>Apple beats Q1 earnings, raises guidance</title>
      <link>https://example.com/apple-q1</link>
      <pubDate>Mon, 12 May 2026 14:30:00 -0400</pubDate>
    </item>
    <item>
      <title>Tesla recalls 50,000 Cybertrucks</title>
      <link>https://example.com/tesla-recall</link>
      <pubDate>Mon, 12 May 2026 15:00:00 GMT</pubDate>
    </item>
    <item>
      <!-- Deliberately empty link — must be skipped -->
      <title>Should be skipped</title>
      <link></link>
      <pubDate>Mon, 12 May 2026 15:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom Feed</title>
  <entry>
    <title>Microsoft acquires startup for $1B</title>
    <link href="https://example.com/msft-deal" />
    <published>2026-05-12T18:00:00Z</published>
  </entry>
  <entry>
    <title>Google announces new AI model</title>
    <link href="https://example.com/google-ai" />
    <published>2026-05-12T19:30:00+00:00</published>
  </entry>
</feed>
"""


# ---------------------------------------------------------------------------
# _parse_rss
# ---------------------------------------------------------------------------

def test_parse_rss_2_0_fixture():
    items = _parse_rss(RSS_2_0_FIXTURE, 'rss:test', 'wire-service')
    # Item with empty link is dropped → 2 items.
    assert len(items) == 2
    titles = {it['title'] for it in items}
    assert titles == {
        'Apple beats Q1 earnings, raises guidance',
        'Tesla recalls 50,000 Cybertrucks',
    }
    for it in items:
        assert it['source'] == 'rss:test'
        assert it['category'] == 'wire-service'
        assert it['id'] == it['link']
        assert it['link'].startswith('https://example.com/')
        assert it['ts'].tzinfo is not None
        assert it['parsed_ts_ok'] is True


def test_parse_atom_fixture():
    items = _parse_rss(ATOM_FIXTURE, 'rss:atomtest', 'commentary')
    assert len(items) == 2
    links = {it['link'] for it in items}
    assert links == {
        'https://example.com/msft-deal',
        'https://example.com/google-ai',
    }
    for it in items:
        assert it['source'] == 'rss:atomtest'
        assert it['category'] == 'commentary'
        assert it['ts'].tzinfo is not None
        # Both parsed via ISO 8601 — must be UTC.
        assert it['ts'].utcoffset() == timedelta(0)
        assert it['parsed_ts_ok'] is True


# ---------------------------------------------------------------------------
# _parse_pubdate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('s,expected_iso', [
    # RFC 822 with numeric offset (SEC, Yahoo, SeekingAlpha)
    ('Mon, 12 May 2026 14:30:00 -0400', '2026-05-12T18:30:00+00:00'),
    # RFC 822 with literal GMT (Federal Reserve, MarketWatch)
    ('Mon, 12 May 2026 15:00:00 GMT',   '2026-05-12T15:00:00+00:00'),
    # CNBC style — no seconds
    ('Mon, 12 May 2026 15:00 GMT',      '2026-05-12T15:00:00+00:00'),
    # BEA style — named US zone (EDT)
    ('Tue, 05 May 2026 08:30:00 EDT',   '2026-05-05T12:30:00+00:00'),
    # ISO 8601
    ('2026-05-12T18:00:00Z',            '2026-05-12T18:00:00+00:00'),
    ('2026-05-12T19:30:00+00:00',       '2026-05-12T19:30:00+00:00'),
    # Date-only
    ('2026-05-12',                      '2026-05-12T00:00:00+00:00'),
])
def test_parse_pubdate_formats(s, expected_iso):
    dt = _parse_pubdate(s, 'rss:test')
    assert dt is not None, f'Failed to parse {s!r}'
    assert dt.isoformat() == expected_iso


def test_parse_pubdate_garbage_returns_none(capsys):
    """Garbage input returns None (NOT silent fall-through to now()) and
    emits a WARN to stderr so silent format drift surfaces."""
    dt = _parse_pubdate('this is not a date', 'rss:test')
    assert dt is None
    err = capsys.readouterr().err
    assert 'WARN: unparseable pubDate' in err
    assert 'rss:test' in err


def test_parse_pubdate_empty_returns_none():
    assert _parse_pubdate('', 'rss:test') is None
    assert _parse_pubdate(None, 'rss:test') is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# insert_into_corpus — split kept/dropped + shared group_id
# ---------------------------------------------------------------------------

def _mock_conn(prior_rows: list[tuple]) -> tuple[MagicMock, MagicMock]:
    """Build a psycopg2-style mock conn whose cursor returns `prior_rows`
    on the prior-window SELECT, and tracks INSERTs for inspection."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = prior_rows
    # Default: ON CONFLICT does NOT fire — every INSERT writes 1 row.
    cur.rowcount = 1
    conn.cursor.return_value = cur
    return conn, cur


def test_insert_into_corpus_splits_kept_dropped_with_shared_group_id():
    now = datetime.now(tz=timezone.utc)
    items = [
        {'id':       'https://a.com/aapl-beats-q1',
         'title':    'Apple beats Q1 earnings, raises guidance',
         'source':   'rss:reuters',
         'link':     'https://a.com/aapl-beats-q1',
         'category': 'wire-service',
         'ts':       now,
         'parsed_ts_ok': True},
        {'id':       'https://b.com/aapl-beats-q1',
         # Same headline → must be Jaccard-dropped against the first.
         'title':    'Apple beats Q1 earnings; raises guidance',
         'source':   'rss:bloomberg',
         'link':     'https://b.com/aapl-beats-q1',
         'category': 'commentary',
         'ts':       now + timedelta(hours=1),
         'parsed_ts_ok': True},
        {'id':       'https://c.com/tsla-recall',
         'title':    'Tesla Cybertruck recall affects 50,000 units',
         'source':   'rss:wsj',
         'link':     'https://c.com/tsla-recall',
         'category': 'wire-service',
         'ts':       now + timedelta(hours=2),
         'parsed_ts_ok': True},
    ]
    conn, cur = _mock_conn(prior_rows=[])
    result = insert_into_corpus(items, conn, dry_run=False)

    assert result['kept'] == 2
    assert result['dropped'] == 1
    assert result['inserted'] == 3            # all 3 INSERTs succeed
    assert result['on_conflict_skipped'] == 0
    assert result['prior_window_size'] == 0
    assert result['batch_group_id']           # UUID assigned

    # Verify all 3 rows were INSERTed and all share the same dedup_group_id.
    insert_calls = [
        call for call in cur.execute.call_args_list
        if 'INSERT INTO research_corpus' in call.args[0]
    ]
    assert len(insert_calls) == 3
    group_ids = {call.args[1][8] for call in insert_calls}  # column 9 = dedup_group_id
    assert len(group_ids) == 1, 'all batch items must share one dedup_group_id'

    # Verify the dropped flag was set correctly:
    #   item 0 (apple, earlier) → kept (False)
    #   item 1 (apple, later)   → dropped (True)
    #   item 2 (tesla, distinct) → kept (False)
    dropped_flags = [call.args[1][9] for call in insert_calls]   # column 10
    links_inserted = [call.args[1][1] for call in insert_calls]  # column 2 (source_url)
    by_link = dict(zip(links_inserted, dropped_flags))
    assert by_link['https://a.com/aapl-beats-q1'] is False
    assert by_link['https://b.com/aapl-beats-q1'] is True
    assert by_link['https://c.com/tsla-recall'] is False

    # Commit was called once at the end.
    conn.commit.assert_called_once()


def test_insert_into_corpus_dry_run_does_not_write():
    now = datetime.now(tz=timezone.utc)
    items = [
        {'id':       'https://a.com/x',
         'title':    'Some headline',
         'source':   'rss:test',
         'link':     'https://a.com/x',
         'category': 'wire-service',
         'ts':       now,
         'parsed_ts_ok': True},
    ]
    conn, cur = _mock_conn(prior_rows=[])
    result = insert_into_corpus(items, conn, dry_run=True)
    assert result['dry_run'] is True
    assert result['inserted'] == 0
    # Only the prior-window SELECT — no INSERTs.
    insert_calls = [
        call for call in cur.execute.call_args_list
        if 'INSERT' in call.args[0]
    ]
    assert insert_calls == []
    conn.commit.assert_not_called()


def test_insert_into_corpus_uses_null_published_date_when_pubdate_unparseable():
    """If parsed_ts_ok is False, published_date column must be NULL even
    though we use the wall-clock ts for dedup math."""
    now = datetime.now(tz=timezone.utc)
    items = [{
        'id':       'https://a.com/x',
        'title':    'Some headline with no parseable date',
        'source':   'rss:test',
        'link':     'https://a.com/x',
        'category': 'wire-service',
        'ts':       now,
        'parsed_ts_ok': False,
    }]
    conn, cur = _mock_conn(prior_rows=[])
    insert_into_corpus(items, conn, dry_run=False)
    insert_call = next(
        call for call in cur.execute.call_args_list
        if 'INSERT INTO research_corpus' in call.args[0]
    )
    # Column 7 = published_date.
    assert insert_call.args[1][6] is None
