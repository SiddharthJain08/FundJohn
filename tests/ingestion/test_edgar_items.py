# tests/ingestion/test_edgar_items.py
from pathlib import Path
import pytest

from src.ingestion.edgar_items import (
    ITEM_DESCRIPTIONS,
    parse_items_from_document,
)


FIXTURE_DIR = Path(__file__).parent / 'fixtures' / 'edgar'


def _load(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def test_descriptions_map_has_28_items():
    assert len(ITEM_DESCRIPTIONS) == 28


def test_descriptions_map_has_core_items():
    for k in ('1.01', '2.02', '4.02', '5.02', '7.01', '8.01', '9.01'):
        assert k in ITEM_DESCRIPTIONS
        assert ITEM_DESCRIPTIONS[k]


def test_parse_well_formed_5_02_header():
    items = parse_items_from_document(_load('sample_8k_5_02.html'))
    assert items == ['5.02']


def test_parse_uppercase_item_header():
    items = parse_items_from_document(_load('sample_8k_9_01_only.html'))
    assert items == ['9.01']


def test_parse_multi_item_filing_dedupes_and_preserves_order():
    items = parse_items_from_document(_load('sample_8k_multi.html'))
    assert items == ['2.02', '9.01']


def test_parse_empty_input_returns_empty_list():
    assert parse_items_from_document(b'') == []


def test_parse_no_items_returns_empty_list():
    html = b'<html><body><p>Some 8-K body with no Item headers at all.</p></body></html>'
    assert parse_items_from_document(html) == []


def test_parse_unknown_item_number_filtered():
    html = b'<html><body><p>Item 99.99 Made-Up Section</p></body></html>'
    assert parse_items_from_document(html) == []


def test_parse_handles_non_utf8_bytes():
    html = b'\xff\xfe<html>Item 5.02 something</html>'
    items = parse_items_from_document(html)
    assert items == ['5.02'] or items == []


def test_parse_string_input_also_works():
    items = parse_items_from_document('Item 5.02 Departure of Directors')
    assert items == ['5.02']


def test_parse_tag_stripping():
    html = b'<html><b>Item 5.02</b> Departure of Officers</html>'
    assert parse_items_from_document(html) == ['5.02']


def test_parse_case_insensitive():
    assert parse_items_from_document(b'item 5.02 Departure') == ['5.02']
    assert parse_items_from_document(b'ITEM 5.02 Departure') == ['5.02']
    assert parse_items_from_document(b'Item 5.02 Departure') == ['5.02']
