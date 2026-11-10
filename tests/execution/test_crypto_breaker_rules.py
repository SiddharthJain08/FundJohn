import json
from datetime import datetime, timedelta, timezone

from execution import crypto_breaker as cb

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(s): s.d = {}; s.ttls = {}
    def get(s, k): return s.d.get(k)
    def set(s, k, v, ex=None): s.d[k] = v; s.ttls[k] = ex
    def delete(s, k): s.d.pop(k, None)
    def ttl(s, k): return s.ttls.get(k)


def _row(sym, ac='crypto', **kw):
    r = dict(symbol=sym, asset_class=ac, qty='1', avg_entry_price='100', current_price='100',
             market_value='100', cost_basis='100', unrealized_pl='0', unrealized_plpc='0')
    r.update(kw)
    return r


def _pos(ticker, cost, pl, plpc=None):
    return cb.CryptoPos(ticker, ticker, 1.0, None, None, cost + pl, cost, pl,
                        plpc if plpc is not None else pl / cost)


def test_positions_parse_both_symbol_forms():
    pos, c = cb.parse_positions([_row('BTCUSD'), _row('ETH/USD'), _row('AAPL', 'us_equity')])
    assert [p.ticker for p in pos] == ['BTC-USD', 'ETH-USD']
    assert c['equity_rows'] == 1 and c['crypto_rows'] == 2 and c['unparsed'] == 0


def test_position_missing_cost_basis_falls_back():
    r = _row('BTCUSD', qty='2', avg_entry_price='50', market_value='90', unrealized_pl='-10')
    del r['cost_basis'], r['unrealized_plpc']
    (p,), _ = cb.parse_positions([r])
    assert p.cost_basis == 100.0 and abs(p.unrealized_plpc + 0.1) < 1e-9
    bad, c = cb.parse_positions([_row('BTCUSD', qty='x')])
    assert bad == [] and c['unparsed'] == 1


def test_sleeve_dd_and_triggers():
    kw = dict(now=NOW, regime_ts=NOW, flow_ts=NOW)
    pos = [_pos('BTC-USD', 8000, -400, -0.05), _pos('ETH-USD', 2000, -500, -0.25)]
    ev = cb.evaluate(pos, **kw)
    assert abs(ev['sleeve_dd'] + 0.09) < 1e-9
    assert [t['id'] for t in ev['triggers']] == ['T4a']
    pos = [_pos('BTC-USD', 8000, 0, 0.0), _pos('ETH-USD', 2000, -300, -0.16)]
    ev = cb.evaluate(pos, **kw)
    assert abs(ev['sleeve_dd'] + 0.03) < 1e-9
    assert [(t['id'], t['ticker']) for t in ev['triggers']] == [('T4b', 'ETH-USD')]
    assert ev['worst_ticker'] == 'ETH-USD'
    assert cb.evaluate([], **kw)['sleeve_dd'] is None


def test_blackout_requires_both_sources_stale_and_positions():
    pos = [_pos('BTC-USD', 1000, 0, 0.0)]
    old, fresh = NOW - timedelta(hours=4), NOW - timedelta(hours=1)
    assert not cb.evaluate(pos, now=NOW, regime_ts=old, flow_ts=fresh)['blackout']
    ev = cb.evaluate(pos, now=NOW, regime_ts=old, flow_ts=old)
    assert ev['blackout'] and ev['triggers'][-1]['id'] == 'T5'
    assert cb.evaluate(pos, now=NOW, regime_ts=None, flow_ts=None)['blackout']


def test_mode_precedence():
    assert cb.mode({'OPENCLAW_CRYPTO_BREAKER': '1'}, dry_run=True) == 'dry_run'
    assert cb.mode({'OPENCLAW_CRYPTO_BREAKER': '1'}) == 'armed'
    assert cb.mode({}) == 'shadow' and cb.mode({'OPENCLAW_CRYPTO_BREAKER': '0'}) == 'shadow'


def test_exec_window():
    assert cb.in_exec_window(datetime(2026, 11, 10, 20, 58, tzinfo=UTC))
    assert not cb.in_exec_window(datetime(2026, 11, 10, 21, 6, tzinfo=UTC))
    assert not cb.in_exec_window(datetime(2026, 11, 14, 20, 58, tzinfo=UTC))


def test_cooldown_and_hold_keys():
    r = FakeRedis()
    assert not cb.cooldown_active(r, 'sleeve_drawdown', NOW)
    cb.set_cooldown(r, 'sleeve_drawdown', NOW)
    assert cb.cooldown_active(r, 'sleeve_drawdown', NOW)
    assert not cb.cooldown_active(r, 'data_blackout', NOW)
    k = 'crypto_breaker:fired:2026-11-10:sleeve_drawdown'
    assert r.ttl(k) == 21600
    h = cb.hold_flat_set(r, 'sleeve_drawdown', NOW)
    assert cb.hold_flat_get(r) == h and h['scope'] == 'sleeve'
    assert r.ttl('crypto_breaker:hold_flat') == 86400
    cb.hold_ticker_set(r, 'ETH-USD', 'position_drawdown:ETH-USD', NOW)
    assert cb.hold_ticker_get(r, 'ETH-USD')['ticker'] == 'ETH-USD'
    assert r.ttl('crypto_breaker:hold_ticker:ETH-USD') == 604800
    cb.clear_hold(r)
    assert cb.hold_flat_get(r) is None
    assert cb.hold_flat_get(None) is None and not cb.cooldown_active(None, 'x', NOW)
    cb.set_cooldown(None, 'x', NOW)


def test_rearm_token_scoped_to_since():
    hold = cb.hold_flat_set(None, 'sleeve_drawdown', NOW)
    assert cb.rearm_requested({cb.REARM_ENV: hold['since']}, hold)
    assert cb.rearm_requested({cb.REARM_ENV: '2026-11-10T20:00:00Z'}, hold)
    old = (NOW - timedelta(days=1)).isoformat()
    assert not cb.rearm_requested({cb.REARM_ENV: old}, hold)
    assert not cb.rearm_requested({cb.REARM_ENV: old}, None)


def test_parse_survives_normaliser_failure(monkeypatch):
    import ingestion.crypto_flow.universe as u

    def boom(*a, **k):
        raise FileNotFoundError('universe.json')
    monkeypatch.setattr(u, 'normalize_broker_symbol', boom)
    pos, c = cb.parse_positions([_row('ETH/USD'), _row('AAPL', 'us_equity')])
    assert [p.ticker for p in pos] == ['ETH-USD']
    assert c['normaliser_errors'] >= 1


import importlib
import pytest


@pytest.mark.parametrize('raw,expected', [('0.08', -0.08), ('nan', -0.08), ('abc', -0.08),
                                          ('-0.05', -0.05)])
def test_env_threshold_rejects_positive_and_nan(monkeypatch, raw, expected):
    monkeypatch.setenv('OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD', raw)
    assert cb._env_threshold('OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD', -0.08) == expected
    monkeypatch.setenv('OPENCLAW_CRYPTO_BREAKER_BLACKOUT_H', raw)
    assert cb._env_threshold('OPENCLAW_CRYPTO_BREAKER_BLACKOUT_H', 3.0,
                             must_be_negative=False) == (0.08 if raw == '0.08' else 3.0)


def test_zero_qty_row_skipped():
    pos, c = cb.parse_positions([_row('BTCUSD', qty='0', unrealized_plpc='-0.5')])
    assert pos == [] and c['zero_qty'] == 1
