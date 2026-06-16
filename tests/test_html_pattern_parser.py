"""Tests for the per-source link-pattern HTML index parser
(`_parse_html_index_pattern`) in expanded_sources.py — Phase 4 of the
Blueprint Fast Lane.

The original `_parse_html_index` only harvests anchors whose href ends in
`.pdf`/`.htm(l)`, so explicit-rule sites with clean (extension-less) URLs
(TuringTrader `/portfolios/<slug>/`, Quantpedia `/strategies/<slug>`) can't be
crawled. The pattern parser lets each seed declare a `link_pattern` regex that
selects its strategy pages and excludes nav/footer junk.
"""
import importlib

es = importlib.import_module('src.ingestion.expanded_sources')


def test_pattern_parser_harvests_only_matching():
    body = open('tests/fixtures/turingtrader_index.html', 'rb').read()
    items = es._parse_html_index_pattern(body, 'https://www.turingtrader.com/portfolios/',
                                         r'/portfolios/[a-z0-9-]+/?$', 'expanded:TuringTrader', max_links=50)
    urls = [i['source_url'] for i in items]
    assert all('/portfolios/' in u for u in urls)
    assert len(items) >= 2          # caught the real strategy links
    assert not any('/about' in u or '#' in u for u in urls)   # nav/footer filtered


def test_pattern_parser_excludes_bare_index():
    """The bare `/portfolios/` index link (no slug) must NOT be harvested."""
    body = open('tests/fixtures/turingtrader_index.html', 'rb').read()
    items = es._parse_html_index_pattern(body, 'https://www.turingtrader.com/portfolios/',
                                         r'/portfolios/[a-z0-9-]+/?$', 'expanded:TuringTrader', max_links=50)
    urls = [i['source_url'] for i in items]
    assert not any(u.rstrip('/').endswith('/portfolios') for u in urls)


def test_pattern_parser_item_shape():
    """Items mirror _parse_html_index's dict shape, with feed_kind html-pattern."""
    body = open('tests/fixtures/turingtrader_index.html', 'rb').read()
    items = es._parse_html_index_pattern(body, 'https://www.turingtrader.com/portfolios/',
                                         r'/portfolios/[a-z0-9-]+/?$', 'expanded:TuringTrader', max_links=50)
    assert items
    it = items[0]
    assert set(it.keys()) >= {'source', 'source_url', 'title', 'abstract',
                              'authors', 'venue', 'published_date', 'raw_metadata'}
    assert it['source'] == 'expanded:TuringTrader'
    assert it['abstract'] == ''
    assert it['authors'] == []
    assert it['published_date'] is None
    assert it['raw_metadata']['feed_kind'] == 'html-pattern'


def test_pattern_parser_dedupes_and_caps():
    body = open('tests/fixtures/turingtrader_index.html', 'rb').read()
    items = es._parse_html_index_pattern(body, 'https://www.turingtrader.com/portfolios/',
                                         r'/portfolios/[a-z0-9-]+/?$', 'expanded:TuringTrader', max_links=1)
    assert len(items) == 1          # cap honored
    # absolute-resolved + deduped: no duplicate URLs
    all_items = es._parse_html_index_pattern(body, 'https://www.turingtrader.com/portfolios/',
                                             r'/portfolios/[a-z0-9-]+/?$', 'expanded:TuringTrader', max_links=50)
    urls = [i['source_url'] for i in all_items]
    assert len(urls) == len(set(urls))
