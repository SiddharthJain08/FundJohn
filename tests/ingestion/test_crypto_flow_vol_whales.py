import json
import time as _time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.ingestion.crypto_flow import universe
from src.ingestion.crypto_flow.providers import base, deribit_vol, hl_whales
from src.ingestion.crypto_flow.providers.base import ProviderFetchError
from src.ingestion.crypto_flow.providers.deribit_vol import DeribitVol, parse_instrument, summarize_options
from src.ingestion.crypto_flow.providers.hl_whales import HlWhales, aggregate_positions, parse_leaderboard

FX = Path(__file__).parent / 'fixtures' / 'crypto_flow'
NOW = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)
ALL = universe.load_universe()
A = {a.ticker: a for a in ALL}
SET = [A[t] for t in ('BTC-USD', 'ETH-USD', 'SOL-USD', 'PEPE-USD', 'DOGE-USD')]
C2T = {a.hyperliquid: a.ticker for a in ALL if a.hyperliquid}


def fx(name):
    return json.loads((FX / name).read_text())['payload']


class Router:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw.get('body')))
        for key, val in self.routes.items():
            if key in url:
                return val(url, kw) if callable(val) else val
        return None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(hl_whales.time, 'sleep', lambda s: None)


def test_instrument_name_parse():
    assert parse_instrument('BTC-26DEC26-80000-C') == ('BTC', date(2026, 12, 26), 80000.0, 'C')
    assert parse_instrument('ETH-9NOV26-2500-P') == ('ETH', date(2026, 11, 9), 2500.0, 'P')
    with pytest.raises(ValueError):
        parse_instrument('BTC-PERPETUAL')


def test_atm_iv_picks_30d_expiry_and_nearest_strike():
    rows = fx('deribit_book_summary.json')['result']
    s = summarize_options(rows, NOW)
    assert s['atm_expiry'] == '2026-11-27'  # 20.5 d vs 48.5 d: nearer 30
    want = [r['mark_iv'] for r in rows if r['instrument_name'].startswith('BTC-27NOV26-76000-')]
    assert len(want) == 2 and s['atm_iv_30d'] == pytest.approx(sum(want) / 2)
    put = next(r['mark_iv'] for r in rows if r['instrument_name'] == 'BTC-27NOV26-68000-P')
    call = next(r['mark_iv'] for r in rows if r['instrument_name'] == 'BTC-27NOV26-84000-C')
    assert s['skew_25d'] == pytest.approx(put - call)
    assert s['n_options'] == 20


def test_weekly_expiry_under_7d_is_ignored():
    rows = [{'instrument_name': f'BTC-9NOV26-100-{k}', 'mark_iv': 99.0, 'open_interest': 1, 'underlying_price': 100.0}
            for k in 'CP']
    assert summarize_options(rows, NOW)['atm_iv_30d'] is None  # 2.5 d only
    rows += [{'instrument_name': f'BTC-4DEC26-100-{k}', 'mark_iv': 50.0, 'open_interest': 1, 'underlying_price': 100.0}
             for k in 'CP']
    assert summarize_options(rows, NOW)['atm_iv_30d'] == 50.0


def test_put_call_oi_ratio():
    rows = fx('deribit_book_summary.json')['result']
    p = sum(r['open_interest'] for r in rows if r['instrument_name'].endswith('-P'))
    c = sum(r['open_interest'] for r in rows if r['instrument_name'].endswith('-C'))
    assert summarize_options(rows, NOW)['put_call_oi'] == pytest.approx(p / c)
    assert summarize_options([r for r in rows if r['instrument_name'].endswith('-P')], NOW)['put_call_oi'] is None


def _deribit_router(btc_dvol=True, eth_dvol=True):
    def dvol(url, kw):
        ok = btc_dvol if 'currency=BTC' in url else eth_dvol
        return fx('deribit_dvol.json') if ok else None
    return Router({'get_volatility_index_data': dvol, 'get_book_summary_by_currency': fx('deribit_book_summary.json')})


def test_deribit_fetch_emits_btc_and_eth(monkeypatch):
    r = _deribit_router()
    monkeypatch.setattr(deribit_vol, 'get_json', r)
    res = DeribitVol().fetch(SET, NOW, NOW)
    assert res.ok and {o.ticker for o in res.observations} == {'BTC-USD', 'ETH-USD'}
    o = res.observations[0]
    assert o.fields['options_degraded'] is False
    assert o.source == 'deribit_vol' and o.fields['dvol'] == 39.58 and o.fields['n_options'] == 20
    assert DeribitVol.cadence == 'tick'


def test_dvol_one_currency_failing_skips_not_raises(monkeypatch):
    monkeypatch.setattr(deribit_vol, 'get_json', _deribit_router(eth_dvol=False))
    res = DeribitVol().fetch(SET, NOW, NOW)
    assert [o.ticker for o in res.observations] == ['BTC-USD']
    monkeypatch.setattr(deribit_vol, 'get_json', _deribit_router(False, False))
    res = base.run_provider(DeribitVol(), SET, NOW, NOW)
    assert not res.ok and 'dvol None' in res.err


def test_parse_leaderboard_top_n_sorted_by_account_value():
    rows = fx('hl_leaderboard.json')
    top = parse_leaderboard(rows, 2)
    assert top == ['0xa822a9ceb6d6cb5b565bd10098abcfa9cf18d748', '0x1c498a93b145e7a73d69691e9023f6f308e1cc3f']
    assert parse_leaderboard(None, 3) == []


def test_hl_whales_signed_sum_by_coin():
    short = fx('hl_clearinghouse.json')
    long_state = {'assetPositions': [
        {'position': {'coin': 'BTC', 'szi': '10', 'positionValue': '1000000'}},
        {'position': {'coin': 'kPEPE', 'szi': '5', 'positionValue': '200'}},
        {'position': {'coin': 'ZZZNOPE', 'szi': '5', 'positionValue': '777'}}]}  # not in universe: dropped
    agg = aggregate_positions([short, long_state, {'assetPositions': []}], C2T)
    btc_short = next(float(p['position']['positionValue']) for p in short['assetPositions']
                     if p['position']['coin'] == 'BTC')
    b = agg['BTC-USD']
    assert b['whale_perp_net_usd'] == pytest.approx(1_000_000 - btc_short)
    assert (b['whale_perp_long_usd'], b['whale_perp_n_long'], b['whale_perp_n_short']) == (1_000_000, 1, 1)
    assert b['whale_perp_short_usd'] == pytest.approx(btc_short)
    assert agg['PEPE-USD']['whale_perp_net_usd'] == 200 and set(agg) == {'BTC-USD', 'ETH-USD', 'SOL-USD', 'DOGE-USD', 'PEPE-USD'}


def _hl_router(lb, state):
    return Router({'leaderboard': lb, 'api.hyperliquid.xyz/info': state})


def test_hl_whales_hourly_cadence_flag(monkeypatch, tmp_path):
    assert HlWhales.cadence == 'hourly' and HlWhales.name == 'hl_whales'
    r = _hl_router(fx('hl_leaderboard.json'), fx('hl_clearinghouse.json'))
    monkeypatch.setattr(hl_whales, 'get_json', r)
    monkeypatch.setenv('OPENCLAW_CRYPTO_HL_WHALES_TOP_N', '3')
    p = HlWhales()
    p.state_path = tmp_path / 's.json'
    res = p.fetch(SET, NOW, NOW)
    assert res.ok
    posts = [b for u, b in r.calls if b]
    assert len(posts) == 3 and posts[0]['type'] == 'clearinghouseState'
    by = {o.ticker: o.fields for o in res.observations}
    assert by['BTC-USD']['whale_perp_accounts_seen'] == 3 and by['BTC-USD']['whale_perp_n_short'] == 3
    assert set(by) == {'BTC-USD', 'ETH-USD', 'SOL-USD', 'DOGE-USD'}  # BNB has no universe hyperliquid name: dropped
    assert json.loads(p.state_path.read_text())['hl_whales_leaderboard']['addresses']


def _warm(tmp_path, monkeypatch):
    p = HlWhales()
    p.state_path = tmp_path / 's.json'
    r = _hl_router(fx('hl_leaderboard.json'), fx('hl_clearinghouse.json'))
    monkeypatch.setattr(hl_whales, 'get_json', r)
    p.fetch(SET, NOW, NOW)
    return p, r


def test_leaderboard_cache_fresh_skips_refetch(monkeypatch, tmp_path):
    p, r = _warm(tmp_path, monkeypatch)
    p.fetch(SET, NOW, NOW)
    assert sum('leaderboard' in u for u, _ in r.calls) == 1


def test_leaderboard_stale_cache_used_when_refetch_fails(monkeypatch, tmp_path, capsys):
    p, _ = _warm(tmp_path, monkeypatch)
    monkeypatch.setattr(hl_whales, 'get_json', _hl_router(None, fx('hl_clearinghouse.json')))
    real = _time.time
    monkeypatch.setattr(hl_whales.time, 'time', lambda: real() + 25 * 3600)
    res = p.fetch(SET, NOW, NOW)
    assert res.ok and res.observations
    assert 'using cached list age=25.0h' in capsys.readouterr().out


def test_leaderboard_none_and_no_cache_raises(monkeypatch, tmp_path):
    p = HlWhales()
    p.state_path = tmp_path / 's.json'
    monkeypatch.setattr(hl_whales, 'get_json', _hl_router(None, fx('hl_clearinghouse.json')))
    with pytest.raises(ProviderFetchError, match='leaderboard None'):
        p.fetch(SET, NOW, NOW)


def test_early_abort_on_majority_failure(monkeypatch, tmp_path):
    p = HlWhales()
    p.state_path = tmp_path / 's.json'
    lb = {'leaderboardRows': [{'ethAddress': f'0x{i}', 'accountValue': str(100 - i)} for i in range(10)]}
    r = _hl_router(lb, None)
    monkeypatch.setattr(hl_whales, 'get_json', r)
    monkeypatch.setenv('OPENCLAW_CRYPTO_HL_WHALES_TOP_N', '10')
    res = base.run_provider(p, SET, NOW, NOW)
    assert not res.ok and '6/10' in res.err
    assert sum(1 for _, b in r.calls if b) == 6


def test_zero_mark_iv_treated_as_absent():
    rows = [{'instrument_name': 'BTC-4DEC26-100-C', 'mark_iv': 0, 'open_interest': 1, 'underlying_price': 100.0},
            {'instrument_name': 'BTC-4DEC26-100-P', 'mark_iv': 55.0, 'open_interest': 1, 'underlying_price': 100.0}]
    assert summarize_options(rows, NOW)['atm_iv_30d'] == 55.0


def test_book_none_flags_options_degraded(monkeypatch):
    monkeypatch.setattr(deribit_vol, 'get_json', Router({
        'get_volatility_index_data': fx('deribit_dvol.json'), 'get_book_summary_by_currency': None}))
    o = DeribitVol().fetch(SET, NOW, NOW).observations[0]
    assert o.fields['dvol'] == 39.58 and o.fields['options_degraded'] is True and o.fields['n_options'] == 0


def test_more_than_half_accounts_failing_raises(monkeypatch, tmp_path):
    p = HlWhales()
    p.state_path = tmp_path / 's.json'
    monkeypatch.setattr(hl_whales, 'get_json', _hl_router(fx('hl_leaderboard.json'), None))
    with pytest.raises(ProviderFetchError, match=r'3/5 accounts failed'):
        p.fetch(SET, NOW, NOW)


def test_deadline_at_stops_hl_whales_mid_loop(monkeypatch, tmp_path, capsys):
    """The tick deadline passes after the 2nd account: the loop stops, partial aggregate returned, not a failure."""
    p = HlWhales()
    p.state_path = tmp_path / 's.json'
    p.deadline_at = _time.monotonic() + 3600
    n = {'posts': 0}

    def state(url, kw):
        n['posts'] += 1
        if n['posts'] == 2:
            p.deadline_at = _time.monotonic() - 1  # deadline passes during the 2nd call
        return fx('hl_clearinghouse.json')
    monkeypatch.setattr(hl_whales, 'get_json', _hl_router(fx('hl_leaderboard.json'), state))
    monkeypatch.setenv('OPENCLAW_CRYPTO_HL_WHALES_TOP_N', '5')
    res = base.run_provider(p, SET, NOW, NOW)
    assert res.ok and n['posts'] == 2 and p.deadline == 3
    by = {o.ticker: o.fields for o in res.observations}
    assert by['BTC-USD']['whale_perp_accounts_seen'] == 2 and by['BTC-USD']['whale_perp_deadline_skipped'] == 3
    assert 'deadline=1 accounts_seen=2 accounts_skipped=3' in capsys.readouterr().out


def test_leaderboard_fetch_timeout_is_20s(monkeypatch, tmp_path):
    seen = []

    def r(url, **kw):
        seen.append((url, kw.get('timeout')))
        return fx('hl_leaderboard.json') if 'leaderboard' in url else fx('hl_clearinghouse.json')
    monkeypatch.setattr(hl_whales, 'get_json', r)
    p = HlWhales()
    p.state_path = tmp_path / 's.json'
    p.fetch(SET, NOW, NOW)
    assert [t for u, t in seen if 'leaderboard' in u] == [20]


def test_parse_leaderboard_peak_memory_bounded():
    """~48k-row leaderboard: parse_leaderboard keeps at most top_n tuples alive (heap over a generator) — peak
    allocation inside the call < 1 MB, and nothing beyond the 200 addresses is retained."""
    import gc
    import tracemalloc
    payload = {'leaderboardRows': [{'ethAddress': f'0x{i:040x}', 'accountValue': str(float(i % 9973) * 1.5),
                                    'displayName': None, 'prize': 0} for i in range(48_000)]}
    gc.collect()
    tracemalloc.start()
    try:
        base_cur, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        addrs = parse_leaderboard(payload, 200)
        cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(addrs) == 200
    assert peak - base_cur < 1_000_000, peak - base_cur
    assert cur - base_cur < 1_000_000, cur - base_cur
