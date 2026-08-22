"""src.pipeline.alpaca_multibars — Python twin of collector.fillPricesAlpacaBatch.

One contract for every Python caller that pulls bars for many symbols
(backfillers/universe_prices, ingestion/ingest_prices_30m_alpaca):

  * `alpaca data multi-bars` with --symbols chunked so each request is about
    one 10 000-point page: chunk = clamp(8000 / (trading_days × bars_per_day), 1, 200)
  * next_page_token followed; a symbol split across pages accumulates
  * well-formed symbols Alpaca doesn't know are ABSENT from `bars` → []
  * `invalid symbol: X` (rc=1) drops X and retries the chunk; X → []
  * any other chunk failure raises MultiBarsError (callers decide fallback)
  * malformed symbols (^GSPC, ES=F, BRK-B) are the CALLER's problem: ValueError

Fake CLI: a python shim that logs argv and replays canned payloads.
"""
from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.pipeline import alpaca_multibars as mb  # noqa: E402


def _fake_cli(tmp_path: Path, payloads):
    """payloads: ordered [{stdout, stderr, exit}] — each call pops the next."""
    (tmp_path / 'payloads.json').write_text(json.dumps(payloads))
    (tmp_path / 'calls').write_text('0')
    bin_ = tmp_path / 'alpaca'
    bin_.write_text('#!/usr/bin/env python3\n' + textwrap.dedent(f'''
        import json, sys, os
        d = {str(tmp_path)!r}
        n = int(open(os.path.join(d, 'calls')).read()); open(os.path.join(d, 'calls'), 'w').write(str(n + 1))
        with open(os.path.join(d, 'argv.log'), 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')
        ps = json.load(open(os.path.join(d, 'payloads.json')))
        p = ps[n] if n < len(ps) else {{'stdout': '', 'exit': 0}}
        sys.stdout.write(p.get('stdout', '')); sys.stderr.write(p.get('stderr', '')); sys.exit(int(p.get('exit', 0)))
    '''))
    bin_.chmod(bin_.stat().st_mode | stat.S_IXUSR)
    def argv():
        p = tmp_path / 'argv.log'
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return str(bin_), argv


def page(bars, token=None):
    return {'stdout': json.dumps({'bars': bars, 'next_page_token': token})}


def bar(t, c=1.0):
    return {'t': f'{t}T04:00:00Z', 'o': c, 'h': c, 'l': c, 'c': c, 'v': 1, 'vw': c, 'n': 1}


def flag(a, f):
    return a[a.index(f) + 1]


# ── pure helpers ────────────────────────────────────────────────────────────

def test_is_alpaca_stock_symbol():
    for ok in ('AAPL', 'BRK.B', 'AGM.PRI', 'A', 'ZZZZNOPE'):
        assert mb.is_alpaca_stock_symbol(ok), ok
    for bad in ('BRK-B', '^GSPC', 'ES=F', 'BTC-USD', 'EURUSD=X', '', 'aapl', None):
        assert not mb.is_alpaca_stock_symbol(bad), bad


def test_chunk_size_scales_with_range_and_bars_per_day():
    assert mb.chunk_size('2026-08-21', '2026-08-21') == 200
    assert mb.chunk_size('2026-08-18', '2026-08-21') == 200
    year = mb.chunk_size('2025-01-01', '2025-12-31')
    assert 28 <= year <= 33, f'one daily year ≈ 8000/262 symbols, got {year}'
    assert mb.chunk_size('2026-08-19', '2026-08-21', bars_per_day=13) == 200
    assert 1 <= mb.chunk_size('2025-01-01', '2025-12-31', bars_per_day=13) <= 3
    assert mb.chunk_size('2026-08-21', '2000-01-01') == 1          # reversed range → 1, never 0


def test_partition_symbols_splits_malformed_and_maps_dash_to_dot():
    good, bad = mb.partition_symbols(['AAPL', 'BRK-B', '^GSPC', 'ES=F', 'AGM-PRI'])
    assert good == {'AAPL': 'AAPL', 'BRK.B': 'BRK-B', 'AGM.PRI': 'AGM-PRI'}
    assert bad == ['^GSPC', 'ES=F']


# ── fetch_multi_bars ────────────────────────────────────────────────────────

def test_single_chunk_args_fanout_and_absent_symbol(tmp_path):
    bin_, argv = _fake_cli(tmp_path, [page({'AAPL': [bar('2026-08-21')], 'BRK.B': [bar('2026-08-20'), bar('2026-08-21')]})])
    out = mb.fetch_multi_bars(['AAPL', 'BRK.B', 'GHOST'], '2026-08-20', '2026-08-21', alpaca_bin=bin_)
    assert {k: len(v) for k, v in out.items()} == {'AAPL': 1, 'BRK.B': 2, 'GHOST': 0}
    (a,) = argv()
    assert a[:2] == ['data', 'multi-bars']
    assert flag(a, '--symbols') == 'AAPL,BRK.B,GHOST'
    assert flag(a, '--start') == '2026-08-20' and flag(a, '--end') == '2026-08-21'
    assert flag(a, '--timeframe') == '1Day' and flag(a, '--adjustment') == 'split' and flag(a, '--feed') == 'sip'
    assert flag(a, '--limit') == '10000' and '--quiet' in a and '--timeout' in a


def test_timeframe_and_adjustment_are_parameters(tmp_path):
    bin_, argv = _fake_cli(tmp_path, [page({})])
    mb.fetch_multi_bars(['AAPL'], '2026-08-21', '2026-08-21', timeframe='30Min', adjustment='raw', bars_per_day=13, alpaca_bin=bin_)
    (a,) = argv()
    assert flag(a, '--timeframe') == '30Min' and flag(a, '--adjustment') == 'raw'


def test_pagination_accumulates_symbol_split_across_pages(tmp_path):
    bin_, argv = _fake_cli(tmp_path, [
        page({'AAPL': [bar('2026-08-19')], 'MSFT': [bar('2026-08-19')]}, token='t1'),
        page({'MSFT': [bar('2026-08-20'), bar('2026-08-21')]}),
    ])
    out = mb.fetch_multi_bars(['AAPL', 'MSFT'], '2026-08-19', '2026-08-21', alpaca_bin=bin_)
    assert {k: len(v) for k, v in out.items()} == {'AAPL': 1, 'MSFT': 3}
    calls = argv()
    assert '--page-token' not in calls[0] and flag(calls[1], '--page-token') == 't1'


def test_chunking_450_one_day_symbols_is_three_requests(tmp_path):
    syms = [f'T{i}' for i in range(450)]
    bin_, argv = _fake_cli(tmp_path, [page({s: [bar('2026-08-21')] for s in syms[:200]}),
                                       page({s: [bar('2026-08-21')] for s in syms[200:400]}),
                                       page({s: [bar('2026-08-21')] for s in syms[400:]})])
    out = mb.fetch_multi_bars(syms, '2026-08-21', '2026-08-21', alpaca_bin=bin_)
    assert len(out) == 450 and all(len(v) == 1 for v in out.values())
    assert [len(flag(a, '--symbols').split(',')) for a in argv()] == [200, 200, 50]


def test_invalid_symbol_is_dropped_and_chunk_retried(tmp_path):
    bin_, argv = _fake_cli(tmp_path, [
        {'stdout': '', 'stderr': json.dumps({'code': 0, 'status': 400, 'error': 'invalid symbol: ODD.X'}), 'exit': 1},
        page({'AAPL': [bar('2026-08-21')]}),
    ])
    out = mb.fetch_multi_bars(['AAPL', 'ODD.X'], '2026-08-21', '2026-08-21', alpaca_bin=bin_)
    assert out == {'AAPL': [bar('2026-08-21')], 'ODD.X': []}
    assert [flag(a, '--symbols') for a in argv()] == ['AAPL,ODD.X', 'AAPL']


def test_other_failures_raise_multibars_error_with_status(tmp_path):
    bin_, _ = _fake_cli(tmp_path, [{'stdout': '', 'stderr': json.dumps({'status': 502, 'error': 'bad gateway'}, indent=2), 'exit': 1}])
    with pytest.raises(mb.MultiBarsError) as ei:
        mb.fetch_multi_bars(['AAPL'], '2026-08-21', '2026-08-21', alpaca_bin=bin_)
    assert ei.value.status == 502 and 'bad gateway' in str(ei.value) and ei.value.auth_error is False


def test_exit_2_is_flagged_auth_error(tmp_path):
    bin_, _ = _fake_cli(tmp_path, [{'stdout': '', 'stderr': json.dumps({'status': 401, 'error': 'unauthorized'}), 'exit': 2}])
    with pytest.raises(mb.MultiBarsError) as ei:
        mb.fetch_multi_bars(['AAPL'], '2026-08-21', '2026-08-21', alpaca_bin=bin_)
    assert ei.value.auth_error is True


def test_malformed_symbol_is_a_caller_error(tmp_path):
    bin_, argv = _fake_cli(tmp_path, [])
    with pytest.raises(ValueError, match='BRK-B'):
        mb.fetch_multi_bars(['AAPL', 'BRK-B'], '2026-08-21', '2026-08-21', alpaca_bin=bin_)
    assert argv() == [], 'no request is made when the input is malformed'


def test_empty_input_makes_no_request(tmp_path):
    bin_, argv = _fake_cli(tmp_path, [])
    assert mb.fetch_multi_bars([], '2026-08-21', '2026-08-21', alpaca_bin=bin_) == {}
    assert argv() == []
