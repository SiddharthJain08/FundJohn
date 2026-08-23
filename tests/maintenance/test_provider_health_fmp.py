"""data_provider_health instrumentation for FMP (2026-08-23).

FMP was the one live provider the dashboard Data Health tile never saw — no
FMP call site recorded. Recording needs a classification first: on the
Starter tier a 402 is EITHER quota ("provider unhealthy") OR a tier-gated
SYMBOL ("provider fine, symbol not available"); 404 is "no data". Only
genuine provider failures may count as errors, or the tile cries wolf on the
~30 preferred/warrant names every cycle.
"""
from __future__ import annotations

from src.maintenance import provider_health as ph

GATED = "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription"


def test_classify_http_distinguishes_fmp_402s():
    assert ph.classify_http(402, GATED) == 'symbol_gated'
    assert ph.classify_http(402, 'Limit Reach. Please upgrade your plan') == 'quota'
    assert ph.classify_http(402, '') == 'quota'
    assert ph.classify_http(429, '') == 'rate_limited'
    assert ph.classify_http(404, '') == 'not_found'
    assert ph.classify_http(200, '[]') == 'ok'
    assert ph.classify_http(500, 'boom') == 'error'
    assert ph.classify_http(None, '') == 'error'          # transport failure


def test_is_provider_error_only_for_real_failures():
    assert ph.is_provider_error('symbol_gated') is False
    assert ph.is_provider_error('not_found') is False
    assert ph.is_provider_error('ok') is False
    assert ph.is_provider_error('quota') is True
    assert ph.is_provider_error('rate_limited') is True
    assert ph.is_provider_error('error') is True


def test_endpoint_tag_normalises_paths():
    assert ph.endpoint_tag('income-statement') == 'income_statement'
    assert ph.endpoint_tag('/stable/insider-trading/search?symbol=AAPL') == 'insider_trading_search'
    assert ph.endpoint_tag('https://financialmodelingprep.com/stable/earnings-calendar?from=x') == 'earnings_calendar'
    assert ph.endpoint_tag('historical-price-eod/full') == 'historical_price_eod_full'


def test_record_http_routes_to_record(monkeypatch):
    calls = []
    monkeypatch.setattr(ph, 'record', lambda p, e, *, success, error=None: calls.append((p, e, success, error)))
    assert ph.record_http('fmp', 'ratios', 200, '') == 'ok'
    assert ph.record_http('fmp', 'income-statement', 402, GATED) == 'symbol_gated'
    assert ph.record_http('fmp', 'quote', 429, 'slow down') == 'rate_limited'
    assert ph.record_http('fmp', 'quote', None, 'ReadTimeout') == 'error'
    assert calls == [
        ('fmp', 'ratios', True, None),
        ('fmp', 'income_statement', True, None),            # gated symbol: provider healthy
        ('fmp', 'quote', False, 'HTTP 429: slow down'),
        ('fmp', 'quote', False, 'ReadTimeout'),
    ]
