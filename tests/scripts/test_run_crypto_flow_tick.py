"""Tests for scripts/run_crypto_flow_tick.py — the 15-min crypto flow tick driver."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.ingestion.crypto_flow.models import MARKET_TICKER, Observation, ProviderResult, WhaleEvent
from src.ingestion.crypto_flow.providers.base import Provider
from src.ingestion.crypto_flow.universe import Asset

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('run_crypto_flow_tick', ROOT / 'scripts' / 'run_crypto_flow_tick.py')
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)

NOW = datetime(2026, 11, 6, 1, 15, tzinfo=timezone.utc)
ASSETS = [Asset('BTC-USD', 'BTC/USD', 'BTCUSD', role='benchmark'), Asset('ETH-USD', 'ETH/USD', 'ETHUSD'),
          Asset('USDT-USD', 'USDT/USD', 'USDTUSD', role='stable')]


class _Fake(Provider):
    def __init__(self, name, cadence='tick', obs=None, events=None, boom=False):
        self.name, self.cadence, self._obs, self._events, self._boom = name, cadence, obs or [], events or [], boom
        self.calls = 0

    def _fetch(self, assets, since, now):
        self.calls += 1
        if self._boom:
            raise RuntimeError('boom')
        return list(self._events), list(self._obs)


class _StatefulFake(_Fake):
    """Writes its own cursor into the shared state file, like btc_onchain."""
    state_path = Path('unused')

    def _fetch(self, assets, since, now):
        st = json.loads(Path(self.state_path).read_text()) if Path(self.state_path).exists() else {}
        Path(self.state_path).write_text(json.dumps({**st, 'btc_last_height': 999}))
        return super()._fetch(assets, since, now)


class _HintFake(_Fake):
    def __init__(self, name):
        super().__init__(name)
        self.price_hint, self.price_hints, self.seen = None, {}, None

    def _fetch(self, assets, since, now):
        self.seen = (self.price_hint, dict(self.price_hints))
        return [], []


class _Cur:
    def __init__(s, rows=()):
        s.sql, s.rows = [], list(rows)
    def execute(s, q, p=None): s.sql.append((q, p))
    def fetchall(s): return s.rows if 'DISTINCT ON' in s.sql[-1][0] else []  # latest-snapshot query only
    def __enter__(s): return s
    def __exit__(s, *a): pass


class _Conn:
    def __init__(s, rows=()):
        s.cur = _Cur(rows); s.committed = 0; s.rolled = 0
    def cursor(s): return s.cur
    def commit(s): s.committed += 1
    def rollback(s): s.rolled += 1
    def inserts(s, table): return [p for q, p in s.cur.sql if f'INSERT INTO {table}' in q]


def _ev(h='aa'):
    return WhaleEvent(ts_utc=NOW - timedelta(minutes=5), source='btc_onchain', chain='bitcoin', symbol='BTC',
                      ticker='BTC-USD', amount=25.0, amount_usd=2e6, from_addr='a', from_owner=None,
                      from_owner_type=None, to_addr='b', to_owner='binance', to_owner_type='exchange',
                      tx_type='transfer', tx_hash=h, out_index=0, direction='exchange_in')


def _paths(tmp_path):
    return dict(events_path=tmp_path / 'ev.parquet', flows_path=tmp_path / 'fl.parquet',
                onchain_daily_path=tmp_path / 'od.parquet')


# every should_skip test injects all four checks: the defaults reach real Redis and the real /proc/meminfo
_CLEAR = dict(pgrep=lambda p: False, lock_held=lambda: False, mem_mb=lambda: 8000.0)


def test_skip_during_compute_window():
    # 2026-11-06 is a Friday; 20:10Z = 15:10 ET (EST)
    assert t.should_skip(datetime(2026, 11, 6, 20, 10, tzinfo=timezone.utc), **_CLEAR) == 'compute_window'
    assert t.should_skip(datetime(2026, 11, 6, 20, 55, tzinfo=timezone.utc), **_CLEAR) is None
    assert t.should_skip(datetime(2026, 11, 7, 20, 10, tzinfo=timezone.utc), **_CLEAR) is None  # Saturday


def test_skip_when_compute_step_running():
    at = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
    for name in ('engine.py', 'regime_blended_sizer_live.py', 'alpaca_executor', 'run_collector_once',
                 'pipeline_orchestrator.py'):
        seen = []
        pg = lambda p, name=name: seen.append(p) or p == name  # noqa: E731
        assert t.should_skip(at, **{**_CLEAR, 'pgrep': pg}) == 'compute_running', name
    assert t.should_skip(at, **{**_CLEAR, 'pgrep': lambda p: True}, force=True) is None


def test_skip_when_run_lock_held():
    at = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
    assert t.should_skip(at, **{**_CLEAR, 'lock_held': lambda: True}) == 'run_lock_held'
    assert t.should_skip(at, **{**_CLEAR, 'lock_held': lambda: True}, force=True) is None


def test_skip_on_low_memory(monkeypatch):
    at = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
    monkeypatch.delenv('OPENCLAW_CRYPTO_FLOW_MIN_MEM_MB', raising=False)
    assert t.should_skip(at, **{**_CLEAR, 'mem_mb': lambda: 1499.0}) == 'low_memory'
    assert t.should_skip(at, **{**_CLEAR, 'mem_mb': lambda: 1501.0}) is None
    assert t.should_skip(at, **{**_CLEAR, 'mem_mb': lambda: None}) is None  # unreadable meminfo: not a skip
    monkeypatch.setenv('OPENCLAW_CRYPTO_FLOW_MIN_MEM_MB', '3000')
    assert t.should_skip(at, **{**_CLEAR, 'mem_mb': lambda: 2000.0}) == 'low_memory'
    assert t.should_skip(at, **{**_CLEAR, 'mem_mb': lambda: 10.0}, force=True) is None


def test_mem_available_mb_parses_meminfo(tmp_path):
    p = tmp_path / 'meminfo'
    p.write_text('MemTotal:  8000000 kB\nMemFree:  100 kB\nMemAvailable:   2048000 kB\n')
    assert t.mem_available_mb(str(p)) == 2000.0
    assert t.mem_available_mb(str(tmp_path / 'missing')) is None


def test_redis_run_lock_held_checks_et_and_utc_keys(monkeypatch, capsys):
    import sys
    import types
    got = []

    class _R:
        def get(self, k):
            got.append(k)
            return b'host:1:2026-11-06T00:00:00' if k.endswith('2026-11-06') else None
    monkeypatch.setitem(sys.modules, 'redis', types.SimpleNamespace(from_url=lambda *a, **k: _R()))
    # 2026-11-06 03:00Z = 2026-11-05 22:00 ET: the ET and UTC dates differ, the UTC-date key is held
    assert t.redis_run_lock_held(datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)) is True
    assert set(got) == {'pipeline:run_lock:2026-11-05', 'pipeline:run_lock:2026-11-06'}

    def boom(*a, **k):
        raise ConnectionError('no redis')
    monkeypatch.setitem(sys.modules, 'redis', types.SimpleNamespace(from_url=boom))
    assert t.redis_run_lock_held(datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)) is False
    out = capsys.readouterr().out
    assert 'run-lock check failed (ConnectionError)' in out and 'no redis' not in out


def test_due_providers_by_cadence():
    tick, hourly, daily = _Fake('a'), _Fake('h', 'hourly'), _Fake('d', 'daily')
    ps = [tick, hourly, daily]
    iso = lambda dt: dt.isoformat()  # noqa: E731
    assert t.due_providers(ps, NOW, {}) == ps  # never run → all due
    st = {'last_run': {'a': iso(NOW), 'h': iso(NOW - timedelta(minutes=30)), 'd': iso(NOW - timedelta(hours=5))}}
    assert t.due_providers(ps, NOW, st) == [tick]
    st = {'last_run': {'h': iso(NOW - timedelta(minutes=55)), 'd': iso(NOW - timedelta(hours=23))}}
    assert t.due_providers(ps, NOW, st) == ps


def test_run_tick_writes_snapshot_even_when_a_provider_fails(tmp_path):
    good = _Fake('derivs', obs=[Observation(NOW, 'BTC-USD', 'derivs', {'funding_8h': 0.0001, 'oi_usd': 1e10, 'oi_venues': 'binance'})],
                 events=[_ev()])
    bad = _Fake('binance', boom=True)
    conn = _Conn()
    c = t.run_tick(now_utc=NOW, assets=ASSETS, providers=[good, bad], conn=conn,
                   state_path=tmp_path / 'state.json', dry_run=False, **_paths(tmp_path))
    assert c['providers_failed'] == 1 and c['providers_ok'] == 1 and 'binance' in c['degraded']
    assert c['snapshot_rows'] >= 1 and c['events_new'] == 1
    snaps = {p[0]: json.loads(p[2]) for p in conn.inserts('crypto_flow_snapshot')}
    assert set(snaps) == {'BTC-USD', 'ETH-USD', MARKET_TICKER}  # stable gets no row
    assert snaps['BTC-USD']['funding_8h'] == 0.0001 and snaps[MARKET_TICKER]['providers_failed_this_tick'] == ['binance']
    health = conn.inserts('crypto_flow_provider_health')
    assert [h[0] for h in health] == ['derivs', 'binance', '__tick__'] and health[-1][2] is True
    assert len(conn.inserts('crypto_whale_events_live')) == 1
    st = json.loads((tmp_path / 'state.json').read_text())
    assert 'derivs' in st['last_run'] and 'binance' not in st['last_run']


def test_dry_run_writes_nothing(tmp_path):
    sp = tmp_path / 'state.json'
    sp.write_text(json.dumps({'btc_last_height': 1, 'last_run': {}}))
    before = sp.read_bytes()
    stateful = _StatefulFake('btc_onchain', events=[_ev()])
    c = t.run_tick(now_utc=NOW, assets=ASSETS, providers=[stateful], conn=None,
                   state_path=sp, dry_run=True, **_paths(tmp_path))
    assert stateful.calls == 1 and c['providers_ok'] == 1
    assert sp.read_bytes() == before  # neither provider cursors nor last_run touched the real file
    assert not any(p.exists() for p in _paths(tmp_path).values())
    assert c['snapshot_rows'] == 0 and c['flows_rows'] == 3


def test_gate_off_returns_zero(monkeypatch, capsys):
    monkeypatch.delenv('OPENCLAW_CRYPTO_FLOWS', raising=False)
    assert t.main([]) == 0
    assert 'gate OFF' in capsys.readouterr().out


def test_deadline_skips_remaining_providers(tmp_path):
    ticks = iter([0.0, 0.0])  # tick start, check before 'a'; every later reading is past the deadline
    clock = lambda: next(ticks, 700.0)  # noqa: E731
    a, b, c_ = _Fake('a'), _Fake('b', 'hourly'), _Fake('c')
    conn = _Conn()
    c = t.run_tick(now_utc=NOW, assets=ASSETS, providers=[a, b, c_], conn=conn,
                   state_path=tmp_path / 'state.json', dry_run=False, deadline_s=600, clock=clock,
                   **_paths(tmp_path))
    assert a.calls == 1 and b.calls == 0 and c_.calls == 0
    assert c['providers_skipped'] == 2 and c['skipped'] == ['b', 'c'] and c['skipped_reasons']['b'] == 'deadline'
    health = conn.inserts('crypto_flow_provider_health')
    assert [h[0] for h in health if h[6] == 'deadline'] == ['b', 'c']
    st = json.loads((tmp_path / 'state.json').read_text())
    assert set(st['last_run']) == {'a'}  # deadline-skipped hourly stays due next tick


def test_santiment_not_run_under_dry_run(tmp_path):
    san = _Fake('santiment', 'daily')
    c = t.run_tick(now_utc=NOW, assets=ASSETS, providers=[san], conn=None,
                   state_path=tmp_path / 'state.json', dry_run=True, **_paths(tmp_path))
    assert san.calls == 0 and c['providers_skipped'] == 1
    assert c['skipped_reasons'] == {'santiment': 'skipped_dry_run'}


def test_price_hints_seeded_from_previous_snapshot(tmp_path):
    ts = NOW - timedelta(minutes=15)
    rows = [('BTC-USD', {'price_usd': 70000.0, 'stable_x': 1}, ts), ('ETH-USD', json.dumps({'price_usd': 3000.0}), ts)]
    hp = _HintFake('btc_onchain')
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[hp], conn=_Conn(rows),
               state_path=tmp_path / 'state.json', dry_run=False, **_paths(tmp_path))
    assert hp.seen == (70000.0, {'BTC-USD': 70000.0, 'ETH-USD': 3000.0})


def test_state_save_preserves_provider_cursors(tmp_path):
    sp = tmp_path / 'state.json'
    sp.write_text(json.dumps({'btc_last_height': 1}))
    p = _StatefulFake('btc_onchain')
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[p], conn=_Conn(), state_path=sp, dry_run=False, **_paths(tmp_path))
    st = json.loads(sp.read_text())
    assert st['btc_last_height'] == 999 and 'btc_onchain' in st['last_run']


def test_slow_fields_carried_forward_from_slow_obs(tmp_path):
    sp = tmp_path / 'state.json'
    ts = (NOW - timedelta(minutes=30)).isoformat()
    sp.write_text(json.dumps({'last_run': {'hl_whales': NOW.isoformat()},
                              'slow_obs': {'BTC-USD': {'whale_perp_net_usd': {'v': -5e6, 'ts': ts, 'cad': 'hourly'}}}}))
    # the previous snapshot's features are NOT a carry source (funding_8h is a tick field)
    rows = [('BTC-USD', {'whale_perp_net_usd': 1.0, 'funding_8h': 0.5}, NOW - timedelta(minutes=15))]
    conn = _Conn(rows)
    hl = _Fake('hl_whales', 'hourly')
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[hl], conn=conn, state_path=sp, dry_run=False, **_paths(tmp_path))
    assert hl.calls == 0
    snaps = {p[0]: json.loads(p[2]) for p in conn.inserts('crypto_flow_snapshot')}
    assert snaps['BTC-USD']['whale_perp_net_usd'] == -5e6
    assert snaps['BTC-USD']['funding_8h'] is None  # tick field is not carried forward
    st = json.loads(sp.read_text())
    assert st['slow_obs']['BTC-USD']['whale_perp_net_usd'] == {'v': -5e6, 'ts': ts, 'cad': 'hourly'}
    assert 'slow_keys' not in st


def test_slow_obs_carry_uses_raw_observations_not_outputs(tmp_path):
    """Tick 1: macro_sent spot 100 + perp mark 100. Tick 2 (macro_sent not due): mark 101 → price_spot_usd stays
    the RAW carried spot 100, never tick 1's computed price_usd."""
    sp = tmp_path / 'state.json'
    macro = _Fake('macro_sent', 'hourly', obs=[Observation(NOW, 'BTC-USD', 'macro_sent',
                                                           {'spot_px': 100.0, 'cg_ret_1h': 0.5, 'cg_ret_24h': 1.5})])
    d1 = _Fake('derivs', obs=[Observation(NOW, 'BTC-USD', 'derivs', {'mark_px': 100.0})])
    c1 = _Conn()
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[macro, d1], conn=c1, state_path=sp, dry_run=False,
               **_paths(tmp_path))
    s1 = {p[0]: json.loads(p[2]) for p in c1.inserts('crypto_flow_snapshot')}['BTC-USD']
    assert s1['price_usd'] == 100.0 and s1['price_spot_usd'] == 100.0
    t2 = NOW + timedelta(minutes=15)
    d2 = _Fake('derivs', obs=[Observation(t2, 'BTC-USD', 'derivs', {'mark_px': 101.0})])
    c2 = _Conn([('BTC-USD', s1, NOW)])
    t.run_tick(now_utc=t2, assets=ASSETS, providers=[macro, d2], conn=c2, state_path=sp, dry_run=False,
               **_paths(tmp_path))
    assert macro.calls == 1  # not due on tick 2
    s2 = {p[0]: json.loads(p[2]) for p in c2.inserts('crypto_flow_snapshot')}['BTC-USD']
    assert s2['price_usd'] == 101.0 and s2['price_spot_usd'] == 100.0
    assert s2['ret_1h'] == 0.5 and s2['ret_24h'] == 1.5  # coingecko fallbacks, carried raw


def test_slow_obs_expires_after_three_cadences():
    def e(age, cad):
        return {'v': 1.0, 'ts': (NOW - age).isoformat(), 'cad': cad}
    slow = {'BTC-USD': {'h_ok': e(timedelta(hours=2, minutes=59), 'hourly'), 'h_old': e(timedelta(hours=3, minutes=1), 'hourly'),
                        'd_ok': e(timedelta(days=2, hours=23), 'daily'), 'd_old': e(timedelta(days=3, hours=1), 'daily')},
            'ETH-USD': {'x': e(timedelta(hours=4), 'hourly')}}
    assert t.prune_slow_obs(slow, NOW) == {'BTC-USD': {'h_ok': slow['BTC-USD']['h_ok'], 'd_ok': slow['BTC-USD']['d_ok']}}
    m = t.merge_observations(slow, [], now_utc=NOW)
    assert m == {'BTC-USD': {'h_ok': 1.0, 'd_ok': 1.0}}  # expired entries are None (absent), ETH dropped
    r = ProviderResult('hl', True, [], [Observation(NOW, 'SOL-USD', 'hl', {'w': 2.0})], 1, 0)
    tick = ProviderResult('derivs', True, [], [Observation(NOW, 'SOL-USD', 'derivs', {'funding_8h': 1.0})], 1, 0)
    out = t.update_slow_obs(slow, [r, tick], {'hl': 'hourly', 'derivs': 'tick'}, NOW)
    assert 'ETH-USD' not in out and 'h_old' not in out['BTC-USD']
    assert out['SOL-USD'] == {'w': {'v': 2.0, 'ts': NOW.isoformat(), 'cad': 'hourly'}}  # tick fields never enter


def test_price_hint_not_seeded_from_stale_snapshot(tmp_path):
    sp = tmp_path / 'state.json'
    spot_ts = lambda age: (NOW - age).isoformat()  # noqa: E731
    sp.write_text(json.dumps({'slow_obs': {
        'ETH-USD': {'spot_px': {'v': 3100.0, 'ts': spot_ts(timedelta(hours=1)), 'cad': 'hourly'}},
        'SOL-USD': {'spot_px': {'v': 150.0, 'ts': spot_ts(timedelta(hours=2, minutes=59)), 'cad': 'hourly'}}}}))
    rows = [('BTC-USD', {'price_usd': 70000.0}, NOW - timedelta(hours=2, minutes=1)),   # stale snapshot, no spot
            ('ETH-USD', {'price_usd': 3000.0}, NOW - timedelta(hours=3)),               # stale snapshot → spot
            ('XRP-USD', {'price_usd': 2.0}, NOW - timedelta(hours=1, minutes=59))]      # fresh snapshot
    hp = _HintFake('btc_onchain')
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[hp], conn=_Conn(rows), state_path=sp, dry_run=False,
               **_paths(tmp_path))
    assert hp.seen == (None, {'ETH-USD': 3100.0, 'SOL-USD': 150.0, 'XRP-USD': 2.0})
    assert t.price_hints({}, {'SOL-USD': {'spot_px': {'v': 1.0, 'ts': spot_ts(timedelta(hours=3))}}}, NOW) == {}


def test_overlap_lock_skips_tick(tmp_path, capsys):
    import fcntl
    sp = tmp_path / 'state.json'
    p = _Fake('derivs')
    with open(str(sp) + '.lock', 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c = t.run_tick(now_utc=NOW, assets=ASSETS, providers=[p], conn=_Conn(), state_path=sp, dry_run=False,
                       **_paths(tmp_path))
    assert p.calls == 0 and c['skipped'] == ['locked'] and c['providers_ok'] == 0
    out = capsys.readouterr().out
    assert '[crypto-flow] skipped=locked' in out and 'skipped=locked dt_s=' in out
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[p], conn=_Conn(), state_path=sp, dry_run=False, **_paths(tmp_path))
    assert p.calls == 1  # released lock → next tick runs


def test_daily_provider_backs_off_one_hour_after_failure(tmp_path):
    sp = tmp_path / 'state.json'
    d = _Fake('cftc_cot', 'daily', boom=True)
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[d], conn=_Conn(), state_path=sp, dry_run=False, **_paths(tmp_path))
    st = json.loads(sp.read_text())
    assert d.calls == 1 and 'cftc_cot' in st['last_attempt'] and 'cftc_cot' not in st['last_run']
    assert t.due_providers([d], NOW + timedelta(minutes=59), st) == []
    assert t.due_providers([d], NOW + timedelta(minutes=60), st) == [d]
    h = _Fake('h', 'hourly')  # hourly/tick cadences ignore the failure backoff
    assert t.due_providers([h], NOW + timedelta(minutes=15), {'last_attempt': {'h': NOW.isoformat()}}) == [h]


def test_snapshot_history_passed_to_features(tmp_path, monkeypatch):
    seen = []
    real = t.F.compute_ticker_features
    monkeypatch.setattr(t.F, 'compute_ticker_features', lambda *a, **k: seen.append(k.get('snap_hist')) or real(*a, **k))
    sentinel = pd.DataFrame({'ticker': ['BTC-USD'], 'ts_utc': [NOW - timedelta(hours=1)], 'oi_usd': [1.0],
                             'oi_venues': ['a'], 'price_usd': [1.0]})
    monkeypatch.setattr(t.store, 'read_snapshot_history', lambda conn, since: sentinel)
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[], conn=_Conn(), state_path=tmp_path / 's.json', dry_run=False,
               **_paths(tmp_path))
    assert len(seen) == 2 and all(s is sentinel for s in seen)
    seen.clear()
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[], conn=None, state_path=tmp_path / 's.json', dry_run=True,
               **_paths(tmp_path))
    assert seen == [None, None]


def test_merge_observations_later_provider_wins_over_carry():
    ts = NOW.isoformat()
    slow = {'BTC-USD': {'whale_perp_net_usd': {'v': 1.0, 'ts': ts, 'cad': 'hourly'},
                        'spot_px': {'v': 1.0, 'ts': ts, 'cad': 'hourly'}}}
    obs = [Observation(NOW, 'BTC-USD', 'a', {'spot_px': 2.0}), Observation(NOW, 'BTC-USD', 'b', {'spot_px': 3.0})]
    m = t.merge_observations(slow, obs, now_utc=NOW)
    assert m['BTC-USD'] == {'whale_perp_net_usd': 1.0, 'spot_px': 3.0}


def test_flows_frame_has_ticker_rows_plus_market_row_with_hour_floor(tmp_path):
    from src.ingestion.crypto_flow import features as F, store
    per = {'BTC-USD': {k: None for k in F.FEATURE_COLS}}
    per['BTC-USD'].update(whale_score_components={'netflow': 1}, whale_direction='neutral', delev_1h=False)
    market = {'fear_greed': 40.0, 'providers_failed_this_tick': [], 'shock_lines': ['x'], 'cot_report_date': None,
              'stable_supply_missing': 'USDG', 'options_degraded_btc': True, 'oi_usd_total_venues_mixed': False}
    df = t.build_flows_frame(per, market, NOW)
    assert list(df['ticker']) == ['BTC-USD', MARKET_TICKER]
    assert (df['ts_utc'] == pd.Timestamp('2026-11-06 01:00', tz='UTC')).all()
    assert 'shock_lines' not in df.columns and 'whale_score_components' not in df.columns
    assert df.loc[1, 'providers_failed_this_tick'] == ''
    assert df.loc[1, 'stable_supply_missing'] == 'USDG'  # string column, not coerced to NaN
    assert bool(df.loc[1, 'options_degraded_btc']) is True and bool(df.loc[1, 'oi_usd_total_venues_mixed']) is False
    p = tmp_path / 'fl.parquet'
    store.write_flows(df, path=p)
    # second tick an hour later with values in previously all-None columns must merge cleanly
    per2 = {'BTC-USD': {**per['BTC-USD'], 'oi_venues': 'binance,okx', 'oi_usd': 1e9, 'delev_1h': True}}
    df2 = t.build_flows_frame(per2, {**market, 'providers_failed_this_tick': ['okx'], 'cot_report_date': '2026-11-03'},
                              NOW + timedelta(hours=1))
    assert store.write_flows(df2, path=p) == 4
    back = pd.read_parquet(p)
    assert back[back.ticker == MARKET_TICKER]['providers_failed_this_tick'].tolist() == ['', 'okx']


def test_locked_tick_writes_health_row(tmp_path):
    import fcntl
    sp = tmp_path / 'state.json'
    conn = _Conn()
    with open(str(sp) + '.lock', 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        t.run_tick(now_utc=NOW, assets=ASSETS, providers=[_Fake('derivs')], conn=conn, state_path=sp,
                   dry_run=False, **_paths(tmp_path))
        dry = _Conn()
        t.run_tick(now_utc=NOW, assets=ASSETS, providers=[], conn=dry, state_path=sp, dry_run=True, **_paths(tmp_path))
    h = conn.inserts('crypto_flow_provider_health')
    assert len(h) == 1 and h[0][0] == '__tick__' and h[0][2] is False and h[0][6] == 'locked'
    assert dry.inserts('crypto_flow_provider_health') == []  # dry-run never writes


def test_locked_tick_health_write_failure_does_not_crash(tmp_path):
    import fcntl
    sp = tmp_path / 'state.json'

    class _Bad(_Conn):
        def cursor(s):
            raise RuntimeError('db down')
    with open(str(sp) + '.lock', 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c = t.run_tick(now_utc=NOW, assets=ASSETS, providers=[], conn=_Bad(), state_path=sp, dry_run=False,
                       **_paths(tmp_path))
    assert c['skipped'] == ['locked']


def test_tick_deadline_cut_at_weekday_1500_et():
    # Fri 2026-11-06 19:55Z = 14:55 ET: the 600 s budget is cut to 5 min (15:00 ET = 20:00Z)
    assert t.tick_deadline(datetime(2026, 11, 6, 19, 55, tzinfo=timezone.utc), 600) == \
        datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
    early = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
    assert t.tick_deadline(early, 600) == early + timedelta(seconds=600)
    sat = datetime(2026, 11, 7, 19, 55, tzinfo=timezone.utc)  # Saturday: no compute window
    assert t.tick_deadline(sat, 600) == sat + timedelta(seconds=600)


def test_deadline_at_passed_to_providers(tmp_path):
    class _DL(_Fake):
        deadline_at = None

        def _fetch(self, assets, since, now):
            self.seen = self.deadline_at
            return [], []
    import time as _time
    p = _DL('spot_tape')
    before = _time.monotonic()
    # 19:55Z Friday: the 15:00 ET cut makes the bound ~300 s, not 600
    t.run_tick(now_utc=datetime(2026, 11, 6, 19, 55, tzinfo=timezone.utc), assets=ASSETS, providers=[p], conn=None,
               state_path=tmp_path / 's.json', dry_run=True, deadline_s=600, **_paths(tmp_path))
    assert before + 299 <= p.seen <= _time.monotonic() + 301


def test_sigterm_releases_lock_and_prints_summary(tmp_path, capsys):
    import fcntl
    import os
    import signal
    sp = tmp_path / 'state.json'

    class _Slow(_Fake):
        def _fetch(self, assets, since, now):
            os.kill(os.getpid(), signal.SIGTERM)  # the scheduler's `timeout 840` firing mid-provider
            raise AssertionError('handler must abort before this line')
    later = _Fake('later')
    before = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(t.TickTerminated):
            t.run_tick(now_utc=NOW, assets=ASSETS, providers=[_Slow('hl_whales'), later], conn=_Conn(), state_path=sp,
                       dry_run=False, install_sigterm=True, **_paths(tmp_path))
    finally:
        assert signal.getsignal(signal.SIGTERM) == before  # previous handler restored
        signal.signal(signal.SIGTERM, before)
    assert later.calls == 0
    out = capsys.readouterr().out
    assert '[crypto-flow] terminated by deadline' in out and 'skipped=sigterm' in out
    with open(str(sp) + '.lock', 'a') as fh:  # lock released
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)


def test_main_returns_143_on_sigterm(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENCLAW_CRYPTO_FLOWS', '1')
    monkeypatch.setattr(t, 'should_skip', lambda now, force=False: None)
    monkeypatch.setattr(t, 'STATE_PATH', tmp_path / 's.json')
    monkeypatch.setattr(t.os, 'nice', lambda n: 0)  # never renice the test runner

    def boom(**kw):
        assert kw['install_sigterm'] is True
        raise t.TickTerminated()
    monkeypatch.setattr(t, 'run_tick', boom)
    monkeypatch.setattr('src.ingestion.crypto_flow.universe.load_universe', lambda: ASSETS)
    assert t.main(['--dry-run', '--providers', 'derivs']) == 143
