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


def test_fetch_contracts_page_parses_stdout(monkeypatch):
    class _R:
        returncode = 0
        stdout = '{"option_contracts": [{"underlying_symbol": "AAPL"}], "next_page_token": null}'
        stderr = ''
    monkeypatch.setattr(oe.subprocess, 'run', lambda *a, **k: _R())
    page = oe._fetch_contracts_page()
    assert oe._parse_underlyings(page) == {'AAPL'}


def test_fetch_contracts_page_raises_on_nonzero(monkeypatch):
    class _R:
        returncode = 1
        stdout = ''
        stderr = 'unauthorized'
    monkeypatch.setattr(oe.subprocess, 'run', lambda *a, **k: _R())
    import pytest
    with pytest.raises(RuntimeError):
        oe._fetch_contracts_page()


def test_fetch_contracts_page_builds_pagetoken_args(monkeypatch):
    seen = {}
    class _R:
        returncode = 0
        stdout = '{"option_contracts": []}'
        stderr = ''
    def fake_run(args, **k):
        seen['args'] = args
        return _R()
    monkeypatch.setattr(oe.subprocess, 'run', fake_run)
    oe._fetch_contracts_page(page_token='abc', limit=500)
    assert '--page-token' in seen['args'] and 'abc' in seen['args']
    assert '--status' in seen['args'] and 'active' in seen['args']
    assert '500' in seen['args']


def test_load_prior_cache_missing_returns_empty(tmp_path):
    assert oe._load_prior_cache(tmp_path / 'nope.json') == {}


def test_atomic_write_and_reload_roundtrip(tmp_path):
    p = tmp_path / 'sub' / 'cache.json'      # parent dir does not exist yet
    oe._atomic_write_cache({'AAPL': True, 'MSFT': True}, p)
    assert oe._load_prior_cache(p) == {'AAPL': True, 'MSFT': True}
    # no leftover temp file
    assert list(p.parent.glob('*.tmp')) == []


def test_atomic_write_replaces_existing(tmp_path):
    p = tmp_path / 'cache.json'
    oe._atomic_write_cache({'OLD': True}, p)
    oe._atomic_write_cache({'NEW': True}, p)
    assert oe._load_prior_cache(p) == {'NEW': True}
