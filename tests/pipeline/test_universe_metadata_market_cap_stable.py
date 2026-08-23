"""_market_cap_for must call FMP /stable/historical-market-capitalization.

The /api/v3/historical-market-capitalization/{sym} path it used returns 403
"Legacy Endpoint" for this key (the docstring's 2026-05-22 probe note was
the same 403, misread as a plan-tier limit). Stable payload verified
2026-08-23: [{"symbol","date","marketCap"}], from/to honoured, [] on a
non-trading date — so the window is widened to the trailing week and the
latest row on/before the snapshot date wins.
"""
from __future__ import annotations

from datetime import date

import requests

from src.pipeline.backfillers import universe_metadata as um


def _stub(monkeypatch, payload, status=200):
    calls = []

    class _R:
        status_code = status
        def json(self):
            return payload

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        return _R()

    monkeypatch.setattr(requests, 'get', fake_get)
    monkeypatch.setenv('FMP_API_KEY', 'k')
    monkeypatch.setattr(um, '_FMP_SLEEP_S', 0)
    return calls


def test_uses_stable_endpoint_and_symbol_param(monkeypatch):
    calls = _stub(monkeypatch, [{'symbol': 'AAPL', 'date': '2026-08-19', 'marketCap': 4655029527450}])
    mc = um._market_cap_for('AAPL', date(2026, 8, 19))
    assert mc == 4655029527450.0
    url, params = calls[0]
    assert url == 'https://financialmodelingprep.com/stable/historical-market-capitalization'
    assert 'api/v3' not in url
    assert params['symbol'] == 'AAPL' and params['apikey'] == 'k'
    assert params['to'] == '2026-08-19' and params['from'] == '2026-08-12'


def test_weekend_snapshot_takes_latest_row_on_or_before(monkeypatch):
    _stub(monkeypatch, [
        {'symbol': 'AAPL', 'date': '2026-05-29', 'marketCap': 100.0},
        {'symbol': 'AAPL', 'date': '2026-05-28', 'marketCap': 90.0},
        {'symbol': 'AAPL', 'date': '2026-06-01', 'marketCap': 999.0},   # after `on` — ignored
    ])
    assert um._market_cap_for('AAPL', date(2026, 5, 31)) == 100.0


def test_empty_or_non_200_is_none(monkeypatch):
    _stub(monkeypatch, [])
    assert um._market_cap_for('AAPL', date(2026, 8, 16)) is None
    _stub(monkeypatch, {'Error Message': 'Legacy Endpoint'}, status=403)
    assert um._market_cap_for('AAPL', date(2026, 8, 19)) is None
