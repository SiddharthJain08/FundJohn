"""Every daily Python FMP call site records into data_provider_health
(2026-08-23). Stubs requests; asserts the recorder saw each call with the
right success/error split."""
from __future__ import annotations

import types

import pandas as pd
import pytest


class _Resp:
    def __init__(self, status, body='', payload=None):
        self.status_code = status; self.text = body; self._p = payload if payload is not None else []
    def json(self): return self._p
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'{self.status_code} error')


@pytest.fixture
def recorded(monkeypatch):
    from src.maintenance import provider_health as ph
    calls = []
    monkeypatch.setattr(ph, 'record', lambda p, e, *, success, error=None: calls.append((p, e, success, error)))
    return calls


def test_insider_stream_records_each_page(monkeypatch, recorded):
    from src.ingestion import intraday_insider as mod
    monkeypatch.setenv('FMP_API_KEY', 'k')
    pages = [
        _Resp(200, '', [{'symbol': 'AAPL', 'filingDate': '2026-08-21', 'transactionDate': '2026-08-20',
                         'reportingName': 'X', 'typeOfOwner': 'officer', 'transactionType': 'P-Purchase',
                         'securitiesTransacted': 10, 'price': 1.0, 'securitiesOwned': 100}]),
        _Resp(200, '', []),
    ]
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: pages.pop(0))
    rows, stats = mod.fetch_latest_filings('2026-08-19')
    assert len(rows) == 1
    assert recorded == [('fmp', 'insider_trading_latest', True, None)] * 2


def test_insider_stream_records_page0_failure(monkeypatch, recorded):
    from src.ingestion import intraday_insider as mod
    monkeypatch.setenv('FMP_API_KEY', 'k')
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: _Resp(429, 'Too Many Requests'))
    with pytest.raises(mod.IntradayInsiderError):
        mod.fetch_latest_filings('2026-08-19')
    assert recorded == [('fmp', 'insider_trading_latest', False, 'HTTP 429: Too Many Requests')]


def test_intraday_financials_calendar_records(monkeypatch, recorded):
    from src.ingestion import intraday_financials as mod
    monkeypatch.setenv('FMP_API_KEY', 'k')
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: _Resp(200, '', [{'symbol': 'AAPL', 'date': '2026-08-21', 'epsActual': 1.5}]))
    out = mod.reporters(pd.Timestamp('2026-08-21'), ['AAPL', 'MSFT'])
    assert out == ['AAPL']
    assert recorded == [('fmp', 'earnings_calendar', True, None)]


def test_refresh_fmp_profiles_records(monkeypatch, recorded):
    import importlib
    mod = importlib.import_module('scripts.refresh_fmp_profiles')
    monkeypatch.setattr(mod, '_http_get', lambda url, params=None, timeout=None: _Resp(200, '', [{'symbol': 'AAPL', 'sector': 'Technology'}]))
    assert mod.fetch_profile('AAPL', 'k')['sector'] == 'Technology'
    monkeypatch.setattr(mod, '_http_get', lambda url, params=None, timeout=None: _Resp(402, "Special Endpoint: This value set for 'symbol' is not available under your current subscription"))
    mod.fetch_profile('ABR.PRD', 'k')
    assert recorded == [('fmp', 'profile', True, None), ('fmp', 'profile', True, None)]
