"""Crypto rows in alpaca_tradable_universe (crypto engine Phase 3, Task 4).

Crypto pairs are stored with the INTERNAL symbol ('BTC-USD') and
asset_class='crypto'; the deactivation UPDATE is scoped to the asset classes
present in the run, so a crypto-only (or equity-only) pull never deactivates
the other class. The alpaca CLI is never invoked (subprocess is patched).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import refresh_tradable_universe as rtu  # noqa: E402
from ingestion.crypto_flow.universe import Asset  # noqa: E402

_UNIVERSE = [
    Asset(ticker='BTC-USD', alpaca='BTC/USD', position='BTCUSD', role='benchmark'),
    Asset(ticker='ETH-USD', alpaca='ETH/USD', position='ETHUSD'),
]


def _eq(symbol, tradable=True):
    return {'symbol': symbol, 'id': f'id-{symbol}', 'name': symbol,
            'class': 'us_equity', 'exchange': 'NYSE', 'status': 'active',
            'tradable': tradable, 'shortable': True, 'marginable': True,
            'fractionable': True, 'easy_to_borrow': True}


def _cr(alpaca_symbol, tradable=True):
    return {'symbol': alpaca_symbol, 'id': f'id-{alpaca_symbol}', 'name': alpaca_symbol,
            'class': 'crypto', 'exchange': 'CRYPTO', 'status': 'active',
            'tradable': tradable, 'shortable': False, 'marginable': False,
            'fractionable': True, 'easy_to_borrow': False}


def _cli(equities, cryptos):
    def run(args, **kw):
        cls = args[args.index('--asset-class') + 1]
        payload = equities if cls == 'us_equity' else cryptos
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr='')
    return run


def _conn(prior_rows):
    conn = MagicMock()
    cur = MagicMock()
    cur.__iter__ = lambda self: iter(prior_rows)
    conn.cursor.return_value = cur
    return conn, cur


def _update_call(cur):
    calls = [c for c in cur.execute.call_args_list
             if 'UPDATE alpaca_tradable_universe' in c.args[0]]
    assert len(calls) == 1, calls
    return calls[0]


def test_crypto_rows_inserted_with_internal_symbol_and_class():
    run = _cli([_eq('AAPL')], [_cr('BTC/USD'), _cr('ETH/USD'),
                               _cr('BTC/USDT'),          # non-USD quote → skipped
                               _cr('ZZZ/USD')])          # not in internal universe → skipped
    with patch.object(rtu.subprocess, 'run', side_effect=run) as sp:
        assets, _ms, stats = rtu.fetch_all_assets(crypto_universe=_UNIVERSE)
    called = [c.args[0][c.args[0].index('--asset-class') + 1] for c in sp.call_args_list]
    assert called == ['us_equity', 'crypto']
    syms = {a['symbol']: a['class'] for a in assets}
    assert syms == {'AAPL': 'us_equity', 'BTC-USD': 'crypto', 'ETH-USD': 'crypto'}
    assert stats['crypto_fetched'] == 4 and stats['crypto'] == 2 and stats['us_equity'] == 1
    assert stats['crypto_skipped_non_usd'] == 1          # BTC/USDT
    assert stats['crypto_unmapped_usd'] == 1             # ZZZ/USD — the one that matters
    assert stats['crypto_unmapped_symbols'] == ['ZZZ/USD']

    conn, _cur = _conn([])
    with patch.object(rtu.psycopg2.extras, 'execute_values') as bulk:
        rtu.upsert_universe(conn, assets)
    rows = bulk.call_args.args[2]
    by_sym = {r[0]: r for r in rows}
    assert 'BTC/USD' not in by_sym and 'BTCUSD' not in by_sym
    assert by_sym['BTC-USD'][3] == 'crypto'
    assert by_sym['BTC-USD'][1] == 'id-BTC/USD'
    assert by_sym['AAPL'][3] == 'us_equity'


def test_crypto_fetch_failure_keeps_equities():
    def run(args, **kw):
        if 'crypto' in args:
            return MagicMock(returncode=1, stdout='', stderr='boom')
        return MagicMock(returncode=0, stdout=json.dumps([_eq('AAPL')]), stderr='')
    with patch.object(rtu.subprocess, 'run', side_effect=run):
        assets, _ms, stats = rtu.fetch_all_assets(crypto_universe=_UNIVERSE)
    assert [a['symbol'] for a in assets] == ['AAPL']
    assert stats['crypto_error']


def test_deactivation_scoped_by_class():
    prior = [
        {'symbol': 'AAPL', 'tradable': True, 'status': 'active', 'asset_class': 'us_equity'},
        {'symbol': 'LEGACY', 'tradable': True, 'status': 'active', 'asset_class': None},
        {'symbol': 'BTC-USD', 'tradable': True, 'status': 'active', 'asset_class': 'crypto'},
        {'symbol': 'ETH-USD', 'tradable': True, 'status': 'active', 'asset_class': 'crypto'},
    ]
    # crypto-only run: ETH-USD dropped out; equities (incl. NULL-class legacy) untouched
    conn, cur = _conn(prior)
    btc = dict(_cr('BTC-USD'))
    with patch.object(rtu.psycopg2.extras, 'execute_values'):
        stats = rtu.upsert_universe(conn, [btc])
    assert stats['newly_inactive'] == 1
    call = _update_call(cur)
    assert 'asset_class' in call.args[0] and 'ANY' in call.args[0]
    assert call.args[1][1] == ['crypto']

    # equity-only run: LEGACY (NULL class ≡ us_equity) drops; crypto untouched
    conn, cur = _conn(prior)
    with patch.object(rtu.psycopg2.extras, 'execute_values'):
        stats = rtu.upsert_universe(conn, [_eq('AAPL')])
    assert stats['newly_inactive'] == 1
    call = _update_call(cur)
    assert call.args[1][1] == ['us_equity']


def test_class_blind_readers_are_equity_scoped():
    """Readers that took 'every row' before crypto rows existed must stay
    us_equity-only (flag-unset equity semantics unchanged): the backtest ETB
    gate (absent ⇒ ungated; crypto rows carry easy_to_borrow=false), the
    ticker-metadata snapshot writers that feed the universe resolver, and the
    backfill-universe builder."""
    guard = "COALESCE(asset_class, 'us_equity') = 'us_equity'"
    for rel in ('src/backtest/unified_backtest.py',
                'src/pipeline/run_ticker_metadata_step.py',
                'src/pipeline/ticker_metadata_writer.py',
                'scripts/build_backfill_universe.py'):
        src = (ROOT / rel).read_text()
        assert guard in src, rel


def test_crypto_counts_and_error_written_to_refresh_log_notes():
    ok = {'crypto_fetched': 40, 'crypto': 3, 'crypto_skipped_non_usd': 37,
          'crypto_unmapped_usd': 0, 'crypto_unmapped_symbols': [], 'crypto_error': None}
    notes = rtu.crypto_notes(ok)
    assert notes == 'crypto fetched=40 upserted=3 skipped_non_usd=37 unmapped_usd=0'

    bad = dict(ok, crypto_fetched=0, crypto=0, crypto_skipped_non_usd=0,
               crypto_error='CalledProcessError: boom')
    assert 'crypto_error=CalledProcessError: boom' in rtu.crypto_notes(bad)
    unm = dict(ok, crypto_unmapped_usd=1, crypto_unmapped_symbols=['ZZZ/USD'])
    assert 'unmapped_usd=1' in rtu.crypto_notes(unm) and 'ZZZ/USD' in rtu.crypto_notes(unm)

    conn, cur = _conn([])
    with patch.object(rtu.psycopg2.extras, 'execute_values'):
        rtu.upsert_universe(conn, [_eq('AAPL')], notes=notes)
    log = [c for c in cur.execute.call_args_list
           if 'alpaca_tradable_universe_refresh_log' in c.args[0]]
    assert len(log) == 1 and log[0].args[1][-1] == notes


def test_main_crypto_failure_warns_records_notes_and_exits_zero(monkeypatch, capsys):
    counts = {'us_equity': rtu.SUSPICIOUS_FLOOR, 'crypto_fetched': 0, 'crypto': 0,
              'crypto_skipped_non_usd': 0, 'crypto_unmapped_usd': 0,
              'crypto_unmapped_symbols': [], 'crypto_error': 'RuntimeError: down'}
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub/stub')
    monkeypatch.setattr(rtu, 'fetch_all_assets', lambda: ([_eq('AAPL')], 5, counts))
    seen = {}

    def fake_upsert(conn, assets, dry_run=False, notes=None):
        seen['notes'] = notes
        return {'total_active': 1, 'total_tradable': 1, 'newly_listed': 0,
                'newly_inactive': 0, 'status_changes': 0, 'dry_run': dry_run}

    monkeypatch.setattr(rtu, 'upsert_universe', fake_upsert)
    monkeypatch.setattr(rtu.psycopg2, 'connect', lambda *a, **k: MagicMock())
    monkeypatch.setattr(rtu.subprocess, 'run', lambda *a, **k: None)   # metadata chain
    monkeypatch.setattr(sys, 'argv', ['refresh_tradable_universe'])
    assert rtu.main() == 0
    assert 'crypto_error=RuntimeError: down' in seen['notes']
    out = capsys.readouterr().out
    assert 'WARN crypto asset list failed' in out
