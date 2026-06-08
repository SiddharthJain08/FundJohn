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


def test_build_eligibility_intersects_and_trues_only():
    optionable = {'AAPL', 'MSFT', 'SPX', 'TSLA'}     # SPX not in our universe
    universe = {'AAPL', 'MSFT', 'TSLA', 'KO'}        # KO not optionable
    out = oe.build_eligibility(optionable, universe)
    assert out == {'AAPL': True, 'MSFT': True, 'TSLA': True}   # KO absent, SPX dropped


def test_decide_write_incomplete_never_writes():
    ok, reason = oe.decide_write({'AAPL': True}, {}, completed=False)
    assert ok is False and 'incomplete' in reason


def test_decide_write_first_run_above_abs_floor():
    new = {f'S{i}': True for i in range(1500)}        # 1500 >= 1000
    ok, _ = oe.decide_write(new, {}, completed=True, abs_floor=1000)
    assert ok is True


def test_decide_write_below_abs_floor_keeps_prior():
    new = {f'S{i}': True for i in range(500)}         # 500 < 1000
    ok, reason = oe.decide_write(new, {}, completed=True, abs_floor=1000)
    assert ok is False and 'floor' in reason


def test_decide_write_relative_floor_blocks_implausible_shrink():
    prior = {f'S{i}': True for i in range(5000)}
    new = {f'S{i}': True for i in range(2000)}        # 2000 < 0.5*5000=2500
    ok, reason = oe.decide_write(new, prior, completed=True, abs_floor=1000)
    assert ok is False and 'floor' in reason


def test_format_summary_contains_counts():
    s = oe._format_summary({'eligible': 4200, 'universe': 13845, 'pages': 132,
                            'added': 10, 'removed': 3, 'secs': 640.0, 'action': 'WROTE'})
    assert '4200' in s and '13845' in s and '132' in s and 'WROTE' in s


def test_post_summary_noop_when_no_url(monkeypatch):
    called = {'n': 0}
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: called.__setitem__('n', called['n'] + 1))
    oe._post_summary('hi', webhook_url='')
    assert called['n'] == 0


def test_post_summary_failopen(monkeypatch):
    import urllib.request
    def boom(*a, **k):
        raise OSError('network down')
    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    oe._post_summary('hi', webhook_url='https://example/wh')   # must not raise
