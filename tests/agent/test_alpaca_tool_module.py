"""tools.alpaca — generated READ-ONLY Alpaca CLI wrapper for sub-agents.

The tool-guide names Alpaca as P1 for prices / options / news / screener /
corporate actions, but until 2026-08-22 `workspace/tools/alpaca.py` was the
generic HTTP stub (no functions). This locks in the generated module's
contract without touching the network: a fake `alpaca` binary records argv
and replays canned JSON.

Contract pinned here (all from github.com/alpacahq/cli + our own incidents):
  * every call passes --quiet and --timeout; JSON is the default output (no --json)
  * exit 2 => AlpacaAuthError (never retried); exit 1 => AlpacaCLIError with the
    stderr JSON fields (status/code/error/hint) — stderr is parsed as a WHOLE
    document (v0.0.10+ pretty-prints it across lines)
  * `order list` reads --nested --limit 500 --direction asc and keyset-paginate
    with --after-order-id until a short page (the 08-12 / 08-18 incidents)
  * data reads use the multi-symbol endpoints, chunk symbols, and follow
    next_page_token
  * the module exposes NO order-submitting / cancelling / closing function
"""
from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / 'src' / 'agent' / 'tools' / 'mcp' / 'alpaca.js'


def _generate_module(tmp_path: Path) -> Path:
    """Render the template exactly as registry.js does and drop it in tmp_path."""
    script = (
        "const m=require(%s);"
        "process.stdout.write(m.generatePython({name:'alpaca',description:'desc'}));"
        % json.dumps(str(TEMPLATE))
    )
    out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    mod = tmp_path / 'alpaca.py'
    mod.write_text(out.stdout)
    # no-op rate limiter / cycle cache — same names registry.js generates
    (tmp_path / '_rate_limiter.py').write_text(textwrap.dedent('''
        def _acquire_token(provider, cost=1): pass
        def _cycle_cache_get(scope, params): return None
        def _cycle_cache_set(scope, params, value, ttl=0): pass
    '''))
    return mod


def _fake_alpaca(tmp_path: Path, script_body: str) -> Path:
    """A stand-in /root/go/bin/alpaca: python that sees argv and prints JSON."""
    p = tmp_path / 'fake_alpaca.py'
    p.write_text('#!/usr/bin/env python3\nimport sys, json, os\nargv = sys.argv[1:]\n' + textwrap.dedent(script_body))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Return (module, argv_log_path). Call with the fake binary's body."""
    def _load(fake_body: str):
        _generate_module(tmp_path)
        fake = _fake_alpaca(tmp_path, fake_body)
        log = tmp_path / 'argv.jsonl'
        monkeypatch.setenv('ALPACA_CLI_BIN', str(fake))
        monkeypatch.setenv('ALPACA_FAKE_LOG', str(log))
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop('alpaca', None)
        sys.modules.pop('_rate_limiter', None)
        return importlib.import_module('alpaca'), log
    return _load


ECHO = '''
with open(os.environ['ALPACA_FAKE_LOG'], 'a') as f: f.write(json.dumps(argv) + '\\n')
'''


def _argv_log(log: Path) -> list[list[str]]:
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


# ── template hygiene ───────────────────────────────────────────────────────

def test_template_renders_valid_python_and_is_read_only(tmp_path):
    mod = _generate_module(tmp_path)
    src = mod.read_text()
    import ast
    ast.parse(src)
    # No write verbs anywhere near the CLI: execution stays in alpaca_executor.py
    for verb in ("'submit'", "'cancel'", "'cancel-all'", "'close'", "'close-all'",
                 "'replace'", "'exercise'", "'locate'", "'api'"):
        assert verb not in src, f'read-only module must not invoke {verb}'
    assert '--json' not in src, 'JSON is the default output; --json does not exist'
    assert "/root/go/bin/alpaca" in src and "ALPACA_CLI_BIN" in src


# ── _run contract ──────────────────────────────────────────────────────────

def test_every_call_is_quiet_with_timeout(load):
    m, log = load(ECHO + 'print(json.dumps({"is_open": False}))')
    assert m.get_clock() == {'is_open': False}
    (argv,) = _argv_log(log)
    assert argv[:1] == ['clock']
    assert '--quiet' in argv
    assert '--timeout' in argv and argv[argv.index('--timeout') + 1].isdigit()


def test_exit_2_is_auth_error_with_stderr_fields(load):
    m, _ = load('''
sys.stderr.write(json.dumps({"error": "unauthorized", "code": 40110000, "status": 401, "hint": "check keys"}))
sys.exit(2)
''')
    with pytest.raises(m.AlpacaAuthError) as ei:
        m.get_account()
    assert ei.value.status == 401 and ei.value.code == 40110000
    assert 'unauthorized' in str(ei.value)
    assert issubclass(m.AlpacaAuthError, m.AlpacaCLIError)


def test_exit_1_parses_multiline_pretty_printed_stderr(load):
    # v0.0.10+ prints the error document indented across lines
    m, _ = load('''
sys.stderr.write(json.dumps({"error": "order not found", "code": 40410000, "status": 404, "hint": ""}, indent=2))
sys.exit(1)
''')
    with pytest.raises(m.AlpacaCLIError) as ei:
        m.get_asset('NOPE')
    assert ei.value.status == 404 and ei.value.rc == 1
    assert not isinstance(ei.value, m.AlpacaAuthError)


def test_non_json_stderr_still_surfaces(load):
    m, _ = load('sys.stderr.write("boom: something odd\\n"); sys.exit(1)')
    with pytest.raises(m.AlpacaCLIError) as ei:
        m.get_account()
    assert 'boom' in str(ei.value)


def test_missing_binary_is_a_clear_error(load, monkeypatch):
    m, _ = load('print("{}")')
    monkeypatch.setattr(m, 'ALPACA_BIN', '/nonexistent/alpaca')
    with pytest.raises(m.AlpacaCLIError) as ei:
        m.get_clock()
    assert 'not found' in str(ei.value)


# ── broker-state reads ─────────────────────────────────────────────────────

def test_open_orders_nested_500_keyset_paginated(load):
    # Page 1: exactly 500 rows (cap) → must fetch page 2 with --after-order-id
    m, log = load(ECHO + '''
if '--after-order-id' in argv:
    print(json.dumps([{"id": "o-501", "legs": [{"id": "l"}]}]))
else:
    print(json.dumps([{"id": f"o-{i}", "legs": []} for i in range(1, 501)]))
''')
    orders = m.get_open_orders()
    assert len(orders) == 501 and orders[-1]['id'] == 'o-501'
    pages = _argv_log(log)
    assert len(pages) == 2
    for argv in pages:
        assert argv[:2] == ['order', 'list']
        assert '--nested' in argv
        assert argv[argv.index('--limit') + 1] == '500'
        assert argv[argv.index('--direction') + 1] == 'asc'
        assert argv[argv.index('--status') + 1] == 'open'
    assert pages[1][pages[1].index('--after-order-id') + 1] == 'o-500'


def test_open_orders_symbols_filter(load):
    m, log = load(ECHO + 'print("[]")')
    assert m.get_open_orders(symbols=['aapl', 'MSFT']) == []
    (argv,) = _argv_log(log)
    assert argv[argv.index('--symbols') + 1] == 'AAPL,MSFT'


def test_positions_and_account_are_not_cycle_cached(load, monkeypatch):
    m, log = load(ECHO + 'print(json.dumps([{"symbol":"SPY"}]) if argv[0]=="position" else json.dumps({"equity":"1"}))')
    hits = []
    monkeypatch.setattr(m, '_cycle_cache_get', lambda scope, params: hits.append(scope) or None)
    m.get_positions(); m.get_account(); m.get_open_orders()
    assert hits == [], 'broker state must never be served from the cycle cache'


# ── market-data reads ──────────────────────────────────────────────────────

def test_bars_uses_multi_bars_chunks_and_follows_page_token(load):
    m, log = load(ECHO + '''
syms = argv[argv.index('--symbols') + 1].split(',')
if '--page-token' in argv:
    print(json.dumps({"bars": {s: [{"t": "d2", "c": 2}] for s in syms}, "next_page_token": None}))
else:
    print(json.dumps({"bars": {s: [{"t": "d1", "c": 1}] for s in syms}, "next_page_token": "tok"}))
''')
    symbols = [f'S{i}' for i in range(450)]          # > 2 chunks of 200
    bars = m.get_bars(symbols, start='2026-08-01', end='2026-08-21')
    assert set(bars) == set(symbols)
    assert [b['t'] for b in bars['S0']] == ['d1', 'd2']
    calls = _argv_log(log)
    assert all(c[:2] == ['data', 'multi-bars'] for c in calls)
    assert len(calls) == 3 * 2                        # 3 chunks × 2 pages
    first = calls[0]
    assert first[first.index('--timeframe') + 1] == '1Day'
    assert first[first.index('--adjustment') + 1] == 'raw'
    assert first[first.index('--feed') + 1] == 'sip'
    assert first[first.index('--limit') + 1] == '10000'
    assert max(len(c[c.index('--symbols') + 1].split(',')) for c in calls) <= 200
    tokened = [c for c in calls if '--page-token' in c]
    assert len(tokened) == 3 and all(c[c.index('--page-token') + 1] == 'tok' for c in tokened)


def test_pagination_stops_at_max_pages(load):
    m, log = load(ECHO + 'print(json.dumps({"bars": {"A": [{"c": 1}]}, "next_page_token": "again"}))')
    bars = m.get_bars('A', start='2026-01-01', max_pages=3)
    assert len(bars['A']) == 3 and len(_argv_log(log)) == 3


def test_snapshots_latest_trades_and_crypto_symbol_form(load):
    m, log = load(ECHO + '''
syms = argv[argv.index('--symbols') + 1].split(',')
print(json.dumps({s: {"latestTrade": {"p": 1.0}} for s in syms}))
''')
    snaps = m.get_snapshots('spy, qqq')
    assert set(snaps) == {'SPY', 'QQQ'}
    lt = m.get_latest_trades(['SPY'])
    assert 'SPY' in lt
    cs = m.get_crypto_snapshots(['BTC-USD', 'eth/usd'])
    calls = _argv_log(log)
    assert calls[0][:2] == ['data', 'multi-snapshots']
    assert calls[1][:2] == ['data', 'latest-trades']
    assert calls[2][:3] == ['data', 'crypto', 'snapshots']
    assert calls[2][calls[2].index('--symbols') + 1] == 'BTC/USD,ETH/USD'
    assert set(cs) == {'BTC/USD', 'ETH/USD'}


def test_news_limit_capped_at_50_and_paginated(load):
    m, log = load(ECHO + '''
if '--page-token' in argv:
    print(json.dumps({"news": [{"id": 2}], "next_page_token": None}))
else:
    print(json.dumps({"news": [{"id": 1}], "next_page_token": "n2"}))
''')
    items = m.get_news(['AAPL'], limit=500, max_pages=5)
    assert [i['id'] for i in items] == [1, 2]
    first = _argv_log(log)[0]
    assert first[:2] == ['data', 'news']
    assert first[first.index('--limit') + 1] == '50'          # API max is 50
    assert '--exclude-contentless' in first


def test_option_chain_corporate_actions_screener_calendar(load):
    m, log = load(ECHO + '''
if argv[:3] == ['data', 'option', 'chain']:
    print(json.dumps({"snapshots": {"AAPL260918C00200000": {"greeks": {}}}, "next_page_token": None}))
elif argv[:2] == ['data', 'corporate-actions']:
    print(json.dumps({"corporate_actions": {"forward_splits": [{"symbol": "X"}]}, "next_page_token": None}))
elif argv[:3] == ['data', 'screener', 'movers']:
    print(json.dumps({"gainers": [], "losers": []}))
elif argv[:3] == ['data', 'screener', 'most-actives']:
    print(json.dumps({"most_actives": []}))
elif argv[:1] == ['calendar']:
    print(json.dumps([{"date": "2026-08-24"}]))
elif argv[:2] == ['option', 'contracts']:
    print(json.dumps({"option_contracts": [{"symbol": "C1"}], "next_page_token": None}))
else:
    print("{}")
''')
    chain = m.get_option_chain('aapl', expiration_lte='2026-12-31', type='call')
    assert 'AAPL260918C00200000' in chain
    ca = m.get_corporate_actions(['X'], start='2026-08-01', types=['forward_split'])
    assert ca['forward_splits'][0]['symbol'] == 'X'
    assert m.get_screener_movers(top=5) == {'gainers': [], 'losers': []}
    assert m.get_screener_most_actives(by='trades', top=5) == {'most_actives': []}
    assert m.get_calendar('2026-08-22', '2026-08-25')[0]['date'] == '2026-08-24'
    assert m.get_option_contracts('AAPL')[0]['symbol'] == 'C1'
    calls = {tuple(c[:3]) for c in _argv_log(log)}
    chain_call = next(c for c in _argv_log(log) if c[:3] == ['data', 'option', 'chain'])
    assert chain_call[chain_call.index('--underlying-symbol') + 1] == 'AAPL'
    assert chain_call[chain_call.index('--limit') + 1] == '1000'
    assert chain_call[chain_call.index('--type') + 1] == 'call'
    ca_call = next(c for c in _argv_log(log) if c[:2] == ['data', 'corporate-actions'])
    assert ca_call[ca_call.index('--types') + 1] == 'forward_split'
    assert ca_call[ca_call.index('--limit') + 1] == '1000'
    assert ('data', 'screener', 'movers') in calls and ('data', 'screener', 'most-actives') in calls


def test_data_reads_go_through_cycle_cache(load, monkeypatch):
    m, log = load(ECHO + 'print(json.dumps({"bars": {"A": []}}))')
    store = {}
    monkeypatch.setattr(m, '_cycle_cache_get', lambda scope, params: store.get(json.dumps(params)))
    monkeypatch.setattr(m, '_cycle_cache_set', lambda scope, params, value, ttl=0: store.__setitem__(json.dumps(params), value))
    m.get_bars('A', start='2026-08-01'); m.get_bars('A', start='2026-08-01')
    assert len(_argv_log(log)) == 1, 'second identical data read must be a cache hit'
