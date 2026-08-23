"""scripts/refresh_fmp_profiles.py — producer for data/.cache/fmp_profile.json.

run_ticker_metadata_step.py has read this cache since SP-2 Phase A, but
nothing ever wrote it (grep fmp_profile: consumer + writer only), so every
ticker_metadata_snapshots row has sector/industry NULL (14,254 rows on
2026-08-21, 0 sectors). The producer fetches FMP /stable/profile?symbol=
per name (the batch form returns []), refreshes only missing or >30d-old
entries, and writes atomically.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from scripts import refresh_fmp_profiles as mod

NOW = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)


def _entry(days_old: int, **kw):
    return {'_fetched_at': (NOW - timedelta(days=days_old)).isoformat(), **kw}


def test_needs_refresh_missing_stale_fresh():
    assert mod.needs_refresh(None, NOW, 30)
    assert mod.needs_refresh({'sector': 'Tech'}, NOW, 30)          # legacy: no stamp
    assert mod.needs_refresh(_entry(31, sector='Tech'), NOW, 30)
    assert not mod.needs_refresh(_entry(29, sector='Tech'), NOW, 30)
    assert not mod.needs_refresh(_entry(1, _empty=True), NOW, 30)  # tombstone honoured


def test_select_symbols_orders_missing_first_then_stalest_and_caps():
    cache = {'A': _entry(40), 'B': _entry(5), 'C': _entry(35)}
    out = mod.select_symbols(['A', 'B', 'C', 'D', 'E'], cache, NOW, 30, limit=None)
    assert out == ['D', 'E', 'A', 'C']                # B is fresh -> skipped
    assert mod.select_symbols(['A', 'B', 'C', 'D', 'E'], cache, NOW, 30, limit=2) == ['D', 'E']


def test_normalize_profile_keeps_writer_fields_and_mktcap_alias():
    raw = {'symbol': 'AAPL', 'sector': 'Technology', 'industry': 'Consumer Electronics',
           'marketCap': 4545129515250, 'ipoDate': '1980-12-12', 'isEtf': False,
           'isActivelyTrading': True, 'cik': '0000320193', 'exchange': 'NASDAQ',
           'companyName': 'Apple Inc.', 'description': 'x' * 5000, 'image': 'http://...'}
    p = mod.normalize_profile(raw, NOW)
    # ticker_metadata_writer reads sector / industry / mktCap / ipoDate
    assert p['sector'] == 'Technology' and p['industry'] == 'Consumer Electronics'
    assert p['mktCap'] == 4545129515250 and p['marketCap'] == 4545129515250
    assert p['ipoDate'] == '1980-12-12'
    assert p['_fetched_at'] == NOW.isoformat()
    assert 'description' not in p and 'image' not in p   # keep the cache small


def test_normalize_empty_payload_is_tombstone():
    p = mod.normalize_profile(None, NOW)
    assert p == {'_fetched_at': NOW.isoformat(), '_empty': True}


def test_atomic_write_json(tmp_path):
    target = tmp_path / 'fmp_profile.json'
    mod.atomic_write_json(target, {'A': {'sector': 'X'}})
    mod.atomic_write_json(target, {'A': {'sector': 'Y'}})
    assert json.loads(target.read_text()) == {'A': {'sector': 'Y'}}
    assert [p.name for p in tmp_path.iterdir()] == ['fmp_profile.json']


def test_fetch_profile_maps_status(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params)))
        sym = params['symbol']
        return {'AAPL': _Resp(200, [{'symbol': 'AAPL', 'sector': 'Technology'}]),
                'NOPE': _Resp(200, []),
                'BAD': _Resp(403, {'Error Message': 'x'})}[sym]

    monkeypatch.setattr(mod, '_http_get', fake_get)
    monkeypatch.setattr(mod, 'RETRY_BACKOFFS_S', ())
    assert mod.fetch_profile('AAPL', 'k')['sector'] == 'Technology'
    assert mod.fetch_profile('NOPE', 'k') is None
    try:
        mod.fetch_profile('BAD', 'k')
    except mod.FMPAuthError:
        pass
    else:
        raise AssertionError('403 must raise FMPAuthError (stop the run, key problem)')
    assert calls[0][0] == 'https://financialmodelingprep.com/stable/profile'
    assert calls[0][1] == {'symbol': 'AAPL', 'apikey': 'k'}


def test_selection_counters_separate_fresh_from_limit_cut():
    """'skipped_fresh' must not absorb names merely cut by --limit — the
    first smoke printed skipped_fresh=13371 with a 23-entry cache."""
    from datetime import datetime, timezone
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    cache = {'A': {'_fetched_at': '2026-08-22T00:00:00+00:00', 'sector': 'x'}}   # fresh
    universe = ['A', 'B', 'C', 'D']                                               # B,C,D missing
    c = mod.selection_counters(universe, cache, now, max_age_days=30, limit=2)
    assert c == {'universe': 4, 'stale': 3, 'to_fetch': 2, 'skipped_fresh': 1, 'deferred_by_limit': 1}
