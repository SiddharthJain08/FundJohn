from src.pipeline import options_eligibility as oe


def test_parse_underlyings_extracts_distinct():
    page = {'option_contracts': [
        {'underlying_symbol': 'AAPL', 'symbol': 'AAPL260101C1'},
        {'underlying_symbol': 'AAPL', 'symbol': 'AAPL260101P1'},
        {'underlying_symbol': 'MSFT', 'symbol': 'MSFT260101C1'},
        {'symbol': 'NOUNDERLYING'},          # missing field → skipped
    ]}
    assert oe._parse_underlyings(page) == {'AAPL', 'MSFT'}


def test_parse_underlyings_empty_page():
    assert oe._parse_underlyings({'option_contracts': []}) == set()
    assert oe._parse_underlyings({}) == set()


def _pager(pages):
    """Return a fetch_page(token) that walks a list of page dicts in order."""
    seq = iter(pages)
    def fetch(_token):
        return next(seq)
    return fetch


def test_enumerate_paginates_to_terminal():
    pages = [
        {'option_contracts': [{'underlying_symbol': 'AA'}], 'next_page_token': 't1'},
        {'option_contracts': [{'underlying_symbol': 'AAPL'}], 'next_page_token': 't2'},
        {'option_contracts': [{'underlying_symbol': 'MSFT'}], 'next_page_token': None},
    ]
    optionable, completed, n = oe.enumerate_optionable_underlyings(fetch_page=_pager(pages))
    assert optionable == {'AA', 'AAPL', 'MSFT'}
    assert completed is True
    assert n == 3


def test_enumerate_incomplete_on_page_error():
    def fetch(_token):
        raise RuntimeError('boom')
    optionable, completed, n = oe.enumerate_optionable_underlyings(fetch_page=fetch)
    assert completed is False
    assert optionable == set()


def test_enumerate_incomplete_on_budget():
    pages = [{'option_contracts': [{'underlying_symbol': 'AA'}], 'next_page_token': 't1'}] * 5
    # clock jumps past the deadline immediately on the first budget check
    ticks = iter([0, 1000, 1001, 1002, 1003, 1004])
    optionable, completed, n = oe.enumerate_optionable_underlyings(
        fetch_page=_pager(pages), budget_s=10, clock=lambda: next(ticks))
    assert completed is False
