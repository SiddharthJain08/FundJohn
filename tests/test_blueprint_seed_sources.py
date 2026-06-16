"""Tests for the deterministic blueprint-strategy seed feeds wired into
expanded_sources.py (Phase 2 of the weekly research sweep).

These guard the contract the Sunday research-ingest run depends on:
  * the seed config loads and has the shape fetch_source() expects,
  * the OPENCLAW_BLUEPRINT_SEEDS=0 kill-switch and --no-seeds path work,
  * seeds are deduped against Opus-discovered feeds (seeds first),
  * seeds survive an EMPTY discovery list (the "ingested every run regardless
    of Opus" guarantee — merge happens before the empty-exit).
"""
import importlib
import json
import os
import re

import pytest

es = importlib.import_module('src.ingestion.expanded_sources')


def test_seed_file_exists_and_well_formed():
    with open(es._SEED_FILE, 'r', encoding='utf-8') as fh:
        blob = json.load(fh)
    assert isinstance(blob, dict)
    assert isinstance(blob.get('sources'), list) and blob['sources']


def test_load_seed_sources_shape():
    seeds = es.load_seed_sources()
    assert len(seeds) >= 3
    for s in seeds:
        assert s['feed_url'].startswith('https://'), s
        assert s.get('domain') and s.get('name')
        assert s.get('kind') in ('rss', 'atom', 'html')
        assert isinstance(s.get('strategy_types'), list)
    domains = {s['domain'] for s in seeds}
    # The three verified one-post-per-item RSS blogs (2026-06-16).
    assert {'quantifiedstrategies.com', 'robotwealth.com',
            'alvarezquanttrading.com'} <= domains
    # Quantocracy is a digest hub — must NOT be a directly-ingested seed.
    assert not any('quantocracy' in d for d in domains)


def test_html_pattern_seeds_load_with_link_pattern():
    """Phase 4: clean-URL HTML seeds (TuringTrader, Quantpedia) load via
    load_seed_sources() and carry a usable link_pattern + html kind, while the
    RSS seeds keep loading unchanged."""
    seeds = es.load_seed_sources()
    html = [s for s in seeds if s.get('kind') == 'html']
    assert len(html) >= 2, 'expected at least the TuringTrader + Quantpedia HTML seeds'
    html_domains = {s['domain'] for s in html}
    assert {'turingtrader.com', 'quantpedia.com'} <= html_domains
    for s in html:
        # link_pattern must be present and a compilable regex.
        assert s.get('link_pattern'), s
        re.compile(s['link_pattern'])
        assert s.get('origin_hint') == 'blog_blueprint', s
        assert s['feed_url'].startswith('https://'), s
    # RSS seeds untouched by the HTML additions.
    rss_domains = {s['domain'] for s in seeds if s.get('kind') == 'rss'}
    assert {'quantifiedstrategies.com', 'robotwealth.com',
            'alvarezquanttrading.com'} <= rss_domains


def test_kill_switch_env_disables_seeds(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BLUEPRINT_SEEDS', '0')
    assert es.load_seed_sources() == []
    monkeypatch.setenv('OPENCLAW_BLUEPRINT_SEEDS', 'off')
    assert es.load_seed_sources() == []


def test_default_env_enables_seeds(monkeypatch):
    monkeypatch.delenv('OPENCLAW_BLUEPRINT_SEEDS', raising=False)
    assert len(es.load_seed_sources()) >= 3
    monkeypatch.setenv('OPENCLAW_BLUEPRINT_SEEDS', '1')
    assert len(es.load_seed_sources()) >= 3


def test_merge_seeds_first_then_discovered():
    seeds = [{'domain': 'a.com', 'feed_url': 'https://a.com/feed/'}]
    disc = [{'domain': 'b.com', 'feed_url': 'https://b.com/feed/'}]
    merged = es.merge_with_seeds(seeds, disc)
    assert [m['feed_url'] for m in merged] == ['https://a.com/feed/', 'https://b.com/feed/']


def test_merge_dedups_overlapping_feed_url():
    seeds = [{'domain': 'a.com', 'feed_url': 'https://a.com/feed/'}]
    # Opus re-discovers the same feed (trailing-slash variance) — must collapse.
    disc = [{'domain': 'a.com', 'feed_url': 'https://a.com/feed'},
            {'domain': 'c.com', 'feed_url': 'https://c.com/feed/'}]
    merged = es.merge_with_seeds(seeds, disc)
    urls = [m['feed_url'] for m in merged]
    assert urls == ['https://a.com/feed/', 'https://c.com/feed/']


def test_seeds_survive_empty_discovery():
    """The whole point: a week where Opus discovers nothing still ingests seeds."""
    seeds = es.load_seed_sources()
    merged = es.merge_with_seeds(seeds, [])
    assert len(merged) == len(seeds) >= 3


def test_merge_skips_entries_without_url():
    seeds = [{'name': 'no-url'}]  # malformed seed
    disc = [{'domain': 'd.com', 'feed_url': 'https://d.com/feed/'}]
    merged = es.merge_with_seeds(seeds, disc)
    assert [m['feed_url'] for m in merged] == ['https://d.com/feed/']
