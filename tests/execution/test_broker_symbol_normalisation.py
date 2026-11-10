"""Crypto engine Phase 3, Task 5 (D-1): one broker-symbol normaliser across the
sizer / crypto redeploy / open reconcile / account breaker / liquidator, and a
class-aware orphan pass (Review Focus 2: with OPENCLAW_CRYPTO_POOLED_SIZING
unset a crypto position at the broker must NOT be orphan-closed by the pooled
path — the legacy crypto lane owns it).

Pure fakes only: subprocess / CLI are monkeypatched, no broker, no DB."""
import json
import logging

import pytest

import execution.regime_blended_sizer as _sizer
import execution.crypto_redeploy_sizer as _crs
import execution.open_reconcile as _orc
import execution.account_breaker as _ab
import execution.regime_liquidator as _liq
import execution.premarket_gate as _pg
from ingestion.crypto_flow import universe as _u

FLAG = 'OPENCLAW_CRYPTO_POOLED_SIZING'
BROKER_FORMS = ['BTCUSD', 'BTC/USD', 'BTC-USD', 'btc/usd']


# ── the shared wrapper ────────────────────────────────────────────────────
@pytest.mark.parametrize('sym', BROKER_FORMS)
def test_internal_symbol_crypto_forms(sym):
    assert _u.internal_symbol(sym) == 'BTC-USD'


@pytest.mark.parametrize('sym', ['AAPL', 'SPY', 'BRK.B', ' msft '])
def test_internal_symbol_equity_passthrough(sym):
    assert _u.internal_symbol(sym) == sym.strip().upper()


def test_internal_symbol_unknown_pair():
    # a pair the universe file does not know: the slash form still maps (legacy
    # behaviour), the compact form maps only when the caller says it is crypto.
    assert _u.internal_symbol('ZZQ/USD') == 'ZZQ-USD'
    assert _u.internal_symbol('ZZQUSD') == 'ZZQUSD'
    assert _u.internal_symbol('ZZQUSD', crypto=True) == 'ZZQ-USD'


def test_internal_symbol_survives_universe_failure(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError('universe.json')
    monkeypatch.setattr(_u, 'normalize_broker_symbol', boom)
    assert _u.internal_symbol('BTC/USD') == 'BTC-USD'
    assert _u.internal_symbol('BTCUSD', crypto=True) == 'BTC-USD'
    assert _u.internal_symbol('AAPL') == 'AAPL'


# ── per-module normalisation ──────────────────────────────────────────────
@pytest.mark.parametrize('sym', BROKER_FORMS)
def test_redeploy_sizer_normalises_both_forms(sym):
    assert _crs._normalize_broker_symbol(sym) == 'BTC-USD'
    assert _crs._normalize_broker_symbol('AAPL') == 'AAPL'


@pytest.mark.parametrize('sym', BROKER_FORMS)
def test_open_reconcile_normalises_both_forms(sym):
    assert _orc._normalize_broker_symbol(sym) == 'BTC-USD'
    assert _orc._normalize_broker_symbol('aapl') == 'AAPL'


def _patch_cli(monkeypatch, positions):
    class _Proc:
        returncode = 0
        stdout = json.dumps(positions).encode()
        stderr = b''
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _Proc())


@pytest.mark.parametrize('sym', ['BTCUSD', 'BTC/USD'])
@pytest.mark.parametrize('with_class', [True, False])
def test_sizer_loader_keys_crypto_internal(monkeypatch, sym, with_class):
    rows = [{'symbol': 'AAPL', 'qty': '10', 'market_value': '1500'},
            {'symbol': sym, 'qty': '0.5', 'market_value': '30000'},
            {'symbol': 'SPY260626C00755000', 'qty': '1', 'market_value': '100'}]
    if with_class:
        rows[0]['asset_class'] = 'us_equity'
        rows[1]['asset_class'] = 'crypto'
    _patch_cli(monkeypatch, rows)
    pos, cls = _sizer._load_broker_positions_usd_with_class()
    assert pos == {'AAPL': 1500.0, 'BTC-USD': 30000.0}
    assert cls == {'AAPL': 'us_equity', 'BTC-USD': 'crypto'}
    assert _sizer._load_broker_positions_usd() == pos
    assert _sizer._broker_asset_classes == cls


def test_sizer_loader_unknown_crypto_pair_still_internal(monkeypatch):
    _patch_cli(monkeypatch, [{'symbol': 'ZZQUSD', 'qty': '3', 'market_value': '30',
                              'asset_class': 'crypto'}])
    pos, cls = _sizer._load_broker_positions_usd_with_class()
    assert pos == {'ZZQ-USD': 30.0} and cls == {'ZZQ-USD': 'crypto'}


def test_sizer_loader_failure_is_empty(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = b''
        stderr = b'nope'
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: _Proc())
    assert _sizer._load_broker_positions_usd_with_class() == ({}, {})
    assert _sizer._load_broker_positions_usd() == {}


@pytest.mark.parametrize('sym', ['BTCUSD', 'BTC/USD'])
def test_account_breaker_classifies_both_forms_crypto(sym):
    # no broker payload (closed position's fill) → the shape guess must still see crypto
    assert _ab._symbol_class(sym) == 'crypto'
    assert not _ab._is_equity_position(sym)
    assert _ab._symbol_class('AAPL') == 'us_equity'
    assert _ab._pos_key(sym) == 'BTCUSD'      # key contract unchanged


@pytest.mark.parametrize('sym', ['BTCUSD', 'BTC/USD'])
@pytest.mark.parametrize('with_class', [True, False])
def test_liquidator_reader_tags_internal_ticker_and_class(monkeypatch, sym, with_class):
    row = {'symbol': sym, 'qty': '0.5', 'side': 'long', 'market_value': '30000'}
    if with_class:
        row['asset_class'] = 'crypto'
    rows = [row, {'symbol': 'AAPL', 'qty': '1', 'side': 'long', 'market_value': '150',
                  'asset_class': 'us_equity'}]
    monkeypatch.setattr(_liq, '_run_cli', lambda *a, **k: (True, rows, None))
    out = _liq._load_broker_positions()
    assert set(out) == {sym, 'AAPL'}              # keys stay broker-form (close CLI)
    assert out[sym]['ticker'] == 'BTC-USD' and out[sym]['asset_class'] == 'crypto'
    assert out['AAPL']['ticker'] == 'AAPL' and out['AAPL']['asset_class'] == 'us_equity'


def test_premarket_gate_excludes_internal_crypto_keys():
    book = {'AAPL': 1000.0, 'BTC-USD': 5000.0, 'BTC/USD': 1.0, 'SPY260626C00755000': 10.0}
    subjects = _pg._load_held_subjects(broker_loader=lambda: book)
    assert [s['ticker'] for s in subjects] == ['AAPL']


# ── class-aware orphan pass (Review Focus 2) ──────────────────────────────
def test_orphan_pass_ignores_crypto_positions_when_not_pooled(monkeypatch, caplog):
    monkeypatch.delenv(FLAG, raising=False)
    broker = {'AAPL': 1500.0, 'BTC-USD': 30000.0, 'ETH/USD': 900.0}
    meta = {}
    with caplog.at_level(logging.INFO):
        em = _sizer._classify_position_deltas({}, broker, meta)
    assert em == [('AAPL', -1500.0, 'orphan_close')]
    assert 'BTC-USD' not in meta and 'ETH/USD' not in meta
    assert any('orphan_pass: crypto_skipped=2' in r.message for r in caplog.records)


def test_orphan_pass_explicit_asset_classes_win(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    em = _sizer._classify_position_deltas(
        {}, {'XYZ': 10.0, 'AAPL': 5.0}, {}, asset_classes={'XYZ': 'crypto'})
    assert em == [('AAPL', -5.0, 'orphan_close')]


def test_orphan_pass_includes_crypto_when_pooled(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    broker = {'AAPL': 1500.0, 'BTC-USD': 30000.0}
    em = _sizer._classify_position_deltas({}, broker, {}, pooled=True)
    assert sorted(em) == [('AAPL', -1500.0, 'orphan_close'),
                          ('BTC-USD', -30000.0, 'orphan_close')]
    # default resolves from the flag
    monkeypatch.setenv(FLAG, '1')
    meta = {}
    em2 = _sizer._classify_position_deltas({}, broker, meta)
    assert sorted(em2) == sorted(em)
    assert meta['BTC-USD']['strategies'] == ['__close_orphan__']


def test_orphan_pass_equity_only_unchanged_and_silent(monkeypatch, caplog):
    monkeypatch.delenv(FLAG, raising=False)
    with caplog.at_level(logging.INFO):
        em = _sizer._classify_position_deltas({'MSFT': 100.0}, {'AAPL': 50.0, 'MSFT': 40.0}, {})
    assert em == [('MSFT', 60.0, 'delta'), ('AAPL', -50.0, 'orphan_close')]
    assert not any('orphan_pass' in r.message for r in caplog.records)


def test_crypto_target_delta_still_emitted_when_not_pooled(monkeypatch):
    # the skip is ORPHAN-only: a crypto key that IS targeted is netted as before
    monkeypatch.delenv(FLAG, raising=False)
    em = _sizer._classify_position_deltas({'BTC-USD': 100.0}, {'BTC-USD': 40.0}, {})
    assert em == [('BTC-USD', 60.0, 'delta')]
