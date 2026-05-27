"""SP-4: ingestion harvest surface includes derivatives/vol categories + authors.
Run: pytest tests/test_ingestion_breadth.py -v
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def test_arxiv_has_derivatives_categories():
    from ingestion import arxiv_discovery as a
    assert 'q-fin.PR' in a.CATEGORIES
    assert 'q-fin.MF' in a.CATEGORIES
    assert 'q-fin.ST' in a.CATEGORIES and 'cs.LG' in a.CATEGORIES


def test_openalex_has_options_authors():
    from ingestion import openalex_discovery as o
    assert 'carr' in o.AUTHOR_WATCHLIST
    assert 'fama' in o.AUTHOR_WATCHLIST
    assert any(c not in ('C10138342', 'C64943373', 'C91602232', 'C93373587')
               for c in o.FINANCE_CONCEPTS)
