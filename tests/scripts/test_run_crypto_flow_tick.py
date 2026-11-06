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
    def fetchall(s): return s.rows
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


def test_skip_during_compute_window():
    # 2026-11-06 is a Friday; 20:10Z = 15:10 ET (EST)
    assert t.should_skip(datetime(2026, 11, 6, 20, 10, tzinfo=timezone.utc), pgrep=lambda p: False, force=False) == 'compute_window'
    assert t.should_skip(datetime(2026, 11, 6, 20, 55, tzinfo=timezone.utc), pgrep=lambda p: False, force=False) is None
    assert t.should_skip(datetime(2026, 11, 7, 20, 10, tzinfo=timezone.utc), pgrep=lambda p: False, force=False) is None  # Saturday


def test_skip_when_orchestrator_running():
    assert t.should_skip(datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc), pgrep=lambda p: 'orchestrator' in p, force=False) == 'orchestrator_running'
    assert t.should_skip(datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc), pgrep=lambda p: True, force=True) is None


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
    assert snaps['BTC-USD']['funding_8h'] == 0.0001 and snaps[MARKET_TICKER]['providers_degraded'] == ['binance']
    assert len(conn.inserts('crypto_flow_provider_health')) == 2
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
    rows = [('BTC-USD', {'price_usd': 70000.0, 'stable_x': 1}), ('ETH-USD', json.dumps({'price_usd': 3000.0}))]
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


def test_slow_fields_carried_forward_from_previous_snapshot(tmp_path):
    sp = tmp_path / 'state.json'
    sp.write_text(json.dumps({'last_run': {'hl_whales': NOW.isoformat()}, 'slow_keys': ['whale_perp_net_usd']}))
    rows = [('BTC-USD', {'whale_perp_net_usd': -5e6, 'funding_8h': 0.5})]
    conn = _Conn(rows)
    hl = _Fake('hl_whales', 'hourly')
    t.run_tick(now_utc=NOW, assets=ASSETS, providers=[hl], conn=conn, state_path=sp, dry_run=False, **_paths(tmp_path))
    assert hl.calls == 0
    snaps = {p[0]: json.loads(p[2]) for p in conn.inserts('crypto_flow_snapshot')}
    assert snaps['BTC-USD']['whale_perp_net_usd'] == -5e6
    assert snaps['BTC-USD']['funding_8h'] is None  # tick field is not carried forward


def test_merge_observations_later_provider_wins_over_carry():
    prev = {'BTC-USD': {'whale_perp_net_usd': 1.0, 'price_usd': 1.0}}
    obs = [Observation(NOW, 'BTC-USD', 'a', {'price_usd': 2.0}), Observation(NOW, 'BTC-USD', 'b', {'price_usd': 3.0})]
    m = t.merge_observations(prev, obs, slow_keys={'whale_perp_net_usd', 'price_usd'})
    assert m['BTC-USD'] == {'whale_perp_net_usd': 1.0, 'price_usd': 3.0}


def test_flows_frame_has_ticker_rows_plus_market_row_with_hour_floor(tmp_path):
    from src.ingestion.crypto_flow import features as F, store
    per = {'BTC-USD': {k: None for k in F.FEATURE_COLS}}
    per['BTC-USD'].update(whale_score_components={'netflow': 1}, whale_direction='neutral', delev_1h=False)
    market = {'fear_greed': 40.0, 'providers_degraded': [], 'shock_lines': ['x'], 'cot_report_date': None}
    df = t.build_flows_frame(per, market, NOW)
    assert list(df['ticker']) == ['BTC-USD', MARKET_TICKER]
    assert (df['ts_utc'] == pd.Timestamp('2026-11-06 01:00', tz='UTC')).all()
    assert 'shock_lines' not in df.columns and 'whale_score_components' not in df.columns
    assert df.loc[1, 'providers_degraded'] == ''
    p = tmp_path / 'fl.parquet'
    store.write_flows(df, path=p)
    # second tick an hour later with values in previously all-None columns must merge cleanly
    per2 = {'BTC-USD': {**per['BTC-USD'], 'oi_venues': 'binance,okx', 'oi_usd': 1e9, 'delev_1h': True}}
    df2 = t.build_flows_frame(per2, {**market, 'providers_degraded': ['okx'], 'cot_report_date': '2026-11-03'},
                              NOW + timedelta(hours=1))
    assert store.write_flows(df2, path=p) == 4
    back = pd.read_parquet(p)
    assert back[back.ticker == MARKET_TICKER]['providers_degraded'].tolist() == ['', 'okx']
