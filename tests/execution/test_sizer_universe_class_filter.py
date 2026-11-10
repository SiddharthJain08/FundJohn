"""Class-filtered tradable universe consumers (crypto engine Phase 3, Task 4).

Crypto rows now live in alpaca_tradable_universe. The sizer's two signal
loaders admit them ONLY when OPENCLAW_CRYPTO_POOLED_SIZING=1; unset ⇒ the
universe is us_equity only, exactly as before crypto rows existed. The asset-
eligibility gate never touches '-USD' crypto pairs (not marginable/borrowable
by design).
"""
from __future__ import annotations

import importlib
from datetime import date

import pytest

rbs = importlib.import_module('execution.regime_blended_sizer')
tu = importlib.import_module('execution.tradable_universe')
ps = importlib.import_module('execution.pooled_sizing')
# conftest stubs the module attribute per test; keep the pristine function
_PRISTINE_LOAD_ELIG = rbs._load_asset_eligibility

_UNIVERSE = {'AAPL': 'us_equity', 'BTC-USD': 'crypto', 'OLD': None}


class _Cur:
    def __init__(self, log):
        self.log = log
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if 'easy_to_borrow' in sql:
            self._rows = [(s, True) for s in params[0]]
        elif 'alpaca_tradable_universe' in sql:
            classes = params[0]
            self._rows = [(s,) for s, c in _UNIVERSE.items()
                          if (c or 'us_equity') in classes]
        elif 'execution_signals' in sql and 'DISTINCT ON' in sql:
            today = date.today()
            self._rows = [
                {'strategy_id': 'S_x', 'ticker': t, 'direction': 'LONG',
                 'signal_date': today, 'target_date': today, 'entry_price': 1.0,
                 'stop_loss': 0.9, 'target_1': 1.1, 'target_2': 1.2,
                 'signal_params': {}}
                for t in ('AAPL', 'BTC-USD')]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, **kw):
        return _Cur(self.log)


@pytest.fixture
def fake_db(monkeypatch):
    import psycopg2
    log = []
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: _Conn(log))
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub/stub')
    monkeypatch.delenv('OPENCLAW_CRYPTO_POOLED_SIZING', raising=False)
    return log


def _tickers(rows):
    return sorted(r['ticker'] for r in rows)


def test_pooled_sizing_flag_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CRYPTO_POOLED_SIZING', raising=False)
    assert ps.pooled_sizing_enabled() is False
    monkeypatch.setenv('OPENCLAW_CRYPTO_POOLED_SIZING', '1')
    assert ps.pooled_sizing_enabled() is True
    assert ps.pooled_sizing_enabled({'OPENCLAW_CRYPTO_POOLED_SIZING': '0'}) is False
    assert ps.pooled_sizing_enabled({}) is False


def test_load_tradable_symbols_default_is_equity_only(fake_db):
    syms = tu.load_tradable_symbols(_Conn(fake_db))
    assert syms == {'AAPL', 'OLD'}          # NULL class ≡ us_equity (legacy rows)
    sql, params = fake_db[-1]
    assert "status='active'" in sql.replace(' ', '') or "status = 'active'" in sql
    assert params == (['us_equity'],)


@pytest.mark.parametrize('loader', ['active_window', 'approved_carried'])
def test_sizer_loader_filters_us_equity_when_not_pooled(fake_db, loader):
    rows = _call(loader)
    assert _tickers(rows) == ['AAPL']
    uni = [p for s, p in fake_db if 'alpaca_tradable_universe' in s]
    assert uni == [(['us_equity'],)]


@pytest.mark.parametrize('loader', ['active_window', 'approved_carried'])
def test_sizer_loader_includes_crypto_when_pooled(fake_db, monkeypatch, loader):
    monkeypatch.setenv('OPENCLAW_CRYPTO_POOLED_SIZING', '1')
    rows = _call(loader)
    assert _tickers(rows) == ['AAPL', 'BTC-USD']
    uni = [p for s, p in fake_db if 'alpaca_tradable_universe' in s]
    assert uni == [(['us_equity', 'crypto'],)]


def _call(loader):
    if loader == 'active_window':
        return rbs._load_active_window_signals(None, {'S_x': 1.0}, {'S_x': 1.0})
    return rbs._load_approved_carried_signals({'S_x': 1.0}, {'S_x': 1.0}, None)


def test_eligibility_gate_skips_usd_pairs(monkeypatch):
    asked = []

    def fake_load(symbols):
        asked.append(list(symbols))
        return {s: False for s in symbols}     # everything asked is ineligible

    monkeypatch.setattr(rbs, '_load_asset_eligibility', fake_load)
    monkeypatch.delenv('OPENCLAW_SIZER_ASSET_GATE', raising=False)
    out = rbs._apply_asset_eligibility_gate(
        {'XXII': 5000.0, 'BTC-USD': 3000.0, 'ETH/USD': 100.0}, {})
    assert asked == [['XXII']]
    assert out == {'BTC-USD': 3000.0, 'ETH/USD': 100.0}


def test_load_asset_eligibility_never_queries_usd_pairs(fake_db):
    out = _PRISTINE_LOAD_ELIG(['XXII', 'BTC-USD'])
    assert out == {'XXII': True}
    asked = [p for s, p in fake_db if 'easy_to_borrow' in s]
    assert asked == [(['XXII'],)]
